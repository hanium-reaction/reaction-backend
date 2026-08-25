"""InviteCode — 초대코드 가입 게이트 (#324, FE #237 §8).

Play 첫 공개는 초대코드 기반 30명으로 제한한다(#237 출시 원칙). 코드는 발급 시점에
운영자(`scripts/manage_invite_codes.py`)가 미리 만들어 두고, 신규 가입(`POST /auth/google`)
이 유효·미사용 코드를 소비한다 — 1코드 1회용, 재사용 불가.

기존 사용자 로그인(이미 `users` 에 email 이 있는 경우)은 이 테이블을 전혀 보지 않는다
(`routes/auth.py` 가 upsert 전에 신규/기존을 먼저 가른다).
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


class InviteCode(Base, TimestampMixin):
    __tablename__ = "invite_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # 사용자가 입력하는 코드 문자열. 대소문자 구분 없이 저장 시점에 대문자로 정규화
    # (repo 책임) — 타이핑 실수(대소문자)로 인한 "틀림" 오탐을 줄인다.
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)

    # 발급 목적 메모(예: "Play 리뷰어", "팀"). 사용자 노출 안 됨 — 운영자 식별용.
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # NULL = 미사용. 값이 있으면 소진됨 — 재사용 불가(1코드 1회).
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── relationships ──
    used_by: Mapped[User | None] = relationship()
