"""BehavioralProfile — 사용자 행동 패턴 (사용자당 1행).

S03 Analysis Confirm 에서 인터뷰 결과로 초기 set, S21 Weekly Review 결과로 점진 갱신.
Planning Agent의 핵심 입력 — 시간 분배 / 카드 길이 / 워크로드 계산에 사용.

DB 설계서 v0.7.1 §5.25:
- time_chunk_preference: VARCHAR(20) "10/20/30/60/90 분"
- energy_cycle: morning/afternoon/evening/night/varies
- success_buffer: NUMERIC(4,3) (1.0~1.5)
- preferred_start_time / preferred_end_time
- context_switching_cost
- recovery_speed_type
"""

from __future__ import annotations

import uuid
from datetime import time
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Integer, Numeric, String, Time, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


# DB 설계서 §5.25 정렬: varies (variable → varies)
ENERGY_CYCLE_VALUES = ("morning", "afternoon", "evening", "night", "varies")

# DB 설계서 §5.25 신규
RECOVERY_SPEED_TYPE_VALUES = ("fast", "medium", "slow")


class BehavioralProfile(Base, TimestampMixin):
    __tablename__ = "behavioral_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    # 카드 1개의 기본 블록 길이 — DB 설계서 §5.25 VARCHAR "10/20/30/60/90"
    time_chunk_preference: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="30"
    )

    energy_cycle: Mapped[str] = mapped_column(
        Enum(*ENERGY_CYCLE_VALUES, name="behavioral_energy_cycle"),
        nullable=False,
        server_default="varies",
    )

    # 여유 비율 (1.0~1.5) — DB 설계서 §5.25 NUMERIC(4,3)
    success_buffer: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    # 선호 시작 시각 — DB 설계서 §5.25
    preferred_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # 선호 종료 시각 — DB 설계서 §5.25
    preferred_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    # 최대 집중 분 (NN)
    attention_span: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))

    # 맥락 전환 비용 분 — DB 설계서 §5.25
    context_switching_cost: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 회복 속도 분류 — DB 설계서 §5.25
    recovery_speed_type: Mapped[str | None] = mapped_column(
        Enum(*RECOVERY_SPEED_TYPE_VALUES, name="behavioral_recovery_speed"),
        nullable=True,
    )

    # ── relationships ──
    user: Mapped[User] = relationship(back_populates="behavioral_profile")
