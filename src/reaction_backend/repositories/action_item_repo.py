"""ActionItem repository — S10 Today/실행 (Issue #22-B 부분 도입).

본 PR 범위는 **convert-to-action 단일 진입점만**. 본격 CRUD/Today/Recovery 는
Issue #19 (Today/Brief) / #20 (Recovery) 에서 확장.

규칙:
- user_id scope 자동.
- 원본 `action_item.status` 변경 금지 (AGENTS.md §2 — Resilience 지표 전제). 본 repo
  는 create 만 노출.
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.session import get_db


class ActionItemRepo:
    """ActionItem 영속화 (본 PR 은 create_from_inbox 만)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
