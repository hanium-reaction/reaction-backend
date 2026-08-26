"""`report_consistency_rolling14.py` 의 쿼리 조립 — 실 Postgres (근거 대장 §7.1/§7.2).

판정 로직(`_consistency_rate`)은 `test_report_consistency_rolling14.py` 가 순수 함수로
고정한다. 여기서는 활성 사용자 스코프와 상태·창 필터가 fake 로는 안 잡히는 WHERE 절
회귀를 검증한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from scripts.report_consistency_rolling14 import _fetch_active_user_ids, _fetch_qualifying_days
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

_NOW = datetime(2026, 8, 1, 9, 0, tzinfo=KST)


async def _seed_user(
    session: AsyncSession, *, onboarding_state: str = "ACTIVE", is_anonymized: bool = False
) -> UUID:
    user_id = uuid4()
    session.add(
        User(
            id=user_id,
            email=f"{user_id}@test.local",
            name="연속성 테스트 유저",
            onboarding_state=onboarding_state,
            is_anonymized=is_anonymized,
        )
    )
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
            title="연속성 테스트 카드",
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


async def test_active_user_ids_excludes_non_active_and_anonymized(
    real_db_session: AsyncSession,
) -> None:
    active = await _seed_user(real_db_session)
    onboarding = await _seed_user(real_db_session, onboarding_state="ONBOARDING_FIRST_PLAN")
    anonymized = await _seed_user(real_db_session, is_anonymized=True)

    ids = await _fetch_active_user_ids(real_db_session)

    assert active in ids
    assert onboarding not in ids
    assert anonymized not in ids


async def test_qualifying_days_excludes_failed_only_days_and_scopes_kst_date(
    real_db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(real_db_session)
    await _seed_execution(
        real_db_session, user_id=user_id, completion_status="failed", plan_start_at=_NOW
    )
    partial_at = _NOW + timedelta(days=1)
    await _seed_execution(
        real_db_session, user_id=user_id, completion_status="partial_done", plan_start_at=partial_at
    )

    days = await _fetch_qualifying_days(real_db_session, since=_NOW)

    assert user_id in days
    assert _NOW.date() not in days[user_id], "failed 만 있는 날은 자격 없음"
    assert partial_at.date() in days[user_id]


async def test_qualifying_days_excludes_before_since(real_db_session: AsyncSession) -> None:
    user_id = await _seed_user(real_db_session)
    too_early: date = (_NOW - timedelta(days=20)).date()
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        completion_status="done",
        plan_start_at=_NOW - timedelta(days=20),
    )

    days = await _fetch_qualifying_days(real_db_session, since=_NOW)

    assert too_early not in days.get(user_id, set())
