"""TimePolicy repository — S07 시간 정책 (Issue #17).

규칙:
- user_id scope 자동 적용. 다른 사용자의 정책 접근 불가.
- soft delete only (archived_at, AGENTS.md §2). hard delete 금지.
- commit 은 호출자 책임 — 라우터에서 `await session.commit()`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.time_policy import TimePolicy
from reaction_backend.db.session import get_db


class TimePolicyRepo:
    """TimePolicy 영속화. FastAPI Depends 로 주입."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self, user_id: UUID) -> list[TimePolicy]:
        stmt = (
            select(TimePolicy)
            .where(
                TimePolicy.user_id == user_id,
                TimePolicy.archived_at.is_(None),
            )
            .order_by(TimePolicy.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, user_id: UUID, policy_id: UUID) -> TimePolicy | None:
        stmt = select(TimePolicy).where(
            TimePolicy.id == policy_id,
            TimePolicy.user_id == user_id,
            TimePolicy.archived_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        user_id: UUID,
        policy_type: str,
        payload: dict[str, Any],
    ) -> TimePolicy:
        policy = TimePolicy(
            user_id=user_id,
            policy_type=policy_type,
            payload=payload,
        )
        self._session.add(policy)
        await self._session.flush()
        await self._session.refresh(policy)
        return policy

    async def update(
        self,
        policy: TimePolicy,
        *,
        payload: dict[str, Any] | None = None,
        is_active: bool | None = None,
    ) -> TimePolicy:
        if payload is not None:
            policy.payload = payload
        if is_active is not None:
            policy.is_active = is_active
        await self._session.flush()
        return policy

    async def soft_delete(self, policy: TimePolicy) -> None:
        """archived_at = now + is_active=false. 두 필드 모두 set 으로 list_active 에서 자동 제외."""
        policy.archived_at = datetime.now(UTC)
        policy.is_active = False
        await self._session.flush()

    async def count_active(self, user_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(TimePolicy)
            .where(
                TimePolicy.user_id == user_id,
                TimePolicy.archived_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_time_policy_repo(session: SessionDep) -> TimePolicyRepo:
    return TimePolicyRepo(session)
