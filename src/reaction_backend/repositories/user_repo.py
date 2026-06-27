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

    async def list_active(self) -> list[User]:
        """모든 활성 사용자 (cron sweep 용, #24) — onboarding ACTIVE + 익명화/삭제 안 됨."""
        stmt = select(User).where(
            User.archived_at.is_(None),
            User.is_anonymized.is_(False),
            User.onboarding_state == "ACTIVE",
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

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

    async def set_tone_mode(self, user: User, tone_mode: str) -> User:
        """톤 모드 변경 (S23 설정, Issue #23).

        사용자 명시 설정 변경 — onboarding 상태 전이는 없다. commit 은 호출자 책임.
        """
        user.tone_mode = tone_mode
        await self._session.flush()
        return user

    async def advance_onboarding(
        self,
        user: User,
        expected_from: str | tuple[str, ...],
        to: str,
    ) -> bool:
        """안전한 onboarding 상태 전이 (Issue #17).

        현재 상태가 `expected_from` 집합에 있을 때만 `to` 로 전이한다.
        이미 더 진행된 상태(예: ACTIVE)면 no-op — 같은 endpoint 두 번 호출해도 멱등.

        Returns:
            전이가 일어났는지 (true=advanced, false=no-op).
        """
        expected = (expected_from,) if isinstance(expected_from, str) else expected_from
        if user.onboarding_state in expected:
            user.onboarding_state = to
            await self._session.flush()
            return True
        return False


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_user_repo(session: SessionDep) -> UserRepo:
    return UserRepo(session)
