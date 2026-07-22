"""Settings 도메인 스키마 (api-contract §16) — S23.

#23-A 범위: S23 Settings (tone / language / timezone + 알림 요약).
S28 Privacy(consent·anonymize) 스키마는 #23-B 에서 추가한다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime

# User.TONE_MODE_VALUES 와 동일 — gentle/strict/encouraging.
ToneMode = Literal["gentle", "strict", "encouraging"]

# ── 프로필 메모리 편집 enum (모델 enum 과 동일) ──
EnergyCycle = Literal["morning", "afternoon", "evening", "night", "varies"]
TimeChunk = Literal["10", "20", "30", "60", "90"]
RecoveryTone = Literal["gentle", "normal", "encouraging"]
ReminderFrequency = Literal["minimal", "standard", "active"]
ExplanationDepth = Literal["brief", "normal", "detailed"]
SuggestionStyle = Literal["soft", "neutral", "firm"]

# user_consents.CONSENT_TYPE_VALUES 와 동일.
ConsentType = Literal["required", "marketing", "research"]


class NotificationSummary(CamelModel):
    """GET /settings 의 알림 설정 요약 (읽기 전용).

    상세 설정·수정은 §15 `/notifications/settings`. 본 요약은 설정 화면 개요용.
    """

    morning_brief_time: str
    evening_reflection_time: str
    pre_card_enabled: bool


class SettingsResponse(CamelModel):
    """GET /settings · PATCH /settings/tone-mode 응답.

    - `language` 는 MVP 잠금(한국어 only, DevBaseline §1.4) → 항상 `"ko"`.
    - `tone_mode` 는 신규 user(인터뷰 전)에서 null 가능.
    - `notifications` 는 아직 설정 행이 없으면 null (GET 은 행을 생성하지 않는다).
    """

    tone_mode: ToneMode | None
    language: str = "ko"
    timezone: str
    notifications: NotificationSummary | None


class ToneModeUpdateRequest(CamelModel):
    """PATCH /settings/tone-mode 요청.

    `tone_mode` 외 값은 Pydantic Literal 검증 → 422 `COMMON_VALIDATION_ERROR`.
    """

    tone_mode: ToneMode


# ── 프로필 메모리 (Policy Snapshot 레이어) — 조회/편집 (#A-1·A-2) ──


class BehavioralProfileView(CamelModel):
    """behavioral_profiles 의 사용자 편집 대상 필드."""

    energy_cycle: EnergyCycle
    attention_span: int
    time_chunk_preference: TimeChunk
    preferred_start_time: str | None = None  # "HH:MM"
    preferred_end_time: str | None = None


class InteractionStyleView(CamelModel):
    """interaction_styles 의 사용자 편집 대상 필드."""

    recovery_tone: RecoveryTone
    suggestion_style: SuggestionStyle
    explanation_depth: ExplanationDepth
    reminder_frequency: ReminderFrequency


class ProfileResponse(CamelModel):
    """GET/PATCH /settings/profile — 지속형 프로필 메모리.

    인터뷰가 아직 안 채웠으면 각 항목 null (행 없음).
    `downscopeUnitMin`/`restOk` 는 회복 선호 — `users.focus_mode_preferences`(JSONB) 출처.
    """

    behavioral: BehavioralProfileView | None
    interaction: InteractionStyleView | None
    downscope_unit_min: int | None = None  # 회복 시 이 분(min) 단위까지 줄이면 해볼 만함
    rest_ok: bool | None = None  # 회복 시 휴식 제안 수용 여부
    # 계획을 잡아도 되는 활동 시간대 "HH:MM"(자정=24:00) — 편집 시 users.focus_mode_preferences.
    activity_start: str | None = None
    activity_end: str | None = None


class ProfileUpdateRequest(CamelModel):
    """PATCH /settings/profile — 지정 필드만 부분 갱신. 미지정(None)은 유지.

    enum 외 값은 Pydantic Literal → 422 `COMMON_VALIDATION_ERROR`.
    """

    energy_cycle: EnergyCycle | None = None
    attention_span: int | None = Field(default=None, ge=5, le=240)
    time_chunk_preference: TimeChunk | None = None
    recovery_tone: RecoveryTone | None = None
    suggestion_style: SuggestionStyle | None = None
    explanation_depth: ExplanationDepth | None = None
    reminder_frequency: ReminderFrequency | None = None
    # 회복 선호 (users.focus_mode_preferences JSONB) — 재인터뷰 없이 편집.
    downscope_unit_min: int | None = Field(default=None, ge=1, le=120)
    rest_ok: bool | None = None
    # 계획을 잡아도 되는 활동 시간대 "HH:MM"(자정=24:00). 계획 생성이 이 시간대만 사용.
    activity_start: str | None = None
    activity_end: str | None = None


# ── S28 Privacy — Consent (#23-B) ──


class ConsentItem(CamelModel):
    """consent_type 별 현재(최신) 동의 상태."""

    consent_type: ConsentType
    is_granted: bool
    updated_at: KstDatetime


class ConsentListResponse(CamelModel):
    """GET /privacy/consent — 동의 현황 (필수/마케팅/연구 분리)."""

    consents: list[ConsentItem]


class ConsentCreateRequest(CamelModel):
    """POST /privacy/consent — 동의/철회 (append-only 새 기록)."""

    consent_type: ConsentType
    granted: bool


# ── S28 Privacy — Anonymize (#23-B) ──


class AnonymizeRequest(CamelModel):
    """POST /settings/anonymize 요청.

    `confirmationToken` 없으면 step1(토큰 발급), 있으면 step2(실행) — 2단계 확인.
    """

    confirmation_token: str | None = None


class AnonymizeResponse(CamelModel):
    """익명화 응답. `status` 로 단계 구분.

    - `confirmation_required` — 토큰 발급(미적용). `confirmationToken`/`expiresAt` 채움.
    - `anonymized` — 적용 완료. `anonymizedAt`/`maskedCount` 채움.
    """

    status: Literal["confirmation_required", "anonymized"]
    message: str
    confirmation_token: str | None = None
    expires_at: KstDatetime | None = None
    anonymized_at: KstDatetime | None = None
    masked_count: int | None = None
