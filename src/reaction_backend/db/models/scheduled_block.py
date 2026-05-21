"""ScheduledBlock — ActionItem 의 시간 배치.

ActionItem 1개가 여러 ScheduledBlock 가질 수 있음 (예: 2일에 걸쳐 분할).
Planning Agent (rule-based scheduler) 또는 사용자 직접 편집 (S15) 으로 생성/수정.

블록 status:
- scheduled: 배치만 됨 (아직 시작 안 함)
- completed: 종료됨 (action_items.status 와 별도)
- cancelled: Replan 등으로 취소됨
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.action_item import ActionItem


BLOCK_STATUS_VALUES = ("scheduled", "completed", "cancelled")

BLOCK_SOURCE_VALUES = ("ai_plan", "user_edit", "recovery", "manual")


class ScheduledBlock(Base, TimestampMixin):
    __tablename__ = "scheduled_blocks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    action_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    block_status: Mapped[str] = mapped_column(
        Enum(*BLOCK_STATUS_VALUES, name="block_status"),
        nullable=False,
        server_default="scheduled",
    )

    source: Mapped[str] = mapped_column(
        Enum(*BLOCK_SOURCE_VALUES, name="block_source"),
        nullable=False,
        server_default="ai_plan",
    )

    # ── relationships ──
    action_item: Mapped[ActionItem] = relationship(back_populates="scheduled_blocks")
