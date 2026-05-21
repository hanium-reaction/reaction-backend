"""CalendarConnection — Google Calendar 연결 (사용자당 1행). S04.

MVP 스코프: read-only freebusy. write-back은 P1.

보안:
- access/refresh token 은 반드시 at-rest 암호화 (`*_encrypted` 접미사 컬럼).
  실제 암호화 로직은 후속 PR (Issue #2 범위 외였지만 컬럼 이름은 잡아둠).
- 권한 박탈 / refresh 실패 시 `revoked_at` set → 다음 진입 시 재연결 안내.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


class CalendarConnection(Base, TimestampMixin):
    __tablename__ = "calendar_connections"

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

    # 암호화된 토큰 (실제 암호화 함수는 후속 PR)
    access_token_encrypted: Mapped[str] = mapped_column(String(2048), nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(String(2048), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # OAuth scope (예: "https://www.googleapis.com/auth/calendar.readonly")
    scope: Mapped[str] = mapped_column(String(512), nullable=False)

    # ── relationships ──
    user: Mapped[User] = relationship(back_populates="calendar_connection")
