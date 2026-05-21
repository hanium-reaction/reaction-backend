"""Goal — 사용자 목표 (S26). Focus / Maintain / Parked 3 tier.

규칙 (잠금):
- Focus 최대 3개
- Maintain 최대 5개
- Parked 자유 (보류한 목표)
- soft delete only (archived_at)

목표 분해는 `goal_nodes` (만다라트 트리). Planning Agent 가 사용.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, ForeignKey, Integer, String, Text, text  # noqa: F401
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.goal_node import GoalNode
    from reaction_backend.db.models.user import User


GOAL_TIER_VALUES = ("focus", "maintain", "parked")

GOAL_STATUS_VALUES = ("active", "archived", "completed")

GOAL_CATEGORY_VALUES = (
    "study",
    "project",
    "health",
    "routine",
    "schedule",
    "career",
    "relationship",
    "self_dev",
    "other",
)


class Goal(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "goals"

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
        Enum(*GOAL_CATEGORY_VALUES, name="goal_category"),
        nullable=False,
        server_default="other",
    )

    goal_tier: Mapped[str] = mapped_column(
        Enum(*GOAL_TIER_VALUES, name="goal_tier"),
        nullable=False,
        server_default="maintain",
    )

    # Planning Agent 의 horizon 계산에 사용 (가장 먼 focus deadline)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)

    # 1 (가장 높음) ~ 5 (가장 낮음) — DB 설계서 v0.7.1 §5.5
    priority_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))

    # 총 예상 소요 분 — DB 설계서 v0.7.1 §5.5
    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 이번 주 분류 라벨 (예: '2026-W21-focus') — Planning Agent 핵심, DB 설계서 v0.7.1 §5.5
    week_tier_key: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Goal 라이프사이클 — DB 설계서 v0.7.1 §5.5
    status: Mapped[str] = mapped_column(
        Enum(*GOAL_STATUS_VALUES, name="goal_status"),
        nullable=False,
        server_default="active",
    )

    # ── 우리 개선 (ADR §4 보존) ──
    # "왜 지금" 이유 — Morning Brief 카드의 reasonWhyNow 에 사용
    why_now: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 첫 동작 한 줄 — S11 Action Detail 의 first_step prefill
    first_step: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── relationships ──
    user: Mapped[User] = relationship()
    nodes: Mapped[list[GoalNode]] = relationship(
        back_populates="goal", cascade="all, delete-orphan"
    )
