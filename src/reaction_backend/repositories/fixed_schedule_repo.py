"""FixedSchedule repository — S05 수동 고정 일정 (Issue #17).

규칙:
- user_id scope 자동 적용.
- soft delete only (archived_at, AGENTS.md §2).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.session import get_db


class FixedScheduleRepo:
    """FixedSchedule 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self, user_id: UUID) -> list[FixedSchedule]:
        stmt = (
            select(FixedSchedule)
            .where(
                FixedSchedule.user_id == user_id,
                FixedSchedule.archived_at.is_(None),
            )
            .order_by(FixedSchedule.start_time.asc(), FixedSchedule.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, user_id: UUID, schedule_id: UUID) -> FixedSchedule | None:
        stmt = select(FixedSchedule).where(
            FixedSchedule.id == schedule_id,
            FixedSchedule.user_id == user_id,
            FixedSchedule.archived_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        user_id: UUID,
        title: str,
        days_of_week: list[str],
        start_time: time,
        end_time: time,
    ) -> FixedSchedule:
        schedule = FixedSchedule(
            user_id=user_id,
            title=title,
            days_of_week=days_of_week,
            start_time=start_time,
            end_time=end_time,
        )
        self._session.add(schedule)
        await self._session.flush()
        await self._session.refresh(schedule)
        return schedule

    async def update(
        self,
        schedule: FixedSchedule,
        *,
        title: str | None = None,
        days_of_week: list[str] | None = None,
        start_time: time | None = None,
        end_time: time | None = None,
    ) -> FixedSchedule:
        if title is not None:
            schedule.title = title
        if days_of_week is not None:
            schedule.days_of_week = days_of_week
        if start_time is not None:
            schedule.start_time = start_time
        if end_time is not None:
            schedule.end_time = end_time
        await self._session.flush()
        return schedule

    async def soft_delete(self, schedule: FixedSchedule) -> None:
        schedule.archived_at = datetime.now(UTC)
        await self._session.flush()

    async def count_active(self, user_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(FixedSchedule)
            .where(
                FixedSchedule.user_id == user_id,
                FixedSchedule.archived_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_fixed_schedule_repo(session: SessionDep) -> FixedScheduleRepo:
    return FixedScheduleRepo(session)
