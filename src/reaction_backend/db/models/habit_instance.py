"""HabitInstance — Habit 의 주별 인스턴스.

매주 월요일 00:00 cron 이 자동 생성. target_count 는 생성 시점의 habits.frequency_per_week 스냅샷
(이후 Habit 빈도가 바뀌어도 이미 생성된 instance 의 목표치는 보존).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.habit import Habit


class HabitInstance(Base, TimestampMixin):
    __tablename__ = "habit_instances"

    __table_args__ = (
        UniqueConstraint("habit_id", "week_start", name="uq_habit_instances_habit_week"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    habit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("habits.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 해당 주의 월요일 날짜 (YYYY-MM-DD)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)

    # 생성 시점의 frequency_per_week 스냅샷
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # 사용자 달성 카운트
    done_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # ── relationships ──
    habit: Mapped[Habit] = relationship(back_populates="instances")
