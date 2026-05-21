"""CalendarConnection — 외부 캘린더 연결 (사용자당 1행). S04.

MVP 스코프: Google read-only freebusy. write-back은 P1.
provider 확장 가능 (apple, samsung).

DB 설계서 v0.7.1 §5.23:
- provider: google/apple/samsung
- scopes (복수): 공백 구분 권한 범위

보안:
- access/refresh token 은 반드시 at-rest 암호화 (`*_encrypted` 접미사 컬럼).
  실제 암호화 로직은 후속 PR.
- 권한 박탈 / refresh 실패 시 `revoked_at` set → 다음 진입 시 재연결 안내.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


# DB 설계서 §5.23
CALENDAR_PROVIDER_VALUES = ("google", "apple", "samsung")


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

    # MVP 는 google 만. apple/samsung 확장 가능 — DB 설계서 §5.23
    provider: Mapped[str] = mapped_column(
        Enum(*CALENDAR_PROVIDER_VALUES, name="calendar_provider"),
        nullable=False,
        server_default="google",
    )

    # 암호화된 토큰 (실제 암호화 함수는 후속 PR)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 권한 범위 — 공백 구분 (예: "https://www.googleapis.com/auth/calendar.readonly")
    # DB 설계서 §5.23 컬럼명 정렬: scopes (복수)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)

    # ── relationships ──
    user: Mapped[User] = relationship(back_populates="calendar_connection")
