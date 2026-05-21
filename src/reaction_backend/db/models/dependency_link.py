"""DependencyLink — ActionItem 사이의 "A 끝나야 B 시작" 관계.

Goal Structuring Agent (Planning Agent LLM Call ③) 가 생성.
Scheduler Agent 가 시간 배치 시 의존성 보장.

DB 설계서 v0.7.1 §5.11:
- action_item_id (대상) / depends_on_action_item_id (선행)
- dependency_type: must_finish / should_finish / soft
- user_id denormalize (v0.7)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum, ForeignKey, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    pass


DEPENDENCY_TYPE_VALUES = ("must_finish", "should_finish", "soft")


class DependencyLink(Base, TimestampMixin):
    __tablename__ = "dependency_links"

    __table_args__ = (
        UniqueConstraint(
            "action_item_id",
            "depends_on_action_item_id",
            name="uq_dependency_links_pair",
        ),
        CheckConstraint(
            "action_item_id <> depends_on_action_item_id",
            name="ck_dependency_links_no_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # denormalize for RLS (v0.7) — DB 설계서 §5.11
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 대상 ActionItem — DB 설계서 명세 이름
    action_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 선행 ActionItem
    depends_on_action_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # must_finish (hard): 선행 완료 필수
    # should_finish: 선행 완료 권장 (soft constraint)
    # soft: 단순 힌트 (Scheduler 가 자유롭게 무시 가능)
    dependency_type: Mapped[str] = mapped_column(
        Enum(*DEPENDENCY_TYPE_VALUES, name="dependency_type"),
        nullable=False,
        server_default="should_finish",
    )
