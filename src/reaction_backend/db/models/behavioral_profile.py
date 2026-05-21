"""BehavioralProfile — 사용자 행동 패턴 (사용자당 1행).

S03 Analysis Confirm 에서 인터뷰 결과로 초기 set, S21 Weekly Review 결과로 점진 갱신.
Planning Agent의 핵심 입력 — 시간 분배 / 카드 길이 / 워크로드 계산에 사용.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, Float, ForeignKey, Integer, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


ENERGY_CYCLE_VALUES = ("morning", "afternoon", "evening", "night", "variable")


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

    # 최대 집중 분 (10/20/30/60/90 등)
    attention_span: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))

    energy_cycle: Mapped[str] = mapped_column(
        Enum(*ENERGY_CYCLE_VALUES, name="behavioral_energy_cycle"),
        nullable=False,
        server_default="variable",
    )

    # 카드 1개의 기본 블록 길이 분 (10/20/30/60/90)
    time_chunk_preference: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
    )

    # Planning Agent 의 여유 비율 (0~1). 0.2 = 20% 버퍼.
    success_buffer: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.2"))

    # ── relationships ──
    user: Mapped[User] = relationship(back_populates="behavioral_profile")
