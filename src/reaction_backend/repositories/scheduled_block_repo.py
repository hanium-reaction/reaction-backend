"""ScheduledBlock repository — S14 주간 그리드 / S15 직접 편집 (Issue #21-B).

규칙:
- user_id scope 자동.
- 주간 조회는 action_items 와 join 해 (블록, 제목, 카테고리) 를 함께 반환.
- 충돌 검사는 자기 자신과 cancelled 블록을 제외한 시간 겹침.
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db


class ScheduledBlockRepo:
    """ScheduledBlock 주간 조회 + 단건 조회 + 충돌 후보."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_week(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[tuple[ScheduledBlock, str, str, UUID | None]]:
        """[start_dt, end_dt) 의 블록을 (블록, action 제목, 카테고리, goal_id) 로 — start_at 오름차순.

        goal_id 는 블록이 매달린 action_item 의 goal FK — 주간 그리드가 블록을 목표와
        연결(분류/색상)할 수 있게 함께 내려준다. 목표 미연결 액션(inbox 등)은 None.
        """
        stmt = (
            select(ScheduledBlock, ActionItem.title, ActionItem.category, ActionItem.goal_id)
            .join(ActionItem, ScheduledBlock.action_item_id == ActionItem.id)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.start_at >= start_dt,
                ScheduledBlock.start_at < end_dt,
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return [
            (block, title, category, goal_id) for block, title, category, goal_id in result.all()
        ]

    async def get_block(self, user_id: UUID, block_id: UUID) -> ScheduledBlock | None:
        stmt = select(ScheduledBlock).where(
            ScheduledBlock.id == block_id,
            ScheduledBlock.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_action_item(
        self, user_id: UUID, action_item_id: UUID
    ) -> list[ScheduledBlock]:
        """특정 ActionItem 의 블록 (cancelled 제외) — replan 멱등 체크용 (#20-B)."""
        stmt = (
            select(ScheduledBlock)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.action_item_id == action_item_id,
                ScheduledBlock.block_status != "cancelled",
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def create_block(
        self,
        *,
        user_id: UUID,
        action_item_id: UUID,
        start_at: datetime,
        end_at: datetime,
        source: str,
    ) -> ScheduledBlock:
        """새 시간 블록 생성 (replan 회복 배치 — source='recovery', #20-B).

        commit 은 호출자 책임.
        """
        block = ScheduledBlock(
            user_id=user_id,
            action_item_id=action_item_id,
            start_at=start_at,
            end_at=end_at,
            source=source,
        )
        self._session.add(block)
        await self._session.flush()
        await self._session.refresh(block)
        return block

    async def list_overlapping(
        self,
        user_id: UUID,
        start_dt: datetime,
        end_dt: datetime,
        *,
        exclude_block_id: UUID,
    ) -> list[ScheduledBlock]:
        """[start_dt, end_dt) 와 겹치는 다른 블록 (자기 자신·cancelled 제외)."""
        stmt = select(ScheduledBlock).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.id != exclude_block_id,
            ScheduledBlock.block_status != "cancelled",
            ScheduledBlock.start_at < end_dt,
            ScheduledBlock.end_at > start_dt,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_scheduled_block_repo(session: SessionDep) -> ScheduledBlockRepo:
    return ScheduledBlockRepo(session)
