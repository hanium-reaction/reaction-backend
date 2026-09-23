"""cron sweep 의 사용자 단위 실패 격리 — **실 Postgres 세션**으로 고정.

왜 이 파일이 필요한가:
`test_scheduler_sweeps.py` · `test_notify_sweeps.py` · `test_habit_instances_sweep.py` 는
`_FakeSession` 을 쓴다. fake 는 commit/rollback 횟수만 세므로 실 `AsyncSession` 의 두 성질을
원리적으로 못 본다.

1. DB 예외가 나면 세션이 aborted 가 된다 — rollback 없이 다음 사용자로 넘어가면 그 뒤 전원이
   `PendingRollbackError` 로 죽고, 배치 말미 commit 까지 터져 앞 사용자의 결과도 사라진다
   (morning_brief / weekly_review sweep 의 예전 모습).
2. rollback 은 세션이 들고 있던 ORM 객체를 전부 만료시킨다 — 그 뒤 루프가 `user.id` 를 읽으면
   비동기 세션은 lazy refresh 를 못 해 `MissingGreenlet` 로 죽고, except 의 로그가 같은 속성을
   다시 읽다가 루프 밖으로 튄다(알림 sweep·habit_instances 의 예전 모습 — rollback 은 있었다).

각 테스트는 첫 사용자에서 **실제 DB 오류**(unique 위반 또는 SQL 오류)를 내고, 뒤 두 사용자가
끝까지 처리되는지 본다.

세션은 `join_transaction_mode="create_savepoint"` 로 바깥 트랜잭션에 묶는다 — sweep 이 부르는
`commit()`/`rollback()` 은 SAVEPOINT 만 풀거나 되돌리고, 테스트가 끝나면 바깥 트랜잭션을
롤백해 DB 에 아무것도 남지 않는다(`conftest.real_db_session` 은 commit 을 못 부르는 픽스처라
여기서는 쓸 수 없다).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.action_item_repo import ActionItemRepo
from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo
from reaction_backend.repositories.execution_repo import ExecutionRepo
from reaction_backend.repositories.habit_instance_repo import HabitInstanceRepo
from reaction_backend.repositories.habit_repo import HabitRepo
from reaction_backend.repositories.notification_repo import NotificationRepo
from reaction_backend.repositories.notification_send_repo import NotificationSendRepo
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.repositories.review_repo import ReviewRepo
from reaction_backend.scheduler import habit_instances, notify_sweeps, sweeps
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

# 먼 미래로 둔다 — 전 사용자 대상 쿼리(pre_card 블록 조회)가 다른 데이터와 섞이지 않게.
SUNDAY_EVENING = datetime(2031, 6, 15, 21, 0, tzinfo=KST)  # 2031-06-15 은 일요일
MORNING = datetime(2031, 6, 16, 8, 0, tzinfo=KST)


@pytest.fixture
async def savepoint_session() -> AsyncIterator[AsyncSession]:
    """commit/rollback 을 불러도 되는 실 세션 — 모듈 docstring 참고."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from reaction_backend.config import get_settings
    from reaction_backend.db.session import normalize_async_url

    engine = create_async_engine(
        normalize_async_url(get_settings().database_url), poolclass=NullPool
    )
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            # 앱 sessionmaker 와 같은 옵션(expire_on_commit=False, autoflush=False) — 차이는
            # 바깥 트랜잭션에 SAVEPOINT 로 합류한다는 것뿐이다.
            session = AsyncSession(
                bind=conn,
                expire_on_commit=False,
                autoflush=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                await session.close()
                await outer.rollback()
    finally:
        await engine.dispose()


async def _seed_users(session: AsyncSession, n: int = 3) -> list[UUID]:
    """이메일 앞자리로 순서를 고정한 활성 사용자 n명 — 첫 사용자가 실패 역할이다."""
    ids = [uuid4() for _ in range(n)]
    for i, uid in enumerate(ids):
        session.add(
            User(
                id=uid,
                email=f"{i}-{uid}@sweep-isolation.test",
                name=f"격리 테스트 {i}",
                onboarding_state="ACTIVE",
                tone_mode="gentle",
            )
        )
    await session.flush()
    await session.commit()
    return ids


class _ScopedUserRepo:
    """`UserRepo.list_active` 와 같은 ORM 행을 **이 테스트가 만든 사용자로만** 좁혀 돌려준다.

    핵심은 실 세션이 로드한 ORM 객체를 넘긴다는 것 — rollback 이 이 객체들을 만료시키는 게
    재현하려는 버그 자체다.
    """

    def __init__(self, session: AsyncSession, ids: Sequence[UUID]) -> None:
        self._session = session
        self._ids = list(ids)

    async def list_active(self) -> list[User]:
        stmt = select(User).where(User.id.in_(self._ids)).order_by(User.email)
        return list((await self._session.execute(stmt)).scalars().all())


async def _fail_in_db(session: AsyncSession) -> None:
    """실제 SQL 오류 — 세션(SAVEPOINT)을 aborted 상태로 만든다."""
    await session.execute(text("SELECT 1 / 0"))


async def _count(session: AsyncSession, model: Any, user_ids: Sequence[UUID]) -> int:
    stmt = select(func.count()).select_from(model).where(model.user_id.in_(list(user_ids)))
    return int((await session.execute(stmt)).scalar_one())


# ─────────────────────────── morning_brief ───────────────────────────


class _RacingBriefRepo(DailyBriefRepo):
    """겹친 실행 재현 — `bad` 사용자에 한해, 존재 확인(`get_by_date`)과 INSERT 사이에 다른
    실행이 같은 날 브리프를 먼저 넣은 상황을 만든다 → `uq_daily_briefs_user_date` 위반."""

    def __init__(self, session: AsyncSession, bad: UUID) -> None:
        super().__init__(session)
        self._bad = bad

    async def create(self, user_id: UUID, brief_date: Any, **kwargs: Any) -> DailyBrief:
        if user_id == self._bad:
            self._session.add(
                DailyBrief(
                    user_id=user_id,
                    brief_date=brief_date,
                    headline_text="다른 실행이 먼저 만든 브리프",
                    expires_at=kwargs["expires_at"],
                )
            )
            await self._session.flush()
        return await super().create(user_id, brief_date, **kwargs)


async def test_morning_brief_sweep_keeps_other_users_briefs_after_a_db_error(
    savepoint_session: AsyncSession,
) -> None:
    """한 사용자의 unique 위반이 나머지 사용자의 브리프를 날리지 않는다.

    예전: rollback 없이 다음 사용자로 넘어가 전원 `PendingRollbackError`, 마지막 일괄 commit
    까지 터져 sweep 자체가 예외로 끝났다 — 그날 브리프를 만드는 곳은 이 job 하나뿐이다.
    """
    session = savepoint_session
    bad, *good = await _seed_users(session)

    result = await sweeps.run_morning_brief_sweep(
        MORNING,
        user_repo=_ScopedUserRepo(session, [bad, *good]),  # type: ignore[arg-type]
        action_repo=ActionItemRepo(session),
        brief_repo=_RacingBriefRepo(session, bad),
        session=session,
    )

    assert result == sweeps.SweepResult(total=3, ok=2, failed=1)
    assert await _count(session, DailyBrief, good) == 2
    # 실패한 사용자의 부분 쓰기는 되돌려졌다 — 다음 폴이 깨끗하게 다시 시도한다.
    assert await _count(session, DailyBrief, [bad]) == 0


# ─────────────────────────── weekly_review ───────────────────────────


class _BrokenReviewRepo(ReviewRepo):
    def __init__(self, session: AsyncSession, bad: UUID) -> None:
        super().__init__(session)
        self._bad = bad

    async def collect_execution_stats(self, user_id: UUID, start_dt: Any, end_dt: Any) -> Any:
        if user_id == self._bad:
            await _fail_in_db(self._session)
        return await super().collect_execution_stats(user_id, start_dt, end_dt)


async def test_weekly_review_sweep_persists_other_users_after_a_db_error(
    savepoint_session: AsyncSession,
) -> None:
    session = savepoint_session
    bad, *good = await _seed_users(session)

    result = await sweeps.run_weekly_review_sweep(
        SUNDAY_EVENING,
        user_repo=_ScopedUserRepo(session, [bad, *good]),  # type: ignore[arg-type]
        review_repo=_BrokenReviewRepo(session, bad),
        session=session,
    )

    assert result == sweeps.SweepResult(total=3, ok=2, failed=1)
    assert await _count(session, PeriodSummary, good) == 2


# ─────────────────────────── 알림 sweep 3종 ───────────────────────────


class _BrokenNotificationRepo(NotificationRepo):
    def __init__(self, session: AsyncSession, bad: UUID) -> None:
        super().__init__(session)
        self._bad = bad

    async def get_by_user(self, user_id: UUID) -> NotificationSetting | None:
        if user_id == self._bad:
            await _fail_in_db(self._session)
        return await super().get_by_user(user_id)


class _NoopSender:
    async def send(self, subscription: Any, payload: Any) -> Any:  # pragma: no cover - 미도달
        raise AssertionError("구독이 없는 사용자에게 발송하면 안 된다")


async def test_evening_notify_sweep_survives_rollback_expiring_loaded_users(
    savepoint_session: AsyncSession,
) -> None:
    """rollback 뒤 다음 사용자로 넘어갈 수 있다 — 만료된 `user.id` 를 다시 읽지 않는다.

    뒤 두 사용자는 알림 설정 행이 없어 skip 이 정상 결과다. 예전 코드는 rollback 직후
    `user.id` 에서 `MissingGreenlet` 이 나 루프 밖으로 튀었다.
    """
    session = savepoint_session
    bad, *good = await _seed_users(session)

    result = await notify_sweeps.run_evening_reflection_notify_sweep(
        SUNDAY_EVENING,
        user_repo=_ScopedUserRepo(session, [bad, *good]),  # type: ignore[arg-type]
        notif_repo=_BrokenNotificationRepo(session, bad),
        execution_repo=ExecutionRepo(session),
        send_repo=NotificationSendRepo(session),
        sender=_NoopSender(),  # type: ignore[arg-type]
        session=session,
        clock=lambda: SUNDAY_EVENING,
    )

    assert result == notify_sweeps.NotifySweepResult(total=3, sent=0, skipped=2, failed=1)


async def test_morning_brief_notify_sweep_survives_rollback_expiring_loaded_users(
    savepoint_session: AsyncSession,
) -> None:
    session = savepoint_session
    bad, *good = await _seed_users(session)

    result = await notify_sweeps.run_morning_brief_notify_sweep(
        MORNING,
        user_repo=_ScopedUserRepo(session, [bad, *good]),  # type: ignore[arg-type]
        notif_repo=_BrokenNotificationRepo(session, bad),
        recovery_repo=RecoveryRepo(session),
        action_repo=ActionItemRepo(session),
        send_repo=NotificationSendRepo(session),
        sender=_NoopSender(),  # type: ignore[arg-type]
        session=session,
        clock=lambda: MORNING,
    )

    assert result == notify_sweeps.NotifySweepResult(total=3, sent=0, skipped=2, failed=1)


class _ScopedExecutionRepo(ExecutionRepo):
    """pre_card 블록 조회(전 사용자 대상)를 이 테스트 사용자로 좁힌다 — 실 쿼리는 그대로."""

    def __init__(self, session: AsyncSession, user_ids: Sequence[UUID]) -> None:
        super().__init__(session)
        self._user_ids = set(user_ids)

    async def list_blocks_starting_between(
        self, *, start: datetime, end: datetime
    ) -> list[ScheduledBlock]:
        blocks = await super().list_blocks_starting_between(start=start, end=end)
        return [b for b in blocks if b.user_id in self._user_ids]


async def test_pre_card_notify_sweep_survives_rollback_expiring_loaded_blocks(
    savepoint_session: AsyncSession,
) -> None:
    """블록 단위 루프도 같다 — rollback 뒤 `block.user_id`·`block.action_item.title` 을 안 읽는다."""
    session = savepoint_session
    user_ids = await _seed_users(session)
    start = MORNING + timedelta(minutes=4)  # [now+2분, now+7분) 창 안
    for i, uid in enumerate(user_ids):
        item_id = uuid4()
        session.add(
            ActionItem(id=item_id, user_id=uid, title=f"카드 {i}", target_date=start.date())
        )
        await session.flush()
        session.add(
            ScheduledBlock(
                id=uuid4(),
                user_id=uid,
                action_item_id=item_id,
                # 순서 고정 — 조회가 start_at 정렬이라 첫 사용자의 블록이 먼저 온다.
                start_at=start + timedelta(seconds=i),
                end_at=start + timedelta(minutes=30),
            )
        )
    await session.flush()
    await session.commit()

    result = await notify_sweeps.run_pre_card_notify_sweep(
        MORNING,
        execution_repo=_ScopedExecutionRepo(session, user_ids),
        notif_repo=_BrokenNotificationRepo(session, user_ids[0]),
        send_repo=NotificationSendRepo(session),
        sender=_NoopSender(),  # type: ignore[arg-type]
        session=session,
        clock=lambda: MORNING,
    )

    assert result == notify_sweeps.NotifySweepResult(total=3, sent=0, skipped=2, failed=1)


# ─────────────────────────── habit_instances ───────────────────────────


class _BrokenHabitRepo(HabitRepo):
    def __init__(self, session: AsyncSession, bad: UUID) -> None:
        super().__init__(session)
        self._bad = bad

    async def list_active(self, user_id: UUID) -> list[Habit]:
        if user_id == self._bad:
            await _fail_in_db(self._session)
        return await super().list_active(user_id)


async def test_habit_instances_sweep_creates_rows_for_users_after_a_db_error(
    savepoint_session: AsyncSession,
) -> None:
    session = savepoint_session
    bad, *good = await _seed_users(session)
    for uid in (bad, *good):
        session.add(Habit(id=uuid4(), user_id=uid, title="러닝", frequency_per_week=3))
    await session.flush()
    await session.commit()
    week_start = MORNING.date() - timedelta(days=MORNING.weekday())

    result = await habit_instances.run_habit_instances_sweep(
        week_start,
        user_repo=_ScopedUserRepo(session, [bad, *good]),  # type: ignore[arg-type]
        habit_repo=_BrokenHabitRepo(session, bad),
        instance_repo=HabitInstanceRepo(session),
        session=session,
    )

    assert result == habit_instances.HabitSweepResult(total=3, ok=2, failed=1, created=2)
    stmt = (
        select(func.count())
        .select_from(HabitInstance)
        .join(Habit, Habit.id == HabitInstance.habit_id)
        .where(Habit.user_id.in_(good), HabitInstance.week_start == week_start)
    )
    assert int((await session.execute(stmt)).scalar_one()) == 2
