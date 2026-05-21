"""InteractionStyle — AI 와의 상호작용 톤/빈도 (사용자당 1행).

S03 Confirm 에서 인터뷰 결과로 초기 set. S22 Habit Penalty / S21 Weekly Review 로 점진 갱신.
LLM 출력 톤 결정 + 알림 빈도 조절에 사용.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


SUGGESTION_STYLE_VALUES = ("soft", "neutral", "firm")
RECOVERY_TONE_VALUES = ("gentle", "normal", "encouraging")
EXPLANATION_DEPTH_VALUES = ("short", "medium", "detailed")
REMINDER_FREQUENCY_VALUES = ("low", "medium", "high")


class InteractionStyle(Base, TimestampMixin):
    __tablename__ = "interaction_styles"

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

    suggestion_style: Mapped[str] = mapped_column(
        Enum(*SUGGESTION_STYLE_VALUES, name="interaction_suggestion_style"),
        nullable=False,
        server_default="neutral",
    )

    recovery_tone: Mapped[str] = mapped_column(
        Enum(*RECOVERY_TONE_VALUES, name="interaction_recovery_tone"),
        nullable=False,
        server_default="normal",
    )

    explanation_depth: Mapped[str] = mapped_column(
        Enum(*EXPLANATION_DEPTH_VALUES, name="interaction_explanation_depth"),
        nullable=False,
        server_default="medium",
    )

    reminder_frequency: Mapped[str] = mapped_column(
        Enum(*REMINDER_FREQUENCY_VALUES, name="interaction_reminder_frequency"),
        nullable=False,
        server_default="medium",
    )

    # ── relationships ──
    user: Mapped[User] = relationship(back_populates="interaction_style")
