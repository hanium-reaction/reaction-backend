"""Habit — 반복 행동 (S27).

규칙:
- frequency_per_week (1~7) — 주간 목표 빈도
- 3주 연속 미달 시 빈도 재설계 제안 (S22 Habit Penalty)
- 주별 진행은 habit_instances 가 추적 (매주 월요일 cron 자동 생성)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.habit_instance import HabitInstance
    from reaction_backend.db.models.user import User


HABIT_CATEGORY_VALUES = (
    "study",
    "health",
    "routine",
    "self_dev",
    "relationship",
    "other",
)


class Habit(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "habits"

    __table_args__ = (
        CheckConstraint("frequency_per_week BETWEEN 1 AND 7", name="ck_habit_frequency_range"),
    )

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

    category: Mapped[str] = mapped_column(
        Enum(*HABIT_CATEGORY_VALUES, name="habit_category"),
        nullable=False,
        server_default="other",
    )

    frequency_per_week: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )

    # ── relationships ──
    user: Mapped[User] = relationship()
    instances: Mapped[list[HabitInstance]] = relationship(
        back_populates="habit", cascade="all, delete-orphan"
    )
