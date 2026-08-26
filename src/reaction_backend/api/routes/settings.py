"""Settings / Privacy — S23, S28 (api-contract §16, Issue #23).

#23-A (본 PR) — S23 Settings 실구현:
- GET   /settings            — tone, language(ko 고정), timezone, 알림 요약
- PATCH /settings/tone-mode  — gentle / strict / encouraging

#23-B (후속) — S28 Privacy, 아래는 501 스텁 유지:
- POST  /settings/anonymize  — 즉시 익명화 (2단계 확인 토큰 + `_encrypted` 필드 마스킹)
- GET   /privacy/consent     — 동의 기록 (append-only `user_consents` 테이블 → 마이그레이션 동반)
- POST  /privacy/consent     — 신규 동의 (마케팅/연구 토글)

#321(FE #237 §4) — Play Store 계정 삭제 요구:
- POST  /settings/delete-account — 계정 삭제 (2단계 확인 토큰 + anonymize 마스킹 + soft delete)

톤 prefix(`llm/prompt_compose.py`)의 `aiClient.run()` 배선은 ADR-0003 §1 동결 시그니처
변경 + LangGraph state(tone_mode) 전달을 수반 → 후속 PR(ADR-0003 addendum). 본 PR 은
톤 모드 영속화(PATCH)와 prefix 헬퍼까지.

자동 익명화(90일 비활성, 04:00 KST cron)는 `scheduler/anonymize_inactive.py` — 이 파일의
`anonymize` 와 **같은 정의**의 익명화를 시간 트리거로 돌린다(#24). 마스킹 로직은 양쪽 다
`PrivacyRepo.anonymize_user` 단일 소스.
"""

from __future__ import annotations

from datetime import time
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.auth.confirm import issue_confirmation_token, verify_confirmation_token
from reaction_backend.db.models.behavioral_profile import BehavioralProfile
from reaction_backend.db.models.interaction_style import InteractionStyle
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.user import User
from reaction_backend.db.models.user_consent import UserConsent
from reaction_backend.db.session import get_db
from reaction_backend.repositories.consent_repo import ConsentRepo, get_consent_repo
from reaction_backend.repositories.notification_repo import (
    NotificationRepo,
    get_notification_repo,
)
from reaction_backend.repositories.privacy_repo import PrivacyRepo, get_privacy_repo
from reaction_backend.repositories.profile_repo import ProfileRepo, get_profile_repo
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.safety.encryption import ANONYMIZED_SENTINEL
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.settings import (
    AnonymizeRequest,
    AnonymizeResponse,
    BehavioralProfileView,
    ConsentCreateRequest,
    ConsentItem,
    ConsentListResponse,
    DeleteAccountRequest,
    DeleteAccountResponse,
    InteractionStyleView,
    NotificationSummary,
    ProfileResponse,
    ProfileUpdateRequest,
    SettingsResponse,
    ToneModeUpdateRequest,
)

router = APIRouter(prefix="/settings", tags=["settings"])
router_privacy = APIRouter(prefix="/privacy", tags=["privacy"])

