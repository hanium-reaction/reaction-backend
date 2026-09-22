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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
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

    async def get_by_id_for_update(self, user_id: UUID, inbox_id: UUID) -> InboxItem | None:
        """승격(convert-*)용 잠금 읽기 — `get_by_id` 와 같은 조건 + `FOR UPDATE`.

        두 번 탭한 두 요청이 모두 "아직 승격 전" 을 읽고 카드·목표를 하나씩 만들던 경로다.
        행을 잠그면 뒤 요청은 앞 요청이 commit 한 `status='promoted'` 를 본다(READ COMMITTED
        에서 잠금 대기 뒤 행을 다시 읽는다). 읽기 전용 조회에는 쓰지 않는다.
        """
        stmt = (
            select(InboxItem)
            .where(
                InboxItem.id == inbox_id,
                InboxItem.user_id == user_id,
                InboxItem.archived_at.is_(None),
            )
            .with_for_update()
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
        *,
        source: str = "user",
        resource_slug: str | None = None,
    ) -> InboxItem:
        item = InboxItem(
            user_id=user_id,
            raw_text_encrypted=raw_text_encrypted,
            ai_category_guess=ai_category_guess,
            status=status,
            source=source,
            resource_slug=resource_slug,
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

    async def has_resource(self, user_id: UUID, resource_slug: str) -> bool:
        """이 사용자에게 이 자료가 이미 있는가 — **보관된 것도 포함** (BE #171).

        `get_by_id_any` 와 함께 `archived_at` 을 일부러 안 거르는 두 번째 조회다.
        보관은 "이 자료 안 볼래" 라는 의사표시라, 필터를 넣으면 목표를 만들 때마다
        사용자가 치운 자료가 되살아난다. 방향을 SQL 핀 테스트로 고정해 두었다.
        """
        stmt = (
            select(func.count())
            .select_from(InboxItem)
            .where(
                InboxItem.user_id == user_id,
                InboxItem.resource_slug == resource_slug,
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one()) > 0

    async def soft_delete(self, item: InboxItem) -> None:
        item.archived_at = datetime.now(UTC)
        item.status = "archived"
        await self._session.flush()

    async def restore(self, item: InboxItem) -> InboxItem:
        """보관 해제 — archived_at 클리어 + status 를 활성 상태로 복원. 이미 활성이면 no-op(멱등).

        AI 분류가 있던 항목은 classified 로, 아니면 captured 로 되돌린다(분류 결과 보존).

        **이미 목표·할 일로 옮긴 항목은 `promoted` 로 돌아온다.** 보관이 status 를
        `archived` 로 덮어써서, 되살리면 옮기기 버튼이 다시 나타나 같은 메모로 카드·목표가
        하나 더 생겼다. 목표는 `promoted_goal_id` 가, 할 일은 이 항목에서 만든 카드가 증거다
        (inbox 에는 할 일 링크 컬럼이 없다). 추천 자료(system)는 승격 대상이 아니라 제외 —
        '한 걸음 채택' 카드도 `inbox_item_id` 를 달지만 승격이 아니다.
        """
        if item.archived_at is None:
            return item
        item.archived_at = None
        if await self._was_promoted(item):
            item.status = "promoted"
        else:
            item.status = "classified" if item.ai_category_guess is not None else "captured"
        await self._session.flush()
        return item

    async def _was_promoted(self, item: InboxItem) -> bool:
        if item.promoted_goal_id is not None:
            return True
        if item.source == "system":
            return False
        stmt = (
            select(func.count())
            .select_from(ActionItem)
            .where(ActionItem.user_id == item.user_id, ActionItem.inbox_item_id == item.id)
        )
        return int((await self._session.execute(stmt)).scalar_one()) > 0


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_inbox_repo(session: SessionDep) -> InboxRepo:
    return InboxRepo(session)
