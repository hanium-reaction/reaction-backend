"""InterruptionEvent — 일시정지 컨텍스트 (v0.6 신규).

[⏸] 탭 시 INSERT, [▶ 계속] 시 UPDATE.

규칙:
- resumed_after_interrupt 가 NULL 인 채로 created_at < now()-6h 이면 6시간 cron 이 자동 false 처리
  (영원한 NULL 방지 — 앱 끄거나 장시간 방치 시)
- suspended_step 은 Context Re-warming 회복 옵션의 입력으로 사용 (v0.7)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.execution_event import ExecutionEvent


INTERRUPTION_TYPE_VALUES = ("user_pause", "system", "forced")

INTERRUPTION_SOURCE_VALUES = (
    "phone",
    "message",
    "person",
    "self_distraction",
    "fatigue",
    "emergency",
    "other",
)


class InterruptionEvent(Base, TimestampMixin):
    __tablename__ = "interruption_events"

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

    interruption_type: Mapped[str] = mapped_column(
        Enum(*INTERRUPTION_TYPE_VALUES, name="interruption_type"),
        nullable=False,
        server_default="user_pause",
    )

    interruption_source: Mapped[str] = mapped_column(
        Enum(*INTERRUPTION_SOURCE_VALUES, name="interruption_source"),
        nullable=False,
        server_default="other",
    )

    # 중단 시점 진행 내용 (예: "슬라이드 3장 작성 중") — Context Re-warming 입력
    suspended_step: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # [▶ 계속] 누른 시점까지의 지연 (분)
    resume_delay_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # NULL = 아직 결정 안 됨. 6h cron 이 NULL→false 처리.
    resumed_after_interrupt: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # ── relationships ──
    execution_event: Mapped[ExecutionEvent] = relationship(back_populates="interruption_events")
