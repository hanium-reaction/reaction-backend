"""Habit repository — S27 (Issue #22).

규칙:
- user_id scope 자동.
- soft delete only (`archived_at`).
- frequency_per_week CHECK 1~7 은 DB CheckConstraint + Pydantic 둘 다 enforce.
- 이번 주 habit_instance 자동 생성은 라우터에서 호출 (`HabitInstanceRepo.create_or_get_for_week`).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.habit import Habit
from reaction_backend.db.session import get_db
from reaction_backend.schemas.common import KST


def current_week_start_kst() -> date:
    """이번 주 월요일 (KST 기준). habit_instances.week_start 와 매칭."""
    today = datetime.now(KST).date()
    return today - timedelta(days=today.weekday())  # Monday=0


class HabitRepo:
    """Habit 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self, user_id: UUID) -> list[Habit]:
        stmt = (
            select(Habit)
            .where(
                Habit.user_id == user_id,
                Habit.archived_at.is_(None),
            )
            .order_by(Habit.priority_level.asc(), Habit.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, user_id: UUID, habit_id: UUID) -> Habit | None:
        stmt = select(Habit).where(
            Habit.id == habit_id,
            Habit.user_id == user_id,
            Habit.archived_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        user_id: UUID,
        title: str,
        category: str,
        frequency_per_week: int,
        minutes_per_session: int,
        time_preference: str,
        priority_level: int,
    ) -> Habit:
        habit = Habit(
            user_id=user_id,
            title=title,
            category=category,
            frequency_per_week=frequency_per_week,
            target_count=frequency_per_week,
            minutes_per_session=minutes_per_session,
            time_preference=time_preference,
            priority_level=priority_level,
        )
        self._session.add(habit)
        await self._session.flush()
        await self._session.refresh(habit)
        return habit

    async def update(
        self,
        habit: Habit,
        *,
        title: str | None = None,
        frequency_per_week: int | None = None,
    ) -> Habit:
        if title is not None:
            habit.title = title
        if frequency_per_week is not None:
            habit.frequency_per_week = frequency_per_week
            habit.target_count = frequency_per_week
        await self._session.flush()
        return habit

    async def soft_delete(self, habit: Habit) -> None:
        habit.archived_at = datetime.now(UTC)
        await self._session.flush()

    async def count_active(self, user_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(Habit)
            .where(
                Habit.user_id == user_id,
                Habit.archived_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_habit_repo(session: SessionDep) -> HabitRepo:
    return HabitRepo(session)