NotifRepoDep = Annotated[NotificationRepo, Depends(get_notification_repo)]
ProfileRepoDep = Annotated[ProfileRepo, Depends(get_profile_repo)]
UserRepoDep = Annotated[UserRepo, Depends(get_user_repo)]
ConsentRepoDep = Annotated[ConsentRepo, Depends(get_consent_repo)]
PrivacyRepoDep = Annotated[PrivacyRepo, Depends(get_privacy_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]

# 2단계 확인 토큰 용도 — 익명화 전용.
_ANONYMIZE_PURPOSE = "anonymize"
# 계정 삭제 전용 (#321) — anonymize 와 토큰을 섞어 쓰지 않는다(서로 다른 되돌릴 수 없는
# 작업이라 한쪽 확인 토큰으로 다른 쪽을 실행할 수 있으면 안 된다).
_DELETE_ACCOUNT_PURPOSE = "delete_account"


def _notif_summary(setting: NotificationSetting | None) -> NotificationSummary | None:
    if setting is None:
        return None
    return NotificationSummary(
        morning_brief_time=setting.morning_brief_time.strftime("%H:%M"),
        evening_reflection_time=setting.evening_reflection_time.strftime("%H:%M"),
        pre_card_enabled=setting.pre_card_enabled,
    )


def _to_settings(user: User, setting: NotificationSetting | None) -> SettingsResponse:
    # language 는 스키마 default("ko") — MVP 한국어 잠금 (DevBaseline §1.4).
    return SettingsResponse(
        tone_mode=user.tone_mode,  # type: ignore[arg-type]
        timezone=user.timezone,
        notifications=_notif_summary(setting),
    )


@router.get("")
async def get_settings(user: CurrentUser, notif_repo: NotifRepoDep) -> SettingsResponse:
    """내 설정 메타 — tone / language / timezone + 알림 요약.

    읽기 전용 — 알림 설정 행이 없으면 `notifications=null` (행을 생성하지 않는다).
    """
    setting = await notif_repo.get_by_user(user.id)
    return _to_settings(user, setting)


@router.patch("/tone-mode")
async def update_tone_mode(
    body: ToneModeUpdateRequest,
    user: CurrentUser,
    user_repo: UserRepoDep,
    notif_repo: NotifRepoDep,
    session: SessionDep,
) -> SettingsResponse:
    """톤 모드 변경 — gentle / strict / encouraging.

    값 검증은 스키마 Literal (그 외 값 → 422 `COMMON_VALIDATION_ERROR`).
    onboarding 상태 전이는 없다 (톤은 설정 화면에서 자유 변경).
    """
    await user_repo.set_tone_mode(user, body.tone_mode)
    setting = await notif_repo.get_by_user(user.id)
    await session.commit()
    return _to_settings(user, setting)


# ───── 프로필 메모리 (Policy Snapshot 레이어) — #A-1·A-2 ─────


def _hhmm(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value is not None else None


def _validate_hhmm(value: str, field: str) -> None:
    """활동 시간대 'HH:MM' 검증(자정=24:00 허용). 위반이면 422."""
    try:
        hh_s, mm_s = value.split(":")
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, AttributeError):
        hh = mm = -1
    ok = (hh == 24 and mm == 0) or (0 <= hh <= 23 and 0 <= mm <= 59)
    if not ok:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            f"{field} 는 HH:MM 형식이어야 해요 (받은 값: {value!r}).",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field=field,
        )


def _behavioral_view(row: BehavioralProfile | None) -> BehavioralProfileView | None:
    if row is None:
        return None
    return BehavioralProfileView(
        energy_cycle=row.energy_cycle,  # type: ignore[arg-type]
        attention_span=row.attention_span,
        time_chunk_preference=row.time_chunk_preference,  # type: ignore[arg-type]
        preferred_start_time=_hhmm(row.preferred_start_time),
        preferred_end_time=_hhmm(row.preferred_end_time),
    )


def _interaction_view(row: InteractionStyle | None) -> InteractionStyleView | None:
    if row is None:
        return None
    return InteractionStyleView(
        recovery_tone=row.recovery_tone,  # type: ignore[arg-type]
        suggestion_style=row.suggestion_style,  # type: ignore[arg-type]
        explanation_depth=row.explanation_depth,  # type: ignore[arg-type]
        reminder_frequency=row.reminder_frequency,  # type: ignore[arg-type]
    )


async def _profile_response(user: User, repo: ProfileRepo) -> ProfileResponse:
    fmp = user.focus_mode_preferences or {}
    beh = await repo.get_behavioral(user.id)
    return ProfileResponse(
        behavioral=_behavioral_view(beh),
        interaction=_interaction_view(await repo.get_interaction(user.id)),
        downscope_unit_min=fmp.get("downscope_unit_min"),
        rest_ok=fmp.get("rest_ok"),
        # 편집값(fmp) 우선, 없으면 인터뷰가 넣은 preferred_start/end 로 폴백.
        activity_start=fmp.get("activity_start")
        or (_hhmm(beh.preferred_start_time) if beh else None),
        activity_end=fmp.get("activity_end") or (_hhmm(beh.preferred_end_time) if beh else None),
    )


@router.get("/profile")
async def get_profile(user: CurrentUser, repo: ProfileRepoDep) -> ProfileResponse:
    """지속형 프로필 메모리 — 에너지/시간(behavioral) + 톤/빈도(interaction) + 회복 선호.

    인터뷰가 아직 안 채웠으면 해당 항목 null (행/키를 생성하지 않는다).
    """
    return await _profile_response(user, repo)


