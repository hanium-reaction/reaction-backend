"""ScheduledBlock — ActionItem 의 시간 배치.

ActionItem 1개가 여러 ScheduledBlock 가질 수 있음 (예: 2일에 걸쳐 분할).
Planning Agent (rule-based scheduler) 또는 사용자 직접 편집 (S15) 으로 생성/수정.

v0.7 변경: user_id denormalize (RLS 단순화).
v0.7 block_status: scheduled / started / finished / cancelled — DB 설계서 §5.10
external_calendar_event_id: Google Calendar 이벤트 ID — 이중 쓰기 방지 가드
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.action_item import ActionItem
    from reaction_backend.db.models.user import User


# DB 설계서 v0.7.1 §5.10: scheduled/started/finished/cancelled
BLOCK_STATUS_VALUES = ("scheduled", "started", "finished", "cancelled")

# DB 설계서 v0.7.1 §5.10: ai_plan/user_edit/recovery
BLOCK_SOURCE_VALUES = ("ai_plan", "user_edit", "recovery")


class ScheduledBlock(Base, TimestampMixin):
    __tablename__ = "scheduled_blocks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # denormalize for RLS (v0.7) — DB 설계서 §5.10
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Google Calendar 이벤트 ID — 이중 쓰기 방지 가드 (선택)
    external_calendar_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

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
    user: Mapped[User] = relationship()
    action_item: Mapped[ActionItem] = relationship(back_populates="scheduled_blocks")
