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


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_action_item_repo(session: SessionDep) -> ActionItemRepo:
    return ActionItemRepo(session)
