"""Consent repository — S28 동의 기록 (Issue #23-B).

append-only: `add` 는 항상 새 행 INSERT. `list_current` 는 consent_type 별 최신 1행.
commit 은 호출자 책임.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.user_consent import UserConsent
from reaction_backend.db.session import get_db


class ConsentRepo:
    """UserConsent 영속화 (append-only)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_current(self, user_id: UUID) -> list[UserConsent]:
        """consent_type 별 최신 1행 (created_at 내림차순에서 첫 등장)."""
        stmt = (
            select(UserConsent)
            .where(UserConsent.user_id == user_id)
            .order_by(UserConsent.created_at.desc())
        )
        result = await self._session.execute(stmt)
        seen: set[str] = set()
        latest: list[UserConsent] = []
        for row in result.scalars().all():
            if row.consent_type not in seen:
                seen.add(row.consent_type)
                latest.append(row)
        return latest

    async def add(self, user_id: UUID, consent_type: str, *, is_granted: bool) -> UserConsent:
        consent = UserConsent(
            user_id=user_id,
            consent_type=consent_type,
            is_granted=is_granted,
        )
        self._session.add(consent)
        await self._session.flush()
        await self._session.refresh(consent)
        return consent


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_consent_repo(session: SessionDep) -> ConsentRepo:
    return ConsentRepo(session)
