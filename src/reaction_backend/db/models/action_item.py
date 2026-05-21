"""ActionItem — 실행 카드 (S10 Today / S14 Weekly).

ActionItem 은 여러 source 에서 올 수 있다:
- goal (Planning Agent 가 Goal 분해)
- habit (HabitInstance 의 한 회차)
- inbox (S25 Triage 에서 변환)
- manual (사용자 직접 추가)
- recovery (S19 회복 결정 → 새 카드 생성, parent_action_item_id 로 원본 추적)

핵심:
- 원본 카드의 status (FAILED 등) 는 절대 변경 X — Resilience 지표 전제
- soft delete only (archived_at). hard delete 금지.
- system_failure_reason = 'reflection_skipped' → 3일 누적 미회고로 자동 만료
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.scheduled_block import ScheduledBlock
    from reaction_backend.db.models.user import User


ACTION_STATUS_VALUES = (
    "planned",
    "in_progress",
    "done",
    "partial_done",
    "failed",
    "over_done",
    "archived",
)

ACTION_SOURCE_VALUES = ("goal", "habit", "inbox", "manual", "recovery")


class ActionItem(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "action_items"

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

    title: Mapped[str] = mapped_column(String(300), nullable=False)

    target_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    estimated_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
    )

    status: Mapped[str] = mapped_column(
        Enum(*ACTION_STATUS_VALUES, name="action_status"),
        nullable=False,
        server_default="planned",
    )

    source: Mapped[str] = mapped_column(
        Enum(*ACTION_SOURCE_VALUES, name="action_source"),
        nullable=False,
        server_default="manual",
    )

    # source 별 출처 — 모두 nullable. source enum 과 매칭은 application 검증.
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    habit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("habits.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    inbox_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Recovery 에서 새 카드 생성 시 원본 카드 추적 — 혈통(parentage)
    parent_action_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Morning Brief 카드의 reasonWhyNow / firstStep 에 사용
    why_now: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_step: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 1 (가장 높음) ~ 5
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))

    # 'reflection_skipped' (3일 누적 자동 만료) / 'cancelled_by_replan' 등
    system_failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── relationships ──
    user: Mapped[User] = relationship()
    scheduled_blocks: Mapped[list[ScheduledBlock]] = relationship(
        back_populates="action_item", cascade="all, delete-orphan"
    )
