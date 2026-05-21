"""DependencyLink — ActionItem 사이의 "A 끝나야 B 시작" 관계.

Goal Structuring Agent (Planning Agent LLM Call ③) 가 생성.
Scheduler Agent 가 시간 배치 시 의존성 보장.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    pass


class DependencyLink(Base, TimestampMixin):
    __tablename__ = "dependency_links"

    __table_args__ = (
        UniqueConstraint(
            "predecessor_action_item_id",
            "successor_action_item_id",
            name="uq_dependency_links_pair",
        ),
        CheckConstraint(
            "predecessor_action_item_id <> successor_action_item_id",
            name="ck_dependency_links_no_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    predecessor_action_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    successor_action_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
