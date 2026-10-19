"""Optional encrypted context; independent from completion and recovery tags."""

from uuid import UUID

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from reaction_backend.db.base import Base, TimestampMixin


class ActionResource(Base, TimestampMixin):
    __tablename__ = "action_resources"
    action_id: Mapped[UUID] = mapped_column(ForeignKey("action_items.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    content_encrypted: Mapped[str | None] = mapped_column(Text)


class ExecutionNote(Base, TimestampMixin):
    __tablename__ = "execution_notes"
    execution_id: Mapped[UUID] = mapped_column(ForeignKey("execution_events.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    memo_encrypted: Mapped[str | None] = mapped_column(Text)
