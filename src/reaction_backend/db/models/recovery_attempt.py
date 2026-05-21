"""RecoveryAttempt — 회복 시도 (S19/S20).

흐름:
- S19 Recovery Coach Agent 가 후보별로 INSERT (user_decision='pending')
- S20 Replan Review 에서 사용자 선택 → user_decision='accepted' (선택 카드) /
  'rejected' (나머지 카드)
- 결과로 새 action_item 생성 시 resulting_action_item_id 에 그 ID 기록 (혈통)

핵심:
- 원본 action_item.status (FAILED 등) 절대 변경 X — Resilience 지표 전제
- llm_fallback_used = true → heuristic fallback 적용된 경우
- recovery_duration_minutes = LLM 응답 latency (v0.6 average_recovery_minutes 원본)
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
    from reaction_backend.db.models.execution_event import ExecutionEvent
    from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog


USER_DECISION_VALUES = ("pending", "accepted", "rejected", "postponed")


class RecoveryAttempt(Base, TimestampMixin):
    __tablename__ = "recovery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    execution_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 9 전략 중 하나 (catalog 참조)
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recovery_strategy_catalog.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # UX 노출용 (catalog.recovery_option_group 복사 — denormalize for read)
    recovery_option_group: Mapped[str] = mapped_column(
        Enum(
            "DOWNSCOPE",
            "RESCHEDULE",
            "CARRY_OVER",
            "PARK",
            name="recovery_option_group",
            create_type=False,  # PR 2-D 의 catalog 와 같은 enum 재사용
        ),
        nullable=False,
    )

    # catalog.if_then_template 에 변수 치환된 최종 텍스트
    suggested_action_text: Mapped[str] = mapped_column(Text, nullable=False)

    user_decision: Mapped[str] = mapped_column(
        Enum(*USER_DECISION_VALUES, name="recovery_user_decision"),
        nullable=False,
        server_default="pending",
    )
    recovery_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # accepted 시 생성된 새 action_item (없는 그룹: RESCHEDULE / PARK)
    resulting_action_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    llm_fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    recovery_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── relationships ──
    execution_event: Mapped[ExecutionEvent] = relationship(back_populates="recovery_attempts")
    strategy: Mapped[RecoveryStrategyCatalog] = relationship(back_populates="attempts")
