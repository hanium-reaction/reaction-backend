"""Habit repository — S27 (Issue #22).

규칙:
- user_id scope 자동.
- soft delete only (`archived_at`).
- frequency_per_week CHECK 1~7 은 DB CheckConstraint + Pydantic 둘 다 enforce.
- habit_instance 생성은 라우터(등록 시점)와 `scheduler/habit_instances.py` cron(주별) 둘 다
  `HabitInstanceRepo.create_or_get_for_week` 로 호출.
- commit 은 호출자 책임.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.habit import Habit
from reaction_backend.db.session import get_db
from reaction_backend.schemas.common import KST


def today_kst() -> date:
    """오늘 날짜(KST). 주 경계·첫 주 목표치 계산이 모두 이 값 하나를 본다(테스트가 여기를 고정)."""
    return datetime.now(KST).date()


def current_week_start_kst() -> date:
    """이번 주 월요일 (KST 기준). habit_instances.week_start 와 매칭.

    생성(`POST /habits`·`scheduler/habit_instances` cron)과 조회(`GET /today/agenda`·
    `GET /habit-instances`)가 **전부 이 함수 하나**를 쓴다. 주 경계를 각자 재면 어긋난 주에
    행이 생겨 습관이 안 보인다 — 새 호출자도 여기로 올 것.
    """
    today = today_kst()
    return today - timedelta(days=today.weekday())  # Monday=0


def week_target(frequency_per_week: int, *, created_on: date | None, week_start: date) -> int:
    """이 주(`week_start`)의 목표 횟수 — **등록한 그 주만** 남은 날 비율로 줄인다.

    토요일에 '매일' 습관을 만들면 이번 주는 이틀뿐인데 목표가 7이라, 이틀 다 해도 2/7(절반
    미만)이었다. 그 주가 '3주 연속 미달' 의 첫 주로 세어져 실제로는 2주 만에 빈도 줄이기 카드가
    떴고, 오늘 화면엔 시작부터 닿을 수 없는 0/7 이 떴다. 그래서 등록한 주는
    `ceil(빈도 × 남은 날 / 7)`(최소 1) — 월요일에 만들면 그대로다. 그다음 주부터는 cron 이
    `habit.target_count`(= 빈도) 그대로 만든다.

    `created_on` 이 없거나(아직 저장 전 등) 이 주보다 앞이면 등록 주가 아니므로 빈도 그대로.
    """
    if created_on is None or created_on < week_start:
        return frequency_per_week
    remaining_days = 7 - created_on.weekday()
    return max(1, math.ceil(frequency_per_week * remaining_days / 7))


def first_week_target(frequency_per_week: int) -> int:
    """**오늘** 등록하는 습관의 이번 주 목표 — `week_target` 의 등록 시점 버전."""
    today = today_kst()
    return week_target(
        frequency_per_week, created_on=today, week_start=today - timedelta(days=today.weekday())
    )


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

    async def get_active_by_goal_node(self, user_id: UUID, goal_node_id: UUID) -> Habit | None:
        """이 만다라 칸에 이미 링크된 활성 습관(ADR-0008 §1) — 링크 endpoint 의 멱등 판정용."""
        stmt = select(Habit).where(
            Habit.goal_node_id == goal_node_id,
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
        goal_node_id: UUID | None = None,
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
            goal_node_id=goal_node_id,
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

    async def apply_penalty(
        self, habit: Habit, *, new_frequency: int, decided_at: datetime
    ) -> Habit:
        """S22 수락 — 빈도 재설계 적용 + 페널티 상태 기록 (#21-C)."""
        habit.frequency_per_week = new_frequency
        habit.target_count = new_frequency
        habit.last_penalty_decision = "accepted"
        habit.last_penalty_evaluated_at = decided_at
        habit.consecutive_miss_weeks = 0
        await self._session.flush()
        return habit

    async def soft_delete(self, habit: Habit) -> None:
        habit.archived_at = datetime.now(UTC)
        await self._session.flush()

    async def archive_linked_to_nodes(self, user_id: UUID, node_ids: Sequence[UUID]) -> int:
        """이 만다라 칸들에 링크된 활성 습관을 soft 보관. 반환: 보관한 수.

        궁극목표를 지우면 만다라가 사라지는데, 그 칸에서 만든 반복형 습관은 `goal_node_id`
        로만 이어져 있어 그대로 살아 있었다 — 오늘 화면에 매주 뜨고, 00:05 cron 이 새 주
        인스턴스를 만들고, 3주 뒤엔 어디서 왔는지 볼 화면도 없는 습관에 빈도 조정 제안이
        왔다. soft 보관만 한다(AGENTS §2) — 주간 기록(`habit_instances`)은 그대로 남는다.
        """
        if not node_ids:
            return 0
        stmt = select(Habit).where(
            Habit.user_id == user_id,
            Habit.goal_node_id.in_(node_ids),
            Habit.archived_at.is_(None),
        )
        habits = list((await self._session.execute(stmt)).scalars().all())
        now = datetime.now(UTC)
        for h in habits:
            h.archived_at = now
        await self._session.flush()
        return len(habits)

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
