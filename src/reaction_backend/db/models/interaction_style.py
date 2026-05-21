"""InteractionStyle — AI 와의 상호작용 톤/빈도 (사용자당 1행).

S03 Confirm 에서 인터뷰 결과로 초기 set. S22 Habit Penalty / S21 Weekly Review 로 점진 갱신.
LLM 출력 톤 결정 + 알림 빈도 조절에 사용.

DB 설계서 v0.7.1 §5.26:
- suggestion_style: soft/neutral/firm
- recovery_tone: gentle/normal/encouraging
- explanation_depth: brief/normal/detailed (이름 정렬)
- reminder_frequency: minimal/standard/active (이름 정렬)
- plan_change_transparency: summary_only/with_reason/full_diff
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
# DB 설계서 §5.26 정렬: brief/normal/detailed
EXPLANATION_DEPTH_VALUES = ("brief", "normal", "detailed")
# DB 설계서 §5.26 정렬: minimal/standard/active
REMINDER_FREQUENCY_VALUES = ("minimal", "standard", "active")
# DB 설계서 §5.26 신규
PLAN_CHANGE_TRANSPARENCY_VALUES = ("summary_only", "with_reason", "full_diff")


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
        server_default="normal",
    )

    reminder_frequency: Mapped[str] = mapped_column(
        Enum(*REMINDER_FREQUENCY_VALUES, name="interaction_reminder_frequency"),
        nullable=False,
        server_default="standard",
    )

    # 계획 변경 시 보여줄 정보의 투명도 — DB 설계서 §5.26
    plan_change_transparency: Mapped[str] = mapped_column(
        Enum(*PLAN_CHANGE_TRANSPARENCY_VALUES, name="interaction_plan_change_transparency"),
        nullable=False,
        server_default="with_reason",
    )

    # ── relationships ──
    user: Mapped[User] = relationship(back_populates="interaction_style")