@router.patch("/profile")
async def update_profile(
    body: ProfileUpdateRequest,
    user: CurrentUser,
    repo: ProfileRepoDep,
    session: SessionDep,
) -> ProfileResponse:
    """프로필 메모리 부분 수정 — 지정 필드만 갱신(미지정 유지). 행/키 없으면 생성.

    enum 검증은 스키마 Literal (그 외 값 → 422 `COMMON_VALIDATION_ERROR`).
    회복 선호(downscopeUnitMin·restOk)는 `users.focus_mode_preferences`(JSONB)에 병합 저장.
    """
    behavioral_fields = {
        "energy_cycle": body.energy_cycle,
        "attention_span": body.attention_span,
        "time_chunk_preference": body.time_chunk_preference,
    }
    interaction_fields = {
        "recovery_tone": body.recovery_tone,
        "suggestion_style": body.suggestion_style,
        "explanation_depth": body.explanation_depth,
        "reminder_frequency": body.reminder_frequency,
    }
    if any(v is not None for v in behavioral_fields.values()):
        await repo.upsert_behavioral(user.id, fields=behavioral_fields)
    if any(v is not None for v in interaction_fields.values()):
        await repo.upsert_interaction(user.id, fields=interaction_fields)
    fmp_changed = any(
        v is not None
        for v in (body.downscope_unit_min, body.rest_ok, body.activity_start, body.activity_end)
    )
    if fmp_changed:
        fmp = dict(user.focus_mode_preferences or {})
        if body.downscope_unit_min is not None:
            fmp["downscope_unit_min"] = body.downscope_unit_min
        if body.rest_ok is not None:
            fmp["rest_ok"] = body.rest_ok
        if body.activity_start is not None:
            _validate_hhmm(body.activity_start, "activityStart")
            fmp["activity_start"] = body.activity_start
        if body.activity_end is not None:
            _validate_hhmm(body.activity_end, "activityEnd")
            fmp["activity_end"] = body.activity_end
        user.focus_mode_preferences = fmp  # 새 dict 재대입 → JSONB 변경 감지
    await session.commit()

    return await _profile_response(user, repo)


# ───── S28 Privacy — Issue #23-B ─────


def _consent_item(consent: UserConsent) -> ConsentItem:
    return ConsentItem(
        consent_type=consent.consent_type,  # type: ignore[arg-type]
        is_granted=consent.is_granted,
        updated_at=consent.created_at,
    )


@router_privacy.get("/consent")
async def get_consent(user: CurrentUser, repo: ConsentRepoDep) -> ConsentListResponse:
    """동의 현황 — consent_type 별 최신 기록 (필수/마케팅/연구)."""
    rows = await repo.list_current(user.id)
    return ConsentListResponse(consents=[_consent_item(c) for c in rows])


@router_privacy.post("/consent")
async def create_consent(
    body: ConsentCreateRequest,
    user: CurrentUser,
    repo: ConsentRepoDep,
    session: SessionDep,
) -> ConsentListResponse:
    """동의/철회 — append-only 새 기록 추가 후 갱신된 현황 반환.

    `consentType` 외 값은 Pydantic Literal → 422 `COMMON_VALIDATION_ERROR`.
    """
    await repo.add(user.id, body.consent_type, is_granted=body.granted)
    await session.commit()
    rows = await repo.list_current(user.id)
    return ConsentListResponse(consents=[_consent_item(c) for c in rows])


