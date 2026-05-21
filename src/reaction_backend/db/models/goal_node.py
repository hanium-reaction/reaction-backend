"""GoalNode — 목표 만다라트 분해 트리.

self-FK 로 parent → children 관계. depth 0 = root (Goal 자체와 매칭).
Goal Structuring Orchestrator → Planning Agent (LLM Call ②) 가 생성.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.goal import Goal


class GoalNode(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "goal_nodes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # self-FK. NULL = root node (depth 0).
    parent_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goal_nodes.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)

    # depth 0 = root, 1 = phase, 2 = milestone (대략)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # ── relationships ──
    goal: Mapped[Goal] = relationship(back_populates="nodes")
    parent: Mapped[GoalNode | None] = relationship(
        remote_side="GoalNode.id",
        back_populates="children",
    )
    children: Mapped[list[GoalNode]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )
