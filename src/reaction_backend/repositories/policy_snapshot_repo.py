"""PolicySnapshot repository — 학습 루프 산출물 조회 (#83 §14).

현재는 활성 스냅샷 조회만(get_active). 버전 이력/미리보기/적용/롤백은 후속
(agents/policy_update_agent.py). commit 은 호출자 책임.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.policy_snapshot import PolicySnapshot
from reaction_backend.db.session import get_db


class PolicySnapshotRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, user_id: UUID) -> PolicySnapshot | None:
        """현재 활성(is_active) 스냅샷 — 최신 버전 우선."""
        stmt = (
            select(PolicySnapshot)
            .where(
                PolicySnapshot.user_id == user_id,
                PolicySnapshot.is_active.is_(True),
            )
            .order_by(PolicySnapshot.version.desc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_policy_snapshot_repo(session: SessionDep) -> PolicySnapshotRepo:
    return PolicySnapshotRepo(session)
