"""ActionItem repository — S10 Today/실행 (Issue #22-B + #19-A 조회 확장).

규칙:
- user_id scope 자동.
- 원본 `action_item.status` 변경 금지 (AGENTS.md §2 — Resilience 지표 전제). 본 repo
  는 create + **read(by date/id)** 만 노출. status 변경은 execution_events 레이어(#19-B).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db


class ActionItemRepo:
    """ActionItem 영속화 — create_from_inbox + 조회(#19-A)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_date(self, user_id: UUID, target_date: date) -> list[ActionItem]:
        """오늘 어젠다 — target_date 의 활성 카드 (priority 오름차순)."""
        stmt = (
            select(ActionItem)
            .where(
                ActionItem.user_id == user_id,
                ActionItem.target_date == target_date,
                ActionItem.archived_at.is_(None),
            )
            .order_by(ActionItem.priority.asc(), ActionItem.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, user_id: UUID, action_id: UUID) -> ActionItem | None:
        stmt = select(ActionItem).where(
            ActionItem.id == action_id,
            ActionItem.user_id == user_id,
            ActionItem.archived_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_planned_without_block(self, user_id: UUID) -> list[ActionItem]:
        """활성 블록(비-cancelled)이 하나도 없는 **planned** 카드 — 미배치 백로그(읽기 전용).

        주간 forward 재계획이 '아직 캘린더에 안 올라간 남은 일'을 함께 배치할 때의 소스.
        수락했지만 아직 개별 재배치하지 않은 회복 카드(source=recovery_*, status=planned)가
        여기 포함된다. **원본 status 는 읽기만 — 변경 금지**(AGENTS §2).
        """
        has_active_block = select(ScheduledBlock.action_item_id).where(
            ScheduledBlock.user_id == user_id,
            ScheduledBlock.block_status != "cancelled",
        )
        stmt = (
            select(ActionItem)
            .where(
                ActionItem.user_id == user_id,
                ActionItem.archived_at.is_(None),
                ActionItem.status == "planned",
                ActionItem.id.not_in(has_active_block),
            )
            .order_by(ActionItem.priority.asc(), ActionItem.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def create_from_inbox(
        self,
        user_id: UUID,
        inbox_item_id: UUID,
        title: str,
        category: str,
        target_date: date,
    ) -> ActionItem:
        """Inbox 항목을 실행 카드(ActionItem)로 변환 (source='inbox')."""
        action = ActionItem(
            user_id=user_id,
            title=title,
            target_date=target_date,
            category=category,
            source="inbox",
            inbox_item_id=inbox_item_id,
        )
        self._session.add(action)
        await self._session.flush()
        await self._session.refresh(action)
        return action

    async def create_from_recovery(
        self,
        *,
        user_id: UUID,
        parent_action_item_id: UUID,
        title: str,
        category: str,
        source: str,
        target_date: date,
        estimated_minutes: int,
    ) -> ActionItem:
        """회복 수락 시 새 실행 카드 생성 (source='recovery_*', Issue #20-A).

        원본 카드의 status 는 변경하지 않고 `parent_action_item_id` 로 혈통만 기록한다
        (AGENTS.md §2 — Resilience 지표 전제).
        """
        action = ActionItem(
            user_id=user_id,
            title=title,
            target_date=target_date,
            category=category,
            source=source,
            parent_action_item_id=parent_action_item_id,
            estimated_minutes=estimated_minutes,
        )
        self._session.add(action)
        await self._session.flush()
        await self._session.refresh(action)
        return action


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_action_item_repo(session: SessionDep) -> ActionItemRepo:
    return ActionItemRepo(session)
