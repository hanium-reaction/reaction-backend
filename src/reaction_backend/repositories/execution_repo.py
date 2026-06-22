"""Execution repository — S13 Focus 실행 로깅 + S18 실패 태깅 (Issue #19-B).

규칙:
- user_id scope 자동.
- `action_item.status` 전이는 체크인(execution 레이어)의 책임 — ActionItemRepo
  docstring 과 합의된 유일한 변경 지점. 회복(Recovery)은 절대 변경하지 않는다.
- 실패 태그는 1회만 기록 (재태깅 시 409) — hard delete 회피 (AGENTS.md §2).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.failure_reason_tag import FailureReasonTag
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db


class ExecutionRepo:
    """ExecutionEvent + ad-hoc ScheduledBlock + ExecutionFailureTag 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── execution ──
    async def get_by_id(self, user_id: UUID, execution_id: UUID) -> ExecutionEvent | None:
        stmt = select(ExecutionEvent).where(
            ExecutionEvent.id == execution_id,
            ExecutionEvent.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_for_action(
        self, user_id: UUID, action_item_id: UUID
    ) -> ExecutionEvent | None:
        """진행 중(in_progress) 실행 — [▶ 시작] 중복 방지."""
        stmt = select(ExecutionEvent).where(
            ExecutionEvent.user_id == user_id,
            ExecutionEvent.action_item_id == action_item_id,
            ExecutionEvent.completion_status == "in_progress",
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def find_open_block(self, user_id: UUID, action_item_id: UUID) -> ScheduledBlock | None:
        """이 카드의 미종결(scheduled/started) 블록 — 가장 이른 것."""
        stmt = (
            select(ScheduledBlock)
            .where(
                ScheduledBlock.user_id == user_id,
                ScheduledBlock.action_item_id == action_item_id,
                ScheduledBlock.block_status.in_(("scheduled", "started")),
            )
            .order_by(ScheduledBlock.start_at)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def create_adhoc_block(
        self, *, user_id: UUID, action_item: ActionItem, start_at: datetime
    ) -> ScheduledBlock:
        """블록 없이 시작한 즉석 실행용 블록 (source='user_edit', §5.10)."""
        block = ScheduledBlock(
            user_id=user_id,
            action_item_id=action_item.id,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=action_item.estimated_minutes),
            block_status="started",
            source="user_edit",
        )
        self._session.add(block)
        await self._session.flush()
        await self._session.refresh(block)
        return block

    async def create_execution(
        self,
        *,
        user_id: UUID,
        action_item_id: UUID,
        block: ScheduledBlock,
        started_at: datetime,
    ) -> ExecutionEvent:
        execution = ExecutionEvent(
            user_id=user_id,
            action_item_id=action_item_id,
            scheduled_block_id=block.id,
            plan_start_at=block.start_at,
            plan_end_at=block.end_at,
            actual_start_at=started_at,
            completion_status="in_progress",
        )
        self._session.add(execution)
        await self._session.flush()
        await self._session.refresh(execution)
        return execution

    async def get_block(self, block_id: UUID) -> ScheduledBlock | None:
        stmt = select(ScheduledBlock).where(ScheduledBlock.id == block_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── failure tags ──
    async def list_active_failure_tags(self) -> list[FailureReasonTag]:
        stmt = (
            select(FailureReasonTag)
            .where(FailureReasonTag.is_active.is_(True))
            .order_by(FailureReasonTag.sort_order)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def has_failure_tags(self, execution_id: UUID) -> bool:
        stmt = select(ExecutionFailureTag.id).where(
            ExecutionFailureTag.execution_id == execution_id
        )
        result = await self._session.execute(stmt)
        return result.scalars().first() is not None

    async def add_failure_tags(
        self,
        *,
        execution_id: UUID,
        tag_codes: list[str],
        memo_encrypted: str | None,
    ) -> list[ExecutionFailureTag]:
        rows = [
            ExecutionFailureTag(
                execution_id=execution_id,
                tag_code=code,
                memo_encrypted=memo_encrypted,
            )
            for code in tag_codes
        ]
        for row in rows:
            self._session.add(row)
        await self._session.flush()
        return rows


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_execution_repo(session: SessionDep) -> ExecutionRepo:
    return ExecutionRepo(session)
