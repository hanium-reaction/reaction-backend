"""NotificationSetting — 알림 설정 (사용자당 1행). S08.

잠금 규칙:
- morning_brief_time: 06:00~10:00 만 허용 (애플리케이션 레이어 검증)
- evening_reflection_time: 19:00~23:00 만 허용
- 야간 23~07시 자동 푸시 금지 (notification dispatcher 가 enforce)
- 같은 클래스 24h 내 중복 발송 금지
- 주 ≤ 3건 enforce
- push_subscription 은 Web Push 표준 객체 {endpoint, keys: {p256dh, auth}}
"""

from __future__ import annotations

import uuid
from datetime import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Time, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


class NotificationSetting(Base, TimestampMixin):
    __tablename__ = "notification_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    morning_brief_time: Mapped[time] = mapped_column(
        Time(timezone=False), nullable=False, server_default=text("'08:00'")
    )
    evening_reflection_time: Mapped[time] = mapped_column(
        Time(timezone=False), nullable=False, server_default=text("'21:00'")
    )
    pre_card_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # Web Push 표준 객체. 권한 거부 사용자는 NULL → 인앱 알림 큐만.
    push_subscription: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ── relationships ──
    user: Mapped[User] = relationship(back_populates="notification_setting")
