"""집중 정지 '열림' 판정을 실 Postgres 로 고정한다 (today-5, sched-14).

열림 = 지연분(`resume_delay_minutes`)이 아직 비어 있다. 6h cron 이 '6시간 안에 안 돌아옴'
(`resumed_after_interrupt=False`)으로 표시해도 사용자가 [▶ 계속] 을 누르기 전까지는 열린
정지다 — 예전처럼 닫힌 것으로 보면 저녁의 [계속] 이 409 로 영영 실패한다.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.interruption_event import InterruptionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.execution_repo import ExecutionRepo
from reaction_backend.schemas.common import now_kst
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _execution(s: AsyncSession) -> ExecutionEvent:
    uid = uuid.uuid4()
    s.add(User(id=uid, email=f"pause+{uid}@test.local", name="pause"))
    await s.flush()
    card = ActionItem(
        id=uuid.uuid4(),
        user_id=uid,
        title="정지 카드",
        target_date=now_kst().date(),
        category="study",
        source="manual",
        status="in_progress",
        estimated_minutes=60,
    )
    s.add(card)
    await s.flush()
    start = now_kst() - timedelta(hours=12)
    block = ScheduledBlock(
        id=uuid.uuid4(),
        user_id=uid,
        action_item_id=card.id,
        start_at=start,
        end_at=start + timedelta(hours=1),
        block_status="started",
        source="ai_plan",
    )
    s.add(block)
    await s.flush()
    return await ExecutionRepo(s).create_execution(
        user_id=uid, action_item_id=card.id, block=block, started_at=start
    )


async def _pause(
    s: AsyncSession,
    execution: ExecutionEvent,
    *,
    resumed: bool | None,
    delay: int | None,
    kind: str = "user_pause",
) -> uuid.UUID:
    row = InterruptionEvent(
        id=uuid.uuid4(),
        user_id=execution.user_id,
        execution_id=execution.id,
        interruption_type=kind,
        resumed_after_interrupt=resumed,
        resume_delay_minutes=delay,
    )
    s.add(row)
    await s.flush()
    return row.id


async def test_resolver_marked_pause_is_still_open(real_db_session: AsyncSession) -> None:
    s = real_db_session
    repo = ExecutionRepo(s)
    execution = await _execution(s)
    await _pause(s, execution, resumed=True, delay=5)  # 재개한 정지 — 닫힘
    await _pause(s, execution, resumed=None, delay=None, kind="external_alert")  # 정지 아님
    stale = await _pause(s, execution, resumed=False, delay=None)  # 6h cron 표시, 미재개

    found = await repo.get_open_pause(execution.id)

    assert found is not None and found.id == stale


async def test_settled_pauses_are_closed(real_db_session: AsyncSession) -> None:
    s = real_db_session
    repo = ExecutionRepo(s)
    execution = await _execution(s)
    await _pause(s, execution, resumed=True, delay=5)
    await _pause(s, execution, resumed=False, delay=600)  # cron 표시 뒤 [계속] 으로 마감

    assert await repo.get_open_pause(execution.id) is None
