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
from sqlalchemy import select, update
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
    ) -> list[tuple[ScheduledBlock, str, str]]:
        """[start_dt, end_dt) 의 블록을 (블록, action 제목, 카테고리) 로 — start_at 오름차순."""
        stmt = (
            select(ScheduledBlock, ActionItem.title, ActionItem.category)
            .join(ActionItem, ScheduledBlock.action_item_id == ActionItem.id)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.start_at >= start_dt,
                ScheduledBlock.start_at < end_dt,
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return [(block, title, category) for block, title, category in result.all()]

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

    async def list_scheduled_between(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[tuple[ScheduledBlock, ActionItem]]:
        """[start_dt, end_dt) 의 **미착수('scheduled')** 블록 + 그 ActionItem.

        주간 forward 재계획의 재배치 대상 — 이 블록들을 취소하고 같은 액션을 다시 배치한다.
        시작/완료된 블록은 제외(불변 보존)한다.
        """
        stmt = (
            select(ScheduledBlock, ActionItem)
            .join(ActionItem, ScheduledBlock.action_item_id == ActionItem.id)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.block_status == "scheduled",
                ScheduledBlock.start_at >= start_dt,
                ScheduledBlock.start_at < end_dt,
                ActionItem.archived_at.is_(None),
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return [(block, action) for block, action in result.all()]

    async def list_committed_between(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[ScheduledBlock]:
        """[start_dt, end_dt) 의 이미 **시작/완료된** 블록 — 재계획이 회피할 확정 일정(fit-around)."""
        stmt = select(ScheduledBlock).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.block_status.in_(("started", "finished")),
            ScheduledBlock.start_at >= start_dt,
            ScheduledBlock.start_at < end_dt,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def cancel_scheduled_between(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> int:
        """[start_dt, end_dt) 의 **미착수** 블록을 cancelled 로 전이 — 재계획 승인 시 미래 교체.

        시작/완료된 블록·과거 블록은 건드리지 않는다(불변). soft state(status 전이)라 hard delete
        아님. 반환값은 취소된 행 수.
        """
        stmt = (
            update(ScheduledBlock)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.block_status == "scheduled",
                ScheduledBlock.start_at >= start_dt,
                ScheduledBlock.start_at < end_dt,
            )
            .values(block_status="cancelled")
        )
        result = await self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_busy_between(
        self, user_id: UUID, start_dt: datetime, end_dt: datetime
    ) -> list[ScheduledBlock]:
        """[start_dt, end_dt) 와 겹치는 모든 블록 (cancelled 제외) — 재계획 시 회피할 기존 일정.

        First Plan 스케줄러가 이미 승인된 블록을 busy 로 반영해 그 위에 겹쳐 잡지 않게 한다
        (비파괴 fit-around). `list_overlapping` 과 달리 자기 자신 제외 인자가 없다.
        """
        stmt = select(ScheduledBlock).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.block_status != "cancelled",
            ScheduledBlock.start_at < end_dt,
            ScheduledBlock.end_at > start_dt,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_scheduled_block_repo(session: SessionDep) -> ScheduledBlockRepo:
    return ScheduledBlockRepo(session)
