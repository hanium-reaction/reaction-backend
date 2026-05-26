"""Goal repository — S26 (Issue #22).

규칙:
- user_id scope 자동.
- soft delete only (`archived_at`).
- Focus ≤ 3 / Maintain ≤ 5 한도는 라우터에서 `count_by_tier` 로 enforce.
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.session import get_db


class GoalRepo:
    """Goal 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self, user_id: UUID) -> list[Goal]:
        stmt = (
            select(Goal)
            .where(
                Goal.user_id == user_id,
                Goal.archived_at.is_(None),
            )
            .order_by(Goal.priority_level.asc(), Goal.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, user_id: UUID, goal_id: UUID) -> Goal | None:
        stmt = select(Goal).where(
            Goal.id == goal_id,
            Goal.user_id == user_id,
            Goal.archived_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_by_tier(self, user_id: UUID, tier: str) -> int:
        stmt = (
            select(func.count())
            .select_from(Goal)
            .where(
                Goal.user_id == user_id,
                Goal.goal_tier == tier,
                Goal.archived_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def create(
        self,
        user_id: UUID,
        title: str,
        category: str,
        goal_tier: str,
        priority_level: int,
        deadline: date | None = None,
        estimated_minutes: int | None = None,
    ) -> Goal:
        goal = Goal(
            user_id=user_id,
            title=title,
            category=category,
            goal_tier=goal_tier,
            priority_level=priority_level,
            deadline=deadline,
            estimated_minutes=estimated_minutes,
        )
        self._session.add(goal)
        await self._session.flush()
        await self._session.refresh(goal)
        return goal

    async def update(
        self,
        goal: Goal,
        *,
        title: str | None = None,
        deadline: date | None = None,
        priority_level: int | None = None,
        goal_tier: str | None = None,
    ) -> Goal:
        if title is not None:
            goal.title = title
        if deadline is not None:
            goal.deadline = deadline
        if priority_level is not None:
            goal.priority_level = priority_level
        if goal_tier is not None:
            goal.goal_tier = goal_tier
        await self._session.flush()
        return goal

    async def park(self, goal: Goal) -> Goal:
        """Focus → Parked 전환 (tier 변경 단축)."""
        goal.goal_tier = "parked"
        await self._session.flush()
        return goal

    async def soft_delete(self, goal: Goal) -> None:
        goal.archived_at = datetime.now(UTC)
        goal.status = "archived"
        await self._session.flush()


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_goal_repo(session: SessionDep) -> GoalRepo:
    return GoalRepo(session)
