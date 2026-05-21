"""InboxItem — Life Inbox (S24). 사용자가 자연어로 캡처한 일/목표/일정.

DB 설계서 v0.7.1 §5.4:
- raw_text_encrypted TEXT NN (at-rest 암호화)
- ai_category_guess VARCHAR(30) — AI 카테고리 추정
- user_category VARCHAR(30) — 사용자 override 카테고리
- status: captured/classified/archived/promoted
- promoted_goal_id UUID → goals.id (S25 Triage 에서 Goal 변환 시 연결)

흐름:
  S24 사용자 캡처 (status=captured)
  → AI 백그라운드 카테고리 추정 (ai_category_guess set, status=classified)
  → S25 Weekly Triage: 사용자가 user_category override + Goal 변환 시 (status=promoted, promoted_goal_id set)
  → 또는 archived
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.goal import Goal
    from reaction_backend.db.models.user import User


INBOX_CATEGORY_VALUES = (
    "study",
    "project",
    "health",
    "routine",
    "schedule",
    "other",
)

# DB 설계서 §5.4: captured/classified/archived/promoted
INBOX_STATUS_VALUES = ("captured", "classified", "archived", "promoted")


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

    # 원문 — at-rest 암호화 (DB 설계서 §5.4, 컬럼명 정렬)
    raw_text_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    # AI 추정 카테고리 — DB 설계서 §5.4
    ai_category_guess: Mapped[str | None] = mapped_column(
        Enum(*INBOX_CATEGORY_VALUES, name="inbox_category"),
        nullable=True,
    )

    # 사용자 override 카테고리 — DB 설계서 §5.4
    user_category: Mapped[str | None] = mapped_column(
        Enum(*INBOX_CATEGORY_VALUES, name="inbox_category", create_type=False),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        Enum(*INBOX_STATUS_VALUES, name="inbox_status"),
        nullable=False,
        server_default="captured",
    )

    # Goal 승격 시 연결 (S25 Triage) — DB 설계서 §5.4
    promoted_goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goals.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── relationships ──
    user: Mapped[User] = relationship()
    promoted_goal: Mapped[Goal | None] = relationship()
