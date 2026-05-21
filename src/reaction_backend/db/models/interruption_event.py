"""InterruptionEvent — 일시정지 컨텍스트 (v0.6 신규).

[⏸] 탭 시 INSERT, [▶ 계속] 시 UPDATE.

DB 설계서 v0.7.1 §5.15:
- user_id denormalize (v0.7)
- execution_id (이름 정렬)
- interruption_type: user_pause/external_alert/timer_break/forced_stop
- interruption_source: phone/message/person/self_distraction/notification/emergency
- interrupt_context_note_encrypted: 자유 텍스트 (at-rest 암호화)

규칙:
- resumed_after_interrupt 가 NULL 인 채로 created_at < now()-6h 이면 6시간 cron 이 자동 false 처리
- suspended_step 은 Context Re-warming 회복 옵션의 입력
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.execution_event import ExecutionEvent
    from reaction_backend.db.models.user import User


# DB 설계서 §5.15 명세 정렬
INTERRUPTION_TYPE_VALUES = ("user_pause", "external_alert", "timer_break", "forced_stop")

INTERRUPTION_SOURCE_VALUES = (
    "phone",
    "message",
    "person",
    "self_distraction",
    "notification",
    "emergency",
)


class InterruptionEvent(Base, TimestampMixin):
    __tablename__ = "interruption_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # denormalize for RLS (v0.7) — DB 설계서 §5.15
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # DB 설계서 컬럼명 정렬: execution_id
    execution_id: Mapped[uuid.UUID] = mapped_column(
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

    interruption_source: Mapped[str | None] = mapped_column(
        Enum(*INTERRUPTION_SOURCE_VALUES, name="interruption_source"),
        nullable=True,
    )

    # 중단 시점 진행 내용 (예: "슬라이드 3장 작성 중") — Context Re-warming 입력
    suspended_step: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # [▶ 계속] 누른 시점까지의 지연 (분)
    resume_delay_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # NULL = 아직 결정 안 됨. 6h cron 이 NULL→false 처리.
    resumed_after_interrupt: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # 중단 당시 상황 메모 (at-rest 암호화, 익명화 대상) — DB 설계서 §5.15
    interrupt_context_note_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── relationships ──
    user: Mapped[User] = relationship()
    execution_event: Mapped[ExecutionEvent] = relationship(back_populates="interruption_events")