@router.post("/anonymize")
async def anonymize(
    body: AnonymizeRequest,
    user: CurrentUser,
    privacy_repo: PrivacyRepoDep,
    session: SessionDep,
) -> AnonymizeResponse:
    """즉시 익명화 — 2단계 확인.

    - `confirmationToken` 없으면 step1: 확인 토큰 발급(미적용).
    - `confirmationToken` 있으면 step2: 검증 후 `_encrypted` 필드 마스킹 + 이름 마스킹 +
      `is_anonymized`/`anonymized_at` set. hard delete 아님(행 보존).
    """
    if user.is_anonymized:
        raise ApiError(
            ErrorCode.PRIVACY_ALREADY_ANONYMIZED,
            "이미 익명화된 계정이에요.",
            http_status=HTTPStatus.CONFLICT,
        )

    if body.confirmation_token is None:
        token, expires_at = issue_confirmation_token(user.id, _ANONYMIZE_PURPOSE)
        return AnonymizeResponse(
            status="confirmation_required",
            message="정말 익명화할까요? 이 작업은 되돌릴 수 없어요. 확인 토큰으로 한 번 더 요청해 주세요.",
            confirmation_token=token,
            expires_at=expires_at,
        )

    if not verify_confirmation_token(body.confirmation_token, user.id, _ANONYMIZE_PURPOSE):
        raise ApiError(
            ErrorCode.PRIVACY_INVALID_CONFIRMATION,
            "확인 토큰이 유효하지 않거나 만료됐어요. 다시 시도해 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="confirmationToken",
        )

    masked = await privacy_repo.anonymize_user(user.id)
    anonymized_at = now_kst()
    user.is_anonymized = True
    user.anonymized_at = anonymized_at
    user.name = ANONYMIZED_SENTINEL
    await session.commit()

    return AnonymizeResponse(
        status="anonymized",
        message="익명화를 완료했어요. 개인정보는 더 이상 식별되지 않아요.",
        anonymized_at=anonymized_at,
        masked_count=masked,
    )


@router.post("/delete-account")
async def delete_account(
    body: DeleteAccountRequest,
    user: CurrentUser,
    privacy_repo: PrivacyRepoDep,
    session: SessionDep,
) -> DeleteAccountResponse:
    """계정 삭제 — 2단계 확인 (#321, FE #237 §4, Play Store 정책 요구).

    `anonymize`(S28, 계정은 유지한 채 과거 텍스트만 마스킹)와는 다른 작업이다 — 이건 앱에
    다시 들어올 수 없게 만드는 것까지 포함한다. hard delete 는 하지 않는다(AGENTS §2) —
    같은 `anonymize_user()` PII 마스킹 위에 `archived_at` 을 얹어 **soft delete** 한다.

    `archived_at` 을 세우는 순간 `UserRepo.get_by_id`/`get_by_email` 의 `archived_at IS NULL`
    필터에 걸려, 이미 발급된 access token 은 다음 요청의 `get_current_user` 에서 그대로
    401 `AUTH_INVALID_TOKEN` 이 된다 — 별도 access-token 블랙리스트가 필요 없다. refresh
    token 은 `/auth/refresh` 가 이제 같은 필터로 사용자 존재를 확인하므로(이 PR 에서 함께
    고침) 동일하게 막힌다.

    email 도 함께 마스킹한다 — `email` 에 hard UNIQUE 제약이 있어(파샬 인덱스 아님) 원본을
    남기면 그 이메일로 재가입이 영영 불가능해진다. `user.id` 는 유일하므로 유니크 제약을
    깨지 않는 결정적 sentinel 로 치환.
    """
    if body.confirmation_token is None:
        token, expires_at = issue_confirmation_token(user.id, _DELETE_ACCOUNT_PURPOSE)
        return DeleteAccountResponse(
            status="confirmation_required",
            message="정말 계정을 삭제할까요? 이 작업은 되돌릴 수 없어요. 확인 토큰으로 한 번 더 요청해 주세요.",
            confirmation_token=token,
            expires_at=expires_at,
        )

    if not verify_confirmation_token(body.confirmation_token, user.id, _DELETE_ACCOUNT_PURPOSE):
        raise ApiError(
            ErrorCode.PRIVACY_INVALID_CONFIRMATION,
            "확인 토큰이 유효하지 않거나 만료됐어요. 다시 시도해 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="confirmationToken",
        )

    await privacy_repo.anonymize_user(user.id)
    deleted_at = now_kst()
    user.is_anonymized = True
    user.anonymized_at = deleted_at
    user.name = ANONYMIZED_SENTINEL
    user.email = f"deleted-{user.id}@reaction.invalid"
    user.archived_at = deleted_at
    await session.commit()

    return DeleteAccountResponse(
        status="deleted",
        message="계정을 삭제했어요. 그동안 이용해 주셔서 감사합니다.",
        deleted_at=deleted_at,
    )
