"""Settings 도메인 스키마 (api-contract §16) — S23.

#23-A 범위: S23 Settings (tone / language / timezone + 알림 요약).
S28 Privacy(consent·anonymize) 스키마는 #23-B 에서 추가한다.
"""

from __future__ import annotations

from typing import Literal

from reaction_backend.schemas.common import CamelModel

# User.TONE_MODE_VALUES 와 동일 — gentle/strict/encouraging.
ToneMode = Literal["gentle", "strict", "encouraging"]


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
