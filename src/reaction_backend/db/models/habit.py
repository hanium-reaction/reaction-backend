"""Habit — 반복 행동 (S27).

규칙:
- frequency_per_week (1~7) — 주간 목표 빈도
- 3주 연속 미달 시 빈도 재설계 제안 (S22 Habit Penalty)
- 페널티 결정 거절 시 4주 cooldown — last_penalty_evaluated_at + last_penalty_decision
- 주별 진행은 habit_instances 가 추적 (매주 월요일 cron 자동 생성)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
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

HABIT_TIME_PREFERENCE_VALUES = ("morning", "afternoon", "evening", "anytime")

HABIT_PENALTY_DECISION_VALUES = ("accepted", "rejected")


class Habit(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "habits"

    __table_args__ = (
        CheckConstraint("frequency_per_week BETWEEN 1 AND 7", name="ck_habit_frequency_range"),
        # 아래 인덱스는 마이그레이션 `9e9b9bd270af` 가 실제로 만든다 — 여기 선언은
        # `alembic check`(모델 ↔ 마이그레이션 drift 검사)가 "모델에 없는 인덱스"로 오인해
        # 제거 대상으로 잡는 걸 막기 위한 것(goal_node.py 의 같은 패턴 참고). `postgresql_where`
        # 문자열은 그 마이그레이션 파일과 글자 단위로 맞춰야 한다.
        Index("ix_habits_goal_node_id", "goal_node_id"),
        Index(
            "uq_habits_goal_node_id_active",
            "goal_node_id",
            unique=True,
            postgresql_where=text("goal_node_id IS NOT NULL AND archived_at IS NULL"),
        ),
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

    # 주간 목표 빈도 (1~7)
    frequency_per_week: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )

    # 이번 주 목표 횟수 (frequency_per_week 동기화) — DB 설계서 v0.7.1 §5.7
    target_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))

    # 1회 소요 분 — DB 설계서 v0.7.1 §5.7
    minutes_per_session: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
    )

    # 시간대 선호 — DB 설계서 v0.7.1 §5.7
    time_preference: Mapped[str] = mapped_column(
        Enum(*HABIT_TIME_PREFERENCE_VALUES, name="habit_time_preference"),
        nullable=False,
        server_default="anytime",
    )

    # 1 (최우선) ~ 5 — DB 설계서 v0.7.1 §5.7
    priority_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))

    # 연속 미달 주 수 (페널티 트리거 — 3 이상이면 S22 Habit Penalty 큐)
    consecutive_miss_weeks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # 마지막 페널티 평가 시각 — cooldown 산정용
    last_penalty_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 마지막 페널티 결정 (rejected 시 4주 cooldown)
    last_penalty_decision: Mapped[str | None] = mapped_column(
        Enum(*HABIT_PENALTY_DECISION_VALUES, name="habit_penalty_decision"),
        nullable=True,
    )

    # 만다라 반복형 칸 링크(ADR-0008 §1) — 있으면 그 칸은 계획을 만들지 않고 이 습관의
    # 주간 횟수(habit_instances.done_count)로만 진척을 본다. 칸이 삭제돼도 습관 기록은
    # 남아야 하므로 SET NULL(hard delete 금지, AGENTS §2). 칸 하나당 활성 습관 하나는
    # 마이그레이션의 부분 유니크 인덱스(`uq_habits_goal_node_id_active`)가 강제한다.
    goal_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goal_nodes.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── relationships ──
    user: Mapped[User] = relationship()
    instances: Mapped[list[HabitInstance]] = relationship(
        back_populates="habit", cascade="all, delete-orphan"
    )
