"""ExecutionEvent — Quick Check-in 4칩 결과 + 실행 메타 (S13 Focus / S17 Reflection).

흐름 (DB 시나리오 분석):
- [▶ 시작] → execution_events INSERT, completion_status='in_progress'
- [⏸] → interruption_events INSERT (별도 모델)
- 종료 시 → actual_end_at, actual_duration_minutes, pause_total_minutes 갱신
- Quick Check-in → completion_status (done/partial_done/failed/over_done) + context_snapshots INSERT

규칙:
- completion_status NN default 'in_progress' (v0.7) — 시작 시점부터 의미있는 lifecycle
- user_feedback 은 PII 가능 → `_encrypted` 컬럼명 (실제 암호화는 후속 PR)
- user_id 는 denormalize (FK to users.id) — RLS / 직접 조회 편의
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.action_item import ActionItem
    from reaction_backend.db.models.context_snapshot import ContextSnapshot
    from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
    from reaction_backend.db.models.interruption_event import InterruptionEvent
    from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
    from reaction_backend.db.models.user import User


EXECUTION_COMPLETION_STATUS_VALUES = (
    "in_progress",
    "done",
    "partial_done",
    "failed",
    "over_done",
)


class ExecutionEvent(Base, TimestampMixin):
    __tablename__ = "execution_events"

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

    # denormalize for RLS / 직접 조회 편의
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    actual_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    actual_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pause_total_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    delay_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    completion_status: Mapped[str] = mapped_column(
        Enum(*EXECUTION_COMPLETION_STATUS_VALUES, name="execution_completion_status"),
        nullable=False,
        server_default="in_progress",
    )

    # 1 (낮음) ~ 5 (높음)
    user_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 자유 입력 (encrypted at-rest)
    user_feedback_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    focus_mode_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    dnd_used: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # ── relationships ──
    action_item: Mapped[ActionItem] = relationship()
    user: Mapped[User] = relationship()
    interruption_events: Mapped[list[InterruptionEvent]] = relationship(
        back_populates="execution_event", cascade="all, delete-orphan"
    )
    context_snapshots: Mapped[list[ContextSnapshot]] = relationship(
        back_populates="execution_event", cascade="all, delete-orphan"
    )
    failure_tags: Mapped[list[ExecutionFailureTag]] = relationship(
        back_populates="execution_event", cascade="all, delete-orphan"
    )
    recovery_attempts: Mapped[list[RecoveryAttempt]] = relationship(
        back_populates="execution_event", cascade="all, delete-orphan"
    )
