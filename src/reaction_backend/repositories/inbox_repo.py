"""Inbox repository — S24/S25 Life Inbox + Triage (Issue #22-B).

규칙:
- user_id scope 자동.
- raw_text 는 application 레이어에서 암호화 (`safety.encrypt_inbox_text`). 본 repo 는
  암호화된 문자열을 그대로 INSERT/SELECT.
- soft delete only (`archived_at` + `status='archived'`).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.inbox_item import InboxItem
from reaction_backend.db.session import get_db


class InboxRepo:
    """InboxItem 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_status(self, user_id: UUID, status: str | None = None) -> list[InboxItem]:
        stmt = (
            select(InboxItem)
            .where(InboxItem.user_id == user_id)
            .order_by(InboxItem.created_at.desc())
        )
        if status == "archived":
            # 보관함 조회 — soft-deleted(archived) 항목만. 기본 활성 필터를 적용하지 않는다.
            stmt = stmt.where(InboxItem.status == "archived")
        else:
            # 활성 항목만(기본). status 지정 시 그 상태로 추가 필터.
            stmt = stmt.where(InboxItem.archived_at.is_(None))
            if status is not None:
                stmt = stmt.where(InboxItem.status == status)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, user_id: UUID, inbox_id: UUID) -> InboxItem | None:
        stmt = select(InboxItem).where(
            InboxItem.id == inbox_id,
            InboxItem.user_id == user_id,
            InboxItem.archived_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_any(self, user_id: UUID, inbox_id: UUID) -> InboxItem | None:
        """archived 포함 조회 — 복원(restore) 진입점 전용."""
        stmt = select(InboxItem).where(
            InboxItem.id == inbox_id,
            InboxItem.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        user_id: UUID,
        raw_text_encrypted: str,
        ai_category_guess: str | None = None,
        status: str = "captured",
    ) -> InboxItem:
        item = InboxItem(
            user_id=user_id,
            raw_text_encrypted=raw_text_encrypted,
            ai_category_guess=ai_category_guess,
            status=status,
        )
        self._session.add(item)
        await self._session.flush()
        await self._session.refresh(item)
        return item

    async def update(
        self,
        item: InboxItem,
        *,
        user_category: str | None = None,
        status: str | None = None,
        ai_category_guess: str | None = None,
    ) -> InboxItem:
        if user_category is not None:
            item.user_category = user_category
        if status is not None:
            item.status = status
        if ai_category_guess is not None:
            item.ai_category_guess = ai_category_guess
        await self._session.flush()
        return item

    async def mark_promoted_to_goal(self, item: InboxItem, goal_id: UUID) -> InboxItem:
        """convert-to-goal 후 status='promoted' + promoted_goal_id 연결."""
        item.status = "promoted"
        item.promoted_goal_id = goal_id
        await self._session.flush()
        return item

    async def mark_promoted_to_action(self, item: InboxItem) -> InboxItem:
        """convert-to-action 후 status='promoted' (action 링크 컬럼은 inbox 모델에 없음)."""
        item.status = "promoted"
        await self._session.flush()
        return item

    async def soft_delete(self, item: InboxItem) -> None:
        item.archived_at = datetime.now(UTC)
        item.status = "archived"
        await self._session.flush()

    async def restore(self, item: InboxItem) -> InboxItem:
        """보관 해제 — archived_at 클리어 + status 를 활성 상태로 복원. 이미 활성이면 no-op(멱등).

        AI 분류가 있던 항목은 classified 로, 아니면 captured 로 되돌린다(분류 결과 보존).
        """
        if item.archived_at is None:
            return item
        item.archived_at = None
        item.status = "classified" if item.ai_category_guess is not None else "captured"
        await self._session.flush()
        return item


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_inbox_repo(session: SessionDep) -> InboxRepo:
    return InboxRepo(session)
