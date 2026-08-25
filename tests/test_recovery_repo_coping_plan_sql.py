"""`RecoveryRepo.create_attempt` 의 v3 코핑 플랜 컬럼 — 실 Postgres (acknowledgment/v3 승격).

`obstacle`/`coping_clause`/`acknowledgment` 는 이번에 처음 쓰기 경로가 생긴 컬럼이라
실제로 INSERT·조회가 되는지 확인한다. 시드 헬퍼는 `test_recovery_repo_lineage.py` 와
같은 정신(각 테스트가 필요한 만큼만, 진짜 INSERT)으로 독립 구성한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

_BASE_AT = datetime(2026, 8, 1, 9, 0, tzinfo=KST)


async def _seed_execution(session: AsyncSession) -> tuple[UUID, UUID]:
    user_id = uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="코핑 플랜 테스트 유저"))
    await session.flush()

    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="코핑 플랜 테스트 카드",
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
    return user_id, execution_id


async def test_coping_plan_fields_round_trip(real_db_session: AsyncSession) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id, execution_id = await _seed_execution(real_db_session)

    attempt = await repo.create_attempt(
        user_id=user_id,
        execution_id=execution_id,
        option_group="DOWNSCOPE",
        strategy_type="NANO_STEP",
        suggested_action_text="책상에 앉으면 표현 하나만 소리 내어 읽어봐요",
        trigger_tag="AVOIDANCE",
        llm_fallback_used=False,
        prompt_version="3",
        obstacle="소리 내어 읽는 게 괜히 부담스러울 수 있어요",
        coping_clause="그마저 부담스러우면 눈으로만 한 번 읽어봐요",
        acknowledgment="누구나 시작이 막막할 때가 있어요",
    )

    fetched = await repo.list_attempts(user_id, execution_id)

    assert len(fetched) == 1
    assert fetched[0].id == attempt.id
    assert fetched[0].obstacle == "소리 내어 읽는 게 괜히 부담스러울 수 있어요"
    assert fetched[0].coping_clause == "그마저 부담스러우면 눈으로만 한 번 읽어봐요"
    assert fetched[0].acknowledgment == "누구나 시작이 막막할 때가 있어요"


async def test_coping_plan_fields_default_to_null(real_db_session: AsyncSession) -> None:
    """v2 배치(호출부가 안 넘기는 경우) — 세 컬럼 다 NULL 로 남는다."""
    repo = RecoveryRepo(real_db_session)
    user_id, execution_id = await _seed_execution(real_db_session)

    await repo.create_attempt(
        user_id=user_id,
        execution_id=execution_id,
        option_group="DOWNSCOPE",
        strategy_type="NANO_STEP",
        suggested_action_text="핵심 2문제만 풀어봐요",
        trigger_tag="AMBIGUITY",
        llm_fallback_used=False,
        prompt_version="2",
    )

    fetched = await repo.list_attempts(user_id, execution_id)

    assert fetched[0].obstacle is None
    assert fetched[0].coping_clause is None
    assert fetched[0].acknowledgment is None
