"""Time Policies — 시간 정책 (S07, api-contract §5).

Issue #17 실구현:
- CRUD 실 DB (`time_policies` 테이블)
- `prefill-from-interview` — 기본 후보 (DB 미저장). 이름과 달리 지금은 인터뷰 답을 읽지 않는다
  (`_build_prefill_candidates` 참조)
- 첫 POST 시 onboarding_state 전이: POLICIES → FIRST_PLAN
- soft delete (`archived_at` + `is_active=false`)
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.time_policy import TimePolicy as TimePolicyModel
from reaction_backend.db.session import get_db
from reaction_backend.repositories.time_policy_repo import (
    TimePolicyRepo,
    get_time_policy_repo,
)
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.time_policies import (
    TimePolicy,
    TimePolicyCreateRequest,
    TimePolicyUpdateRequest,
)

router = APIRouter(prefix="/time-policies", tags=["time-policies"])

_ID_PREFIX = "policy_"


def _to_schema(policy: TimePolicyModel) -> TimePolicy:
    return TimePolicy(
        policy_id=f"{_ID_PREFIX}{policy.id}",
        policy_type=policy.policy_type,
        payload=dict(policy.payload),
        is_active=policy.is_active,
    )


def _parse_policy_id(policy_id: str) -> UUID:
    if not policy_id.startswith(_ID_PREFIX):
        raise _not_found()
    try:
        return UUID(policy_id[len(_ID_PREFIX) :])
    except ValueError as e:
        raise _not_found() from e


def _not_found() -> ApiError:
    return ApiError(
        ErrorCode.POLICY_NOT_FOUND,
        "해당 시간 정책을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


RepoDep = Annotated[TimePolicyRepo, Depends(get_time_policy_repo)]
UserRepoDep = Annotated[UserRepo, Depends(get_user_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]

# policy_type 별 payload 에 반드시 있어야 하는 시각 키 — 룰 스케줄러(goal_structuring.
# time_policies_to_busy)가 busy 전개에 쓰는 계약. 여기서 못 막으면 잘못된 정책이 저장돼
# 나중에 계획 생성(POST /plans/generate)이 원인 불명 500 으로 죽는다.
_POLICY_TIME_KEYS: dict[str, tuple[str, ...]] = {
    "sleep": ("start_time", "end_time"),
    "lunch": ("start_time", "end_time"),
    "no_touch": ("start_time", "end_time"),
    "late_night_block": ("start_time",),
}

# payload 는 CamelModel 이 안 건드리는 자유 dict 라, 클라이언트가 camelCase 로 보내면 그대로
# 저장돼 스케줄러(snake_case)와 어긋난다. 경계에서 정규화해 **저장은 항상 snake_case** 로 통일.
_PAYLOAD_KEY_ALIASES: dict[str, str] = {
    "startTime": "start_time",
    "endTime": "end_time",
    "daysOfWeek": "days_of_week",
    "minMinutes": "min_minutes",
}


def _normalize_payload_keys(payload: Mapping[str, Any]) -> dict[str, Any]:
    """payload 의 알려진 camelCase 키를 snake_case 로 정규화(그 외 키는 그대로)."""
    return {_PAYLOAD_KEY_ALIASES.get(k, k): v for k, v in payload.items()}


def _is_hhmm(raw: object) -> bool:
    """'HH:MM' 형식 검증 (스케줄러 _parse_hhmm 과 동일 허용치 — 24:00 = 하루 끝)."""
    try:
        hh_s, mm_s = str(raw).split(":")
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, AttributeError):
        return False
    if hh == 24 and mm == 0:
        return True
    return 0 <= hh <= 23 and 0 <= mm <= 59


def _validate_policy_payload(policy_type: str, payload: Mapping[str, Any]) -> None:
    """policy_type 별 payload 필수 키·형식 검증 (생성/수정 시). 위반이면 422.

    시간창 정책(sleep/lunch/no_touch/late_night_block)은 시각 키가 없거나 형식이 틀리면
    스케줄러가 나중에 죽으므로, 저장 전에 원인 지점에서 명확히 막는다.
    """
    required = _POLICY_TIME_KEYS.get(policy_type, ())
    missing = [k for k in required if k not in payload]
    if missing:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            f"'{policy_type}' 정책에는 {', '.join(missing)} 값이 필요해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="payload",
        )
    for key in required:
        if not _is_hhmm(payload[key]):
            raise ApiError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                f"'{key}' 는 HH:MM 형식이어야 해요 (받은 값: {payload[key]!r}).",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="payload",
            )
    if policy_type == "no_touch":
        dow = payload.get("days_of_week")
        if dow is not None and not isinstance(dow, list):
            raise ApiError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                'days_of_week 는 요일 배열이어야 해요 (예: ["mon", "wed"]).',
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="payload",
            )


# ───── prefill 후보 ─────


def _build_prefill_candidates() -> list[tuple[str, dict[str, Any]]]:
    """(policy_type, payload) 기본 후보 — 수면 23:00–07:00 · 휴식 15분 · 22:00 이후 심야 차단.

    ⚠️ **인터뷰 답을 읽지 않는다.** 예전 구현은 `time.sleep_window`·`time.lunch`·`time.peak_hours`
    슬롯을 찾았지만, 인터뷰 카탈로그에 그런 슬롯은 처음부터 없었다(지금은 `time.activity_window`·
    `time.peak_window`·`time.fixed_blocks`, 레포 이력에도 앞의 세 키를 쓰는 곳이 없다). 그래서
    언제나 이 기본값이 나갔고, 그 사실을 숨긴 채 인터뷰를 조회만 하고 있었다 — 조회와 죽은
    분기를 걷어내 결과는 그대로 두고 코드가 하는 일을 정직하게 만든다.

    인터뷰 활동 시간대로 개인화하려면(예: 활동창이 23:30 까지면 심야 차단 22:00 은 그와 어긋난다)
    후보 규칙 자체를 정하는 제품 결정이 먼저다. 지금 이 endpoint 를 부르는 FE 화면은 없다.
    """
    return [
        ("sleep", {"start_time": "23:00", "end_time": "07:00"}),
        ("break_min", {"min_minutes": 15}),
        ("late_night_block", {"start_time": "22:00", "blocked_categories": []}),
    ]


# ───── endpoints ─────


@router.get("")
async def list_policies(user: CurrentUser, repo: RepoDep) -> list[TimePolicy]:
    """내 활성 시간 정책 전체."""
    items = await repo.list_active(user.id)
    return [_to_schema(p) for p in items]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: TimePolicyCreateRequest,
    user: CurrentUser,
    repo: RepoDep,
    user_repo: UserRepoDep,
    session: SessionDep,
) -> TimePolicy:
    """신규 시간 정책 추가.

    부수 효과: `ONBOARDING_POLICIES` → `ONBOARDING_FIRST_PLAN` 으로 전이 (멱등).
    """
    payload = _normalize_payload_keys(body.payload)
    _validate_policy_payload(body.policy_type, payload)
    policy = await repo.create(user.id, body.policy_type, payload)
    await user_repo.advance_onboarding(
        user,
        expected_from="ONBOARDING_POLICIES",
        to="ONBOARDING_FIRST_PLAN",
    )
    await session.commit()
    await session.refresh(policy)
    return _to_schema(policy)


@router.post("/prefill-from-interview")
async def prefill_from_interview(user: CurrentUser) -> list[TimePolicy]:
    """정책 기본 후보 (DB 미저장) — 이름과 달리 아직 인터뷰 답을 쓰지 않는다
    (`_build_prefill_candidates`). `user` 는 인증만 요구한다.

    응답의 `policyId` 는 prefill 임시 식별자 — FE 는 사용자 선택 후 POST `/time-policies` 로 실제 저장.
    """
    candidates = _build_prefill_candidates()
    return [
        TimePolicy(
            policy_id=f"{_ID_PREFIX}prefill_{i}",
            policy_type=ptype,
            payload=payload,
            is_active=True,
        )
        for i, (ptype, payload) in enumerate(candidates)
    ]


@router.patch("/{policy_id}")
async def update_policy(
    policy_id: str,
    body: TimePolicyUpdateRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> TimePolicy:
    """시간 정책 부분 수정 (`payload`/`is_active`)."""
    policy = await repo.get_by_id(user.id, _parse_policy_id(policy_id))
    if policy is None:
        raise _not_found()
    normalized = _normalize_payload_keys(body.payload) if body.payload is not None else None
    if normalized is not None:
        # 수정은 type 을 안 바꾸므로 기존 policy.policy_type 기준으로 검증.
        _validate_policy_payload(policy.policy_type, normalized)
    updated = await repo.update(
        policy,
        payload=normalized,
        is_active=body.is_active,
    )
    await session.commit()
    await session.refresh(updated)
    return _to_schema(updated)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: str,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> None:
    """시간 정책 soft delete (`archived_at` + `is_active=false`)."""
    policy = await repo.get_by_id(user.id, _parse_policy_id(policy_id))
    if policy is None:
        raise _not_found()
    await repo.soft_delete(policy)
    await session.commit()
    return None
