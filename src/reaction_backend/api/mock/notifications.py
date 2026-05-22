"""Notifications mock fixture — #3-C 스텁용 (S08).

데모 알림 설정. 실제 Web Push 발송·스케줄러는 후속.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DemoNotificationSettings:
    """알림 설정 (api-contract §15). 사용자당 1건."""

    morning_brief_time: str  # HH:MM, 06~10시
    evening_reflection_time: str  # HH:MM, 19~23시
    pre_card_enabled: bool
    push_subscribed: bool


DEMO_NOTIFICATION_SETTINGS = DemoNotificationSettings(
    morning_brief_time="08:00",
    evening_reflection_time="21:00",
    pre_card_enabled=False,
    push_subscribed=False,
)
