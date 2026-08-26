"""`report_re_engagement.py` 의 쿼리 조립 — 실 Postgres (근거 대장 §7.2, A3).

판정 로직(`_re_engaged`)은 `test_report_re_engagement.py` 가 순수 함수로 고정한다.
여기서는 "앵커 도래" 게이트와 그룹·결정 필터가 fake 로는 안 잡히는 WHERE 절 회귀를
검증한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from scripts.report_re_engagement import (
    _fetch_anchor_arrived_attempts,
    _fetch_goal_sibling_success_starts,
)
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

_NOW = datetime(2026, 8, 1, 9, 0, tzinfo=KST)


async def _seed_user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="재관여율 테스트 유저"))
    await session.flush()
    return user_id


async def _seed_execution(
    session: AsyncSession, *, user_id: UUID, goal_id: UUID | None = None
) -> UUID:
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="재관여율 테스트 카드",
            target_date=_NOW.date(),
            goal_id=goal_id,
        )
    )
    await session.flush()

    block_id = uuid4()
    session.add(
        ScheduledBlock(
            id=block_id,
            user_id=user_id,
            action_item_id=action_item_id,
            start_at=_NOW,
            end_at=_NOW + timedelta(minutes=30),
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
            plan_start_at=_NOW,
            plan_end_at=_NOW + timedelta(minutes=30),
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
    user_decision: str,
    anchor_at: datetime | None,
) -> None:
    session.add(
        RecoveryAttempt(
            id=uuid4(),
            user_id=user_id,
            execution_id=execution_id,
            recovery_option_group=option_group,
            recovery_strategy_type="NANO_STEP",
            user_decision=user_decision,
            recovery_decided_at=_NOW,
            re_engagement_anchor_at=anchor_at,
        )
    )
    await session.flush()


async def test_excludes_future_anchor_and_null_anchor_and_non_adopted(
    real_db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(real_db_session)

    arrived = await _seed_execution(real_db_session, user_id=user_id)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=arrived,
        option_group="PARK",
        user_decision="accepted",
        anchor_at=_NOW - timedelta(days=1),
    )

    future = await _seed_execution(real_db_session, user_id=user_id)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=future,
        option_group="PARK",
        user_decision="accepted",
        anchor_at=_NOW + timedelta(days=1),  # 아직 안 옴 — 제외돼야
    )

    no_anchor = await _seed_execution(real_db_session, user_id=user_id)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=no_anchor,
        option_group="PARK",
        user_decision="accepted",
        anchor_at=None,  # S8 이전 — 제외돼야
    )

    rejected = await _seed_execution(real_db_session, user_id=user_id)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=rejected,
        option_group="PARK",
        user_decision="rejected",  # 미채택 — 제외돼야
        anchor_at=_NOW - timedelta(days=1),
    )

    reschedule = await _seed_execution(real_db_session, user_id=user_id)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=reschedule,
        option_group="RESCHEDULE",  # 대상 그룹 아님 — 제외돼야
        user_decision="accepted",
        anchor_at=_NOW - timedelta(days=1),
    )

    rows = await _fetch_anchor_arrived_attempts(real_db_session, _NOW)

    assert len(rows) == 1
    assert rows[0].anchor_at == _NOW - timedelta(days=1)


async def test_carry_over_group_is_included(real_db_session: AsyncSession) -> None:
    user_id = await _seed_user(real_db_session)
    execution_id = await _seed_execution(real_db_session, user_id=user_id)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=execution_id,
        option_group="CARRY_OVER",
        user_decision="edited",
        anchor_at=_NOW - timedelta(hours=1),
    )

    rows = await _fetch_anchor_arrived_attempts(real_db_session, _NOW)

    assert len(rows) == 1


async def test_goal_sibling_success_starts_scopes_to_requested_goals_and_success_statuses(
    real_db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(real_db_session)
    goal_id = uuid4()
    other_goal_id = uuid4()
    real_db_session.add(Goal(id=goal_id, user_id=user_id, title="같은 계보 테스트 목표"))
    real_db_session.add(Goal(id=other_goal_id, user_id=user_id, title="무관 목표"))
    await real_db_session.flush()

    sibling_action_item_id = uuid4()
    real_db_session.add(
        ActionItem(
            id=sibling_action_item_id,
            user_id=user_id,
            title="같은 계보 카드",
            target_date=_NOW.date(),
            goal_id=goal_id,
        )
    )
    await real_db_session.flush()
    block_id = uuid4()
    real_db_session.add(
        ScheduledBlock(
            id=block_id,
            user_id=user_id,
            action_item_id=sibling_action_item_id,
            start_at=_NOW,
            end_at=_NOW + timedelta(minutes=30),
        )
    )
    await real_db_session.flush()
    success_at = _NOW + timedelta(hours=1)
    real_db_session.add(
        ExecutionEvent(
            id=uuid4(),
            action_item_id=sibling_action_item_id,
            scheduled_block_id=block_id,
            user_id=user_id,
            plan_start_at=success_at,
            plan_end_at=success_at + timedelta(minutes=30),
            completion_status="done",
        )
    )
    await real_db_session.flush()

    starts = await _fetch_goal_sibling_success_starts(real_db_session, {goal_id})

    assert starts == {goal_id: [success_at]}
    assert other_goal_id not in starts
