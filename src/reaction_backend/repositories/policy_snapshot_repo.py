"""PolicySnapshot repository — 학습 루프 산출물 (#83 §14, #168).

`#168` 이전에는 `get_active` **하나뿐**이었고 행을 만드는 코드가 레포 전체에 0곳이라
`GET /policy-snapshot/current` 가 프로덕션에서 **항상 404** 였다(라우트 버그가 아니라
생산 경로가 통째로 없었다). 여기에 생산·이력·활성 전환을 채운다.

append-only (ADR-0001 §3.2 · 모델 docstring):
- 새 버전은 **INSERT**, 이전 활성 행은 `is_active=false` + `valid_to` 로 닫는다.
- 기존 행의 4 영역 JSONB 는 **절대 수정하지 않는다** — 이력이 곧 감사 기록이다.

commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
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

    async def list_history(self, user_id: UUID) -> list[PolicySnapshot]:
        """버전 이력 — 최신 버전이 앞. 비활성(지난) 버전도 포함한다."""
        stmt = (
            select(PolicySnapshot)
            .where(PolicySnapshot.user_id == user_id)
            .order_by(PolicySnapshot.version.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_version(self, user_id: UUID, version: int) -> PolicySnapshot | None:
        stmt = select(PolicySnapshot).where(
            PolicySnapshot.user_id == user_id,
            PolicySnapshot.version == version,
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def next_version(self, user_id: UUID) -> int:
        """다음 버전 번호 — 첫 스냅샷이면 1.

        `max(version)+1` 로 계산한다. `count()+1` 이면 롤백으로 버전이 늘어난 뒤
        기존 번호와 충돌해 `uq_policy_snapshots_user_version` 에 걸린다.
        """
        stmt = select(func.max(PolicySnapshot.version)).where(PolicySnapshot.user_id == user_id)
        result = await self._session.execute(stmt)
        return int(result.scalar() or 0) + 1

    async def create_active(
        self,
        user_id: UUID,
        *,
        behavioral_profile: dict[str, Any],
        execution_constraints: dict[str, Any],
        interaction_style: dict[str, Any],
        recovery_policy: dict[str, Any],
        source: str,
        reason_for_update: str | None,
        now: datetime,
        prompt_version: str | None = None,
    ) -> PolicySnapshot:
        """새 버전을 INSERT 하고 활성으로 만든다. 이전 활성 행은 닫는다.

        같은 트랜잭션 안에서 (1) 기존 활성 닫기 (2) 새 행 INSERT 를 함께 한다 — 두 활성
        스냅샷이 동시에 존재하는 순간이 없어야 `get_active` 가 흔들리지 않는다.
        """
        for previous in await self.list_history(user_id):
            if previous.is_active:
                previous.is_active = False
                previous.valid_to = now

        snapshot = PolicySnapshot(
            user_id=user_id,
            version=await self.next_version(user_id),
            is_active=True,
            behavioral_profile=behavioral_profile,
            execution_constraints=execution_constraints,
            interaction_style=interaction_style,
            recovery_policy=recovery_policy,
            source=source,
            reason_for_update=reason_for_update,
            prompt_version=prompt_version,
            valid_from=now,
            valid_to=None,
        )
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_policy_snapshot_repo(session: SessionDep) -> PolicySnapshotRepo:
    return PolicySnapshotRepo(session)
