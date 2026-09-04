"""`report_next_day_return.py` 의 쿼리 조립 — 실 Postgres (근거 대장 §7.3 SQL#3).

판정 로직(`_is_next_day_return`)은 `test_report_next_day_return.py` 가 순수 함수로
고정한다. 여기서는 `_fetch_days_by_status` 가 (user_id, KST 날짜) 집합을 올바르게
만드는지만 검증한다 — 특히 KST 변환과 completion_status 필터가 fake 로는 안 잡히는
회귀 지점이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from scripts.report_next_day_return import _fetch_days_by_status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

_BASE_AT = datetime(2026, 7, 21, 21, 0, tzinfo=KST)


async def _seed_user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="다음날 복귀 테스트 유저"))
    await session.flush()
    return user_id


async def _seed_execution(
    session: AsyncSession, *, user_id: UUID, completion_status: str, plan_start_at: datetime
) -> None:
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="다음날 복귀 테스트 카드",
            target_date=plan_start_at.date(),
        )
    )
    await session.flush()

    block_id = uuid4()
    session.add(
        ScheduledBlock(
            id=block_id,
            user_id=user_id,
            action_item_id=action_item_id,
            start_at=plan_start_at,
            end_at=plan_start_at + timedelta(minutes=30),
        )
    )
    await session.flush()
    session.add(
        ExecutionEvent(
            id=uuid4(),
            action_item_id=action_item_id,
            scheduled_block_id=block_id,
            user_id=user_id,
            plan_start_at=plan_start_at,
            plan_end_at=plan_start_at + timedelta(minutes=30),
            completion_status=completion_status,
        )
    )
    await session.flush()


async def test_fetch_days_by_status_scopes_to_requested_statuses(
    real_db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(real_db_session)
    await _seed_execution(
        real_db_session, user_id=user_id, completion_status="failed", plan_start_at=_BASE_AT
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        completion_status="done",
        plan_start_at=_BASE_AT + timedelta(days=1),
    )

    fail_days = await _fetch_days_by_status(real_db_session, ("failed",))
    win_days = await _fetch_days_by_status(real_db_session, ("done", "over_done"))

    assert (user_id, _BASE_AT.date()) in fail_days
    assert (user_id, _BASE_AT.date()) not in win_days
    assert (user_id, (_BASE_AT + timedelta(days=1)).date()) in win_days


async def test_fetch_days_by_status_deduplicates_same_user_same_day(
    real_db_session: AsyncSession,
) -> None:
    """같은 (사용자, 날짜)에 실패가 2건이어도 집합엔 1개만 — 몇 건인지는 이 지표와 무관.

    ⚠️ `_fetch_days_by_status` 는 **전 사용자**를 집계하는 리포트 쿼리다(그게 정상이다).
    그래서 결과 전체를 내 시드와 정확히 일치시키면 **깨끗한 CI DB 에서만 통과하고
    데이터가 남은 개발 DB 에서는 실패한다.** 시드 **전후의 증분**만 본다.
    """
    before = await _fetch_days_by_status(real_db_session, ("failed",))
    user_id = await _seed_user(real_db_session)
    await _seed_execution(
        real_db_session, user_id=user_id, completion_status="failed", plan_start_at=_BASE_AT
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(hours=2),
    )

    fail_days = await _fetch_days_by_status(real_db_session, ("failed",))

    assert fail_days - before == {(user_id, _BASE_AT.date())}
