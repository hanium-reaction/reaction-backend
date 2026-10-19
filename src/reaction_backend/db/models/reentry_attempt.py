"""First-action reporting is deliberately separate from task completion."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from reaction_backend.db.base import Base, TimestampMixin


class ReentryAttempt(Base, TimestampMixin):
    __tablename__ = "reentry_attempts"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    action_item_id: Mapped[UUID] = mapped_column(ForeignKey("action_items.id"), index=True)
    execution_id: Mapped[UUID | None] = mapped_column(ForeignKey("execution_events.id"))
    entry_kind: Mapped[str] = mapped_column(String(20))
    first_step_encrypted: Mapped[str | None] = mapped_column(Text)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    step_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
