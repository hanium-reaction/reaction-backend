"""FixedSchedule — 수동 고정 일정 (S05).

캘린더 연결 안 한 사용자가 수업/알바/정기 약속을 직접 입력.
계획 생성 시 "절대 침범하면 안 되는 고정 구역" 역할.

days_of_week JSONB 예: ["mon", "wed", "fri"]
"""

from __future__ import annotations

import uuid
from datetime import time
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Time, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


class FixedSchedule(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "fixed_schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)

    # 요일 배열: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] 부분집합
    days_of_week: Mapped[list[str]] = mapped_column(JSONB, nullable=False)

    start_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    end_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)

    # ── relationships ──
    user: Mapped[User] = relationship(back_populates="fixed_schedules")
