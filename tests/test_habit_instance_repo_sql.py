"""HabitInstanceRepo 실 Postgres 핀 — 두 번 탭·cron 과의 겹침에서도 카운터가 맞는가.

- 동시에 온 두 체크가 같은 값을 읽고 둘 다 +1 을 쓰면 한 번이 사라졌다(읽고-더하고-쓰기).
- "있나 보고 → 넣기" get-or-create 는 동시 두 요청이 UNIQUE 위반 500 을 냈다.
- 이번 주 채우기(`ensure_for_week`)는 멱등이고 쌓인 `done_count` 를 덮지 않는다.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.user import User
from reaction_backend.repositories.habit_instance_repo import HabitInstanceRepo
from tests.conftest import DB_AVAILABLE

_WEEK = date(2026, 9, 14)


def _habit(user_id: uuid.UUID, *, freq: int = 3, archived: bool = False) -> Habit:
    h = Habit(
        user_id=user_id,
        title="스트레칭",
        category="health",
        frequency_per_week=freq,
        target_count=freq,
        minutes_per_session=10,
        time_preference="anytime",
        priority_level=3,
    )
    if archived:
        from reaction_backend.schemas.common import now_kst

        h.archived_at = now_kst()
    return h


async def _seed_user(session: Any) -> uuid.UUID:
    uid = uuid.uuid4()
    session.add(User(id=uid, email=f"habit+{uid}@test.local", name="habit sql"))
    await session.flush()
    return uid


# ── 롤백 격리 세션(flush 만) ─────────────────────────────────────────────


async def test_ensure_for_week_fills_active_habits_once(real_db_session: Any) -> None:
    uid = await _seed_user(real_db_session)
    live = _habit(uid, freq=4)
    gone = _habit(uid, archived=True)
    real_db_session.add_all([live, gone])
    await real_db_session.flush()
    repo = HabitInstanceRepo(real_db_session)

    await repo.ensure_for_week(uid, _WEEK)
    [inst] = await repo.list_for_user_week(uid, _WEEK)
    assert (inst.habit_id, inst.target_count, inst.done_count) == (live.id, 4, 0)

    await repo.increment_done(inst)
    await repo.ensure_for_week(uid, _WEEK)  # 다시 불러도 1행, done 그대로
    rows = await repo.list_for_user_week(uid, _WEEK)
    assert [(r.id, r.done_count) for r in rows] == [(inst.id, 1)]


async def test_increment_caps_and_decrement_floors(real_db_session: Any) -> None:
    uid = await _seed_user(real_db_session)
    habit = _habit(uid, freq=2)
    real_db_session.add(habit)
    await real_db_session.flush()
    repo = HabitInstanceRepo(real_db_session)
    inst = await repo.create_or_get_for_week(habit.id, _WEEK, 2)

    for _ in range(3):
        await repo.increment_done(inst)
    assert inst.done_count == 2
    for _ in range(3):
        await repo.decrement_done(inst)
    assert inst.done_count == 0


async def test_sync_week_target_moves_only_that_week(real_db_session: Any) -> None:
    uid = await _seed_user(real_db_session)
    habit = _habit(uid, freq=5)
    real_db_session.add(habit)
    await real_db_session.flush()
    repo = HabitInstanceRepo(real_db_session)
    last = await repo.create_or_get_for_week(habit.id, date(2026, 9, 7), 5)
    this = await repo.create_or_get_for_week(habit.id, _WEEK, 5)
    for _ in range(4):
        await repo.increment_done(this)

    await repo.sync_week_target(habit.id, _WEEK, 2)

    assert (this.target_count, this.done_count) == (2, 2)
    await real_db_session.refresh(last)
    assert last.target_count == 5


# ── 실 동시성 — 커넥션 두 개(커밋이 필요해 스스로 치운다) ─────────────────


async def _sessionmaker() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from reaction_backend.config import get_settings
    from reaction_backend.db.session import normalize_async_url

    engine = create_async_engine(
        normalize_async_url(get_settings().database_url), poolclass=NullPool
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _cleanup(sm: Any, uid: uuid.UUID) -> None:
    async with sm() as s:
        await s.execute(
            text(
                "DELETE FROM habit_instances WHERE habit_id IN "
                "(SELECT id FROM habits WHERE user_id = :u)"
            ),
            {"u": uid},
        )
        await s.execute(text("DELETE FROM habits WHERE user_id = :u"), {"u": uid})
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        await s.commit()


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_two_concurrent_checks_both_count() -> None:
    agen = _sessionmaker()
    sm = await anext(agen)
    try:
        async with sm() as s:
            uid = await _seed_user(s)
            habit = _habit(uid, freq=3)
            s.add(habit)
            await s.flush()
            inst = await HabitInstanceRepo(s).create_or_get_for_week(habit.id, _WEEK, 3)
            inst_id = inst.id
            await s.commit()

        async def tap() -> None:
            async with sm() as s:
                repo = HabitInstanceRepo(s)
                loaded = await repo.get_for_user(uid, inst_id)
                assert loaded is not None
                await asyncio.sleep(0.3)  # 두 요청이 모두 done=0 을 읽은 뒤에 쓰게
                await repo.increment_done(loaded)
                await asyncio.sleep(0.2)
                await s.commit()

        await asyncio.wait_for(asyncio.gather(tap(), tap()), timeout=30)

        async with sm() as s:
            final = await HabitInstanceRepo(s).get_for_user(uid, inst_id)
            assert final is not None
            done = final.done_count
    finally:
        await _cleanup(sm, uid)
        await agen.aclose()

    assert done == 2


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_concurrent_get_or_create_makes_one_row_without_error() -> None:
    agen = _sessionmaker()
    sm = await anext(agen)
    try:
        async with sm() as s:
            uid = await _seed_user(s)
            habit = _habit(uid, freq=3)
            s.add(habit)
            await s.commit()
            habit_id = habit.id

        async def make() -> uuid.UUID:
            async with sm() as s:
                inst = await HabitInstanceRepo(s).create_or_get_for_week(habit_id, _WEEK, 3)
                await asyncio.sleep(0.3)  # 첫 INSERT 가 커밋되기 전에 두 번째가 넣게
                await s.commit()
                return inst.id

        ids = await asyncio.wait_for(asyncio.gather(make(), make()), timeout=30)

        async with sm() as s:
            count = (
                await s.execute(
                    text("SELECT count(*) FROM habit_instances WHERE habit_id = :h"),
                    {"h": habit_id},
                )
            ).scalar_one()
    finally:
        await _cleanup(sm, uid)
        await agen.aclose()

    assert ids[0] == ids[1]
    assert count == 1
