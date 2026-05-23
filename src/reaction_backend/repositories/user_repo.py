"""User repository — DB upsert / 조회 (Issue #16).

규칙:
- `email` 이 1차 식별 키 (Google OAuth). 신규는 `onboarding_state=WELCOME` (DB server_default).
- 기존 user 는 `name` · `last_active_at` 만 갱신, `onboarding_state` · `tone_mode` 는 보존.
- hard delete 금지 (AGENTS.md §2). 본 repo 는 delete 미제공.
- commit 은 호출자 책임 — 라우터에서 `await session.commit()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db


@dataclass(slots=True)
class GoogleProfile:
    """upsert 입력 — Google id_token 검증 결과에서 추출."""

    email: str
    name: str


class UserRepo:
    """User 영속화. FastAPI Depends 로 주입 (`get_user_repo`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        stmt = select(User).where(User.id == user_id, User.archived_at.is_(None))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email, User.archived_at.is_(None))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_from_google(self, profile: GoogleProfile) -> User:
        """email 기준 upsert.

        - 신규: WELCOME 상태로 생성 (`onboarding_state` 는 DB server_default).
        - 기존: `name` · `last_active_at` 만 갱신, `onboarding_state` 등 보존.
        """
        existing = await self.get_by_email(profile.email)
        now = datetime.now(UTC)
        if existing is not None:
            existing.name = profile.name
            existing.last_active_at = now
            await self._session.flush()
            return existing
        user = User(
            email=profile.email,
            name=profile.name,
            last_active_at=now,
        )
        self._session.add(user)
        await self._session.flush()
        await self._session.refresh(user)
        return user


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_user_repo(session: SessionDep) -> UserRepo:
    return UserRepo(session)
