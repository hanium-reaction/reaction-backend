"""InboxItem — Life Inbox (S24). 사용자가 자연어로 캡처한 일/목표/일정.

흐름:
  S24 사용자 캡처 (status=captured)
  → S25 Weekly Triage 분류 (Focus/Maintain/Parked → Goal 변환 시 status=triaged)
  → 또는 archived

규칙: AI 개입 없이 순수 입력만 수집 (PRD 원칙: 입력 부담 최소화).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


INBOX_CATEGORY_VALUES = (
    "study",
    "project",
    "health",
    "routine",
    "schedule",
    "other",
)

INBOX_STATUS_VALUES = ("captured", "triaged", "archived")


class InboxItem(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "inbox_items"

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

    text: Mapped[str] = mapped_column(Text, nullable=False)

    category: Mapped[str] = mapped_column(
        Enum(*INBOX_CATEGORY_VALUES, name="inbox_category"),
        nullable=False,
        server_default="other",
    )

    status: Mapped[str] = mapped_column(
        Enum(*INBOX_STATUS_VALUES, name="inbox_status"),
        nullable=False,
        server_default="captured",
    )

    # ── relationships ──
    user: Mapped[User] = relationship()
