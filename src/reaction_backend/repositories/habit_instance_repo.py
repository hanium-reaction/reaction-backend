"""HabitInstance repository — S27 주별 인스턴스 (Issue #22).

규칙:
- 사용자 scope 는 habits → user_id 조인.
- (habit_id, week_start) UNIQUE — DB 설계서. 중복 INSERT 시도 X (`create_or_get_for_week`).
- 생성 경로: POST /habits·만다라 반복형 전환(등록한 그 주) + `scheduler/habit_instances.py`
  cron(주별) + `GET /habit-instances`·체크(cron 이 아직 안 돈 새 주, 월요일 00:00~00:05).
  전부 get-or-create(`ON CONFLICT DO NOTHING`)라 겹치거나 동시에 와도 1행 — `done_count` 가
  쌓인 행을 덮어쓰지 않는다.
- `done_count` 증감은 한 줄 UPDATE 로 DB 가 계산한다 — 읽고-더하고-쓰기면 두 번 탭이 같은
  값을 읽어 한 번이 사라진다.
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import ColumnElement, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.db.session import get_db


class HabitInstanceRepo:
    """HabitInstance 영속화."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user_week(self, user_id: UUID, week_start: date) -> list[HabitInstance]:
        """해당 사용자의 그 주 모든 active habit 의 인스턴스.

        `joinedload(habit)` — 오늘 어젠다(`today.py:_habit_schema`)가 제목을 읽으려면
        `instance.habit.title` 이 이미 로드돼 있어야 한다(비동기 세션은 lazy load 를
        지원하지 않아 접근 시 MissingGreenlet 로 죽는다).
        """
        stmt = (
            select(HabitInstance)
            .join(Habit, Habit.id == HabitInstance.habit_id)
            .options(joinedload(HabitInstance.habit))
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

    async def list_recent_for_habit(
        self, habit_id: UUID, before_week: date, limit: int = 3
    ) -> list[HabitInstance]:
        """habit 의 week_start <= before_week 인스턴스를 최신순 limit 개 (S22 페널티 감지)."""
        stmt = (
            select(HabitInstance)
            .where(
                HabitInstance.habit_id == habit_id,
                HabitInstance.week_start <= before_week,
            )
            .order_by(HabitInstance.week_start.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

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
        """(habit_id, week_start) UNIQUE — 이미 있으면 그것 반환, 없으면 생성.

        `ON CONFLICT DO NOTHING` 으로 넣고 다시 읽는다 — "있나 보고 → 넣기" 는 두 요청(두 번
        탭한 체크, 체크와 cron)이 같이 "없음" 을 보고 둘 다 넣어 UNIQUE 위반 500 이 났다.
        이미 있으면 target/done 을 건드리지 않는다.
        """
        existing = await self.get_for_week(habit_id, week_start)
        if existing is not None:
            return existing
        await self._session.execute(
            pg_insert(HabitInstance)
            .values(
                habit_id=habit_id,
                week_start=week_start,
                target_count=target_count,
                done_count=0,
            )
            .on_conflict_do_nothing(constraint=_UQ_HABIT_WEEK)
        )
        created = await self.get_for_week(habit_id, week_start)
        if created is None:  # pragma: no cover — 방금 넣었거나 이미 있던 행이다
            raise RuntimeError("habit_instance get-or-create returned no row")
        return created

    async def ensure_for_week(self, user_id: UUID, week_start: date) -> None:
        """이 사용자의 활성 습관마다 그 주 인스턴스가 없으면 만든다(한 문장, 멱등).

        새 주 인스턴스는 월요일 00:05 cron 이 만든다 — 그 전에(또는 그 사용자 처리가 실패해)
        오늘 화면을 열면 인스턴스가 없어 체크가 아무 일도 안 했다. 조회가 cron 과 **같은 값**
        (`habit.target_count`)으로 채워 둔다. 이미 있는 행은 `ON CONFLICT DO NOTHING` 으로
        그대로 — cron 과 겹쳐도 1행이다.
        """
        rows = select(
            Habit.id,
            literal(week_start),
            Habit.target_count,
            literal(0),
        ).where(Habit.user_id == user_id, Habit.archived_at.is_(None))
        await self._session.execute(
            pg_insert(HabitInstance)
            .from_select(["habit_id", "week_start", "target_count", "done_count"], rows)
            .on_conflict_do_nothing(constraint=_UQ_HABIT_WEEK)
        )

    async def increment_done(self, instance: HabitInstance) -> HabitInstance:
        """1회 달성. `target_count` 에서 멈춘다.

        상한이 없으면 중복 탭·재요청마다 카운트가 끝없이 쌓인다. 이미 목표치에 도달한 뒤 다시
        호출해도(중복 탭 재현) 상태가 그대로 유지되는 멱등 동작이 된다. 계산은 DB 가 한 줄
        UPDATE 로 한다 — 동시에 온 두 체크가 같은 값을 읽고 둘 다 +1 을 써서 한 번이 사라지지
        않게.
        """
        return await self._bump(
            instance,
            func.least(HabitInstance.done_count + 1, HabitInstance.target_count),
        )

    async def decrement_done(self, instance: HabitInstance) -> HabitInstance:
        """1회 되돌리기(잘못 누른 체크). 0 아래로는 안 내려간다 — 다시 불러도 안전하다."""
        return await self._bump(instance, func.greatest(HabitInstance.done_count - 1, 0))

    async def sync_week_target(self, habit_id: UUID, week_start: date, target_count: int) -> None:
        """빈도를 바꾼 습관의 **그 주** 목표치를 새 값으로 — 이미 한 횟수는 새 목표에서 멈춘다.

        빈도 변경(`PATCH /habits`)·빈도 줄이기 수락이 `habits` 만 고쳐서, 이번 주 카드는 옛
        목표(예: 0/5)로 남았다. 지난 주 행은 그 주의 기록이라 건드리지 않는다. 이번 주 행이
        아직 없으면 할 일도 없다(cron·조회가 새 값으로 만든다).
        """
        await self._session.execute(
            update(HabitInstance)
            .where(HabitInstance.habit_id == habit_id, HabitInstance.week_start == week_start)
            .values(
                target_count=target_count,
                done_count=func.least(HabitInstance.done_count, target_count),
            )
            .execution_options(synchronize_session=False)
        )
        existing = await self.get_for_week(habit_id, week_start)
        if existing is not None:
            await self._session.refresh(existing)

    async def _bump(self, instance: HabitInstance, new_done: ColumnElement[int]) -> HabitInstance:
        await self._session.execute(
            update(HabitInstance)
            .where(HabitInstance.id == instance.id)
            .values(done_count=new_done)
            .execution_options(synchronize_session=False)
        )
        await self._session.refresh(instance)
        return instance


_UQ_HABIT_WEEK = "uq_habit_instances_habit_week"


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_habit_instance_repo(session: SessionDep) -> HabitInstanceRepo:
    return HabitInstanceRepo(session)
