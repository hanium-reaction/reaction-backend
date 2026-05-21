"""InterviewSession — 딥 인터뷰 S02 세션 메타.

핵심:
- 사용자당 진행 중 세션은 1개 (애플리케이션 로직에서 enforce)
- end_reason = completed / turn_limit / early_user
- total_turns 와 ambiguity_final 은 분석/디버깅용
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
    from reaction_backend.db.models.user import User


# DB 설계서 §5.2: 4종 (abandoned 추가)
INTERVIEW_END_REASON_VALUES = ("completed", "turn_limit", "early_user", "abandoned")


class InterviewSession(Base, TimestampMixin):
    __tablename__ = "interview_sessions"

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

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(
        Enum(*INTERVIEW_END_REASON_VALUES, name="interview_end_reason"),
        nullable=True,
    )

    llm_model: Mapped[str] = mapped_column(String(64), nullable=False)
    total_turns: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # 종료 시점 모호함 지표 (0~1) — DB 설계서 §5.2: NUMERIC(4,3)
    ambiguity_final: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    # ── relationships ──
    user: Mapped[User] = relationship(back_populates="interview_sessions")
    slot_answers: Mapped[list[InterviewSlotAnswer]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
