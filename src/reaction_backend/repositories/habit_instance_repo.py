"""HabitInstance repository — S27 주별 인스턴스 (Issue #22).

규칙:
- 사용자 scope 는 habits → user_id 조인.
- (habit_id, week_start) UNIQUE — DB 설계서. 중복 INSERT 시도 X (`create_or_get_for_week`).
- 본 PR 에서는 POST /habits 시 이번 주 instance 자동 생성. 미래 주 cron 은 후속 (Issue #24).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.db.session import get_db


class HabitInstanceRepo:
    """HabitInstance 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user_week(self, user_id: UUID, week_start: date) -> list[HabitInstance]:
        """해당 사용자의 그 주 모든 active habit 의 인스턴스."""
        stmt = (
            select(HabitInstance)
            .join(Habit, Habit.id == HabitInstance.habit_id)
            .where(
                Habit.user_id == user_id,
                Habit.archived_at.is_(None),
                HabitInstance.week_start == week_start,
            )
            .order_by(HabitInstance.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_for_user(self, user_id: UUID, instance_id: UUID) -> HabitInstance | None:
        """user_id scope — habits 조인으로 다른 사용자 instance 접근 차단."""
        stmt = (
            select(HabitInstance)
            .join(Habit, Habit.id == HabitInstance.habit_id)
            .where(
                HabitInstance.id == instance_id,
                Habit.user_id == user_id,
                Habit.archived_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_for_week(self, habit_id: UUID, week_start: date) -> HabitInstance | None:
        stmt = select(HabitInstance).where(
            HabitInstance.habit_id == habit_id,
            HabitInstance.week_start == week_start,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_or_get_for_week(
        self, habit_id: UUID, week_start: date, target_count: int
    ) -> HabitInstance:
        """(habit_id, week_start) UNIQUE — 이미 있으면 그것 반환, 없으면 생성."""
        existing = await self.get_for_week(habit_id, week_start)
        if existing is not None:
            return existing
        instance = HabitInstance(
            habit_id=habit_id,
            week_start=week_start,
            target_count=target_count,
            done_count=0,
        )
        self._session.add(instance)
        await self._session.flush()
        await self._session.refresh(instance)
        return instance

    async def increment_done(self, instance: HabitInstance) -> HabitInstance:
        instance.done_count = instance.done_count + 1
        await self._session.flush()
        return instance


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_habit_instance_repo(session: SessionDep) -> HabitInstanceRepo:
    return HabitInstanceRepo(session)
