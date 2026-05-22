"""Notifications 도메인 스키마 (api-contract §15) — S08."""

from __future__ import annotations

from pydantic import Field

from reaction_backend.schemas.common import CamelModel

# HH:MM 24시간 형식
_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class NotificationSettings(CamelModel):
    """알림 설정 — GET/PATCH /notifications/settings 응답. 사용자당 1건."""

    morning_brief_time: str
    evening_reflection_time: str
    pre_card_enabled: bool
    push_subscribed: bool


class NotificationSettingsUpdateRequest(CamelModel):
    """PATCH /notifications/settings 요청 — 부분 수정.

    시간은 HH:MM 형식 검증만 스키마에서. 06~10·19~23 범위는 라우터에서 검사.
    """

    morning_brief_time: str | None = Field(default=None, pattern=_HHMM)
    evening_reflection_time: str | None = Field(default=None, pattern=_HHMM)
    pre_card_enabled: bool | None = None


class PushSubscribeRequest(CamelModel):
    """POST /notifications/subscribe 요청 — Web Push subscription 객체."""

    endpoint: str = Field(min_length=1)
    keys: dict[str, str]
