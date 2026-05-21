"""ContextSnapshot — 환경 14필드 캡처 (v0.6).

Quick Check-in 완료 시 자동 INSERT. S21 Weekly Review 의 Peak/Drain/Location 인사이트의 원본.

규칙:
- overwhelm_level >= 4 → S19 에서 PARK 회복 옵션 후보
- location_type='home' + focus_level 낮음 → Environment Shift 회복 옵션 후보
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.execution_event import ExecutionEvent


TIME_OF_DAY_VALUES = ("early_morning", "morning", "afternoon", "evening", "night")

LOCATION_TYPE_VALUES = ("home", "cafe", "library", "school", "work", "transit", "other")

DEVICE_TYPE_VALUES = ("mobile", "tablet", "desktop")


class ContextSnapshot(Base, TimestampMixin):
    __tablename__ = "context_snapshots"

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

    # ── when ── (시간/요일)
    time_of_day: Mapped[str] = mapped_column(
        Enum(*TIME_OF_DAY_VALUES, name="context_time_of_day"),
        nullable=False,
    )
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 0=월 .. 6=일

    calendar_density: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_event_gap_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── state ── (1~5 척도)
    estimated_energy_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    focus_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    overwhelm_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    noise_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    # ── environment ──
    interruption_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    location_type: Mapped[str | None] = mapped_column(
        Enum(*LOCATION_TYPE_VALUES, name="context_location_type"),
        nullable=True,
    )
    device_type: Mapped[str | None] = mapped_column(
        Enum(*DEVICE_TYPE_VALUES, name="context_device_type"),
        nullable=True,
    )

    weather: Mapped[str | None] = mapped_column(String(64), nullable=True)
    companion_present: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # ── relationships ──
    execution_event: Mapped[ExecutionEvent] = relationship(back_populates="context_snapshots")
