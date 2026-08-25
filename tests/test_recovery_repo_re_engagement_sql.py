"""`RecoveryRepo.list_due_re_engagement` — 실 Postgres (근거 대장 §6.2 T2).

T2 알림(다음날 morning_brief 재관여 슬롯)의 재료 쿼리다 — KST 달력일 경계가 틀리면
전날/다음날로 새거나 조용히 놓친다. 시드 헬퍼는 `test_recovery_repo_lineage.py` 와 같은
정신(각 테스트가 필요한 만큼만, 진짜 INSERT)으로 독립 구성한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

_BASE_AT = datetime(2026, 8, 1, 9, 0, tzinfo=KST)


async def _seed_user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="재관여 테스트 유저"))
    await session.flush()
    return user_id


async def _seed_execution(session: AsyncSession, *, user_id: UUID) -> UUID:
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="재관여 테스트 카드",
            target_date=_BASE_AT.date(),
        )
    )
    await session.flush()

    block_id = uuid4()
    session.add(
        ScheduledBlock(
            id=block_id,
            user_id=user_id,
            action_item_id=action_item_id,
            start_at=_BASE_AT,
            end_at=_BASE_AT + timedelta(minutes=30),
        )
    )
    await session.flush()

    execution_id = uuid4()
    session.add(
        ExecutionEvent(
            id=execution_id,
            action_item_id=action_item_id,
            scheduled_block_id=block_id,
            user_id=user_id,
            plan_start_at=_BASE_AT,
            plan_end_at=_BASE_AT + timedelta(minutes=30),
            completion_status="failed",
        )
    )
    await session.flush()
    return execution_id


async def _seed_attempt(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    option_group: str,
    anchor_at: datetime | None,
) -> UUID:
    attempt_id = uuid4()
    session.add(
        RecoveryAttempt(
            id=attempt_id,
            user_id=user_id,
            execution_id=execution_id,
            recovery_option_group=option_group,
            recovery_strategy_type="NANO_STEP",
            user_decision="accepted",
            recovery_decided_at=_BASE_AT,
            re_engagement_anchor_at=anchor_at,
        )
    )
    await session.flush()
    return attempt_id


async def test_returns_attempt_anchored_to_the_target_day(real_db_session: AsyncSession) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    execution_id = await _seed_execution(real_db_session, user_id=user_id)
    anchor = datetime(2026, 8, 2, 9, 0, tzinfo=KST)  # PARK 앵커 — 다음날 09:00
    attempt_id = await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=execution_id,
        option_group="PARK",
        anchor_at=anchor,
    )

    due = await repo.list_due_re_engagement(user_id, anchor.date())

    assert [a.id for a in due] == [attempt_id]


async def test_excludes_the_day_before_and_after(real_db_session: AsyncSession) -> None:
    """KST 달력일 경계 — 앵커 전날·다음날 조회는 만나지 않는다."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    execution_id = await _seed_execution(real_db_session, user_id=user_id)
    anchor = datetime(2026, 8, 2, 9, 0, tzinfo=KST)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=execution_id,
        option_group="PARK",
        anchor_at=anchor,
    )

    assert await repo.list_due_re_engagement(user_id, anchor.date() - timedelta(days=1)) == []
    assert await repo.list_due_re_engagement(user_id, anchor.date() + timedelta(days=1)) == []


async def test_boundary_just_before_midnight_kst_is_still_the_earlier_day(
    real_db_session: AsyncSession,
) -> None:
    """23:59:59 KST 는 그날 — 자정을 넘겨야 다음날로 넘어간다."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    execution_id = await _seed_execution(real_db_session, user_id=user_id)
    anchor = datetime(2026, 8, 2, 23, 59, 59, tzinfo=KST)
    attempt_id = await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=execution_id,
        option_group="CARRY_OVER",
        anchor_at=anchor,
    )

    same_day = await repo.list_due_re_engagement(user_id, anchor.date())
    next_day = await repo.list_due_re_engagement(user_id, anchor.date() + timedelta(days=1))

    assert [a.id for a in same_day] == [attempt_id]
    assert next_day == []


async def test_excludes_other_users_and_null_anchor(real_db_session: AsyncSession) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    other_user_id = await _seed_user(real_db_session)
    execution_id = await _seed_execution(real_db_session, user_id=user_id)
    other_execution_id = await _seed_execution(real_db_session, user_id=other_user_id)
    anchor = datetime(2026, 8, 2, 9, 0, tzinfo=KST)
    # 다른 사용자의 같은 날 앵커 — user_id 스코프 밖.
    await _seed_attempt(
        real_db_session,
        user_id=other_user_id,
        execution_id=other_execution_id,
        option_group="PARK",
        anchor_at=anchor,
    )
    # 앵커 없는(RESCHEDULE/DOWNSCOPE) 결정 — anchor IS NULL 이라 대상이 아니다.
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=execution_id,
        option_group="RESCHEDULE",
        anchor_at=None,
    )

    assert await repo.list_due_re_engagement(user_id, anchor.date()) == []
