"""Notifications 도메인 스키마 (api-contract §15) — S08."""

from __future__ import annotations

from pydantic import Field, field_validator

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


class VapidPublicKeyResponse(CamelModel):
    """GET /notifications/vapid-public-key 응답 — FE `applicationServerKey` 용 공개값.

    `publicKey` 가 null 이면 서버에 VAPID 미설정 → FE 는 **구독을 만들지 말고** '알림 미지원'
    을 표시해야 한다. 서버가 발송할 수 없는데 구독만 만들면, 브라우저 subscribe 는 성공해도
    발송 시 push 서비스가 키 불일치로 403 을 던져 도달 못 하는 구독이 조용히 쌓인다.
    """

    public_key: str | None


class PushSubscribeRequest(CamelModel):
    """POST /notifications/subscribe 요청 — Web Push subscription 객체.

    브라우저 `PushSubscription.toJSON()` 의 `{endpoint, keys: {p256dh, auth}}`.
    p256dh/auth 는 pywebpush 발송(payload 암호화)에 필수라 스키마에서 강제한다 —
    빠진 채 저장되면 **발송 시점**에야 터져서, 구독은 됐다고 믿는 사용자가 조용히
    알림을 못 받는다.
    """

    endpoint: str = Field(min_length=1)
    keys: dict[str, str]

    @field_validator("keys")
    @classmethod
    def _require_webpush_keys(cls, v: dict[str, str]) -> dict[str, str]:
        missing = [k for k in ("p256dh", "auth") if not v.get(k)]
        if missing:
            raise ValueError(f"keys 에 {', '.join(missing)} 가 필요해요 (Web Push 표준 구독 객체)")
        return v
