"""`RecoveryRepo.abandon_stale` — 실 Postgres (data-7).

`test_recovery_completion.py` 는 UPDATE 문의 WHERE 를 문자열로 고정한다. 여기서는 **어떤
카드가 실제로 '포기'가 되는가**를 진짜 INSERT 로 확인한다 — 특히 목표 완료로 치워진 회복
카드가 3일 뒤 'abandoned' 로 적혀 거짓 L1 에스컬레이션을 만들던 경로.
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

# 회고 창 경계(cron 이 `pending_reflection_since(오늘)` 로 넘기는 값)
_BEFORE = datetime(2026, 8, 10, 0, 0, tzinfo=KST)
# 원본 실패와 회복 카드 날짜 — 경계보다 나흘 앞(창 밖)
_FAILED_AT = datetime(2026, 8, 6, 9, 0, tzinfo=KST)


async def _seed_adopted_recovery(
    session: AsyncSession,
    *,
    archived: bool,
    system_failure_reason: str | None = None,
) -> UUID:
    """원본 실패 실행 1건 + 채택된 회복(한 번도 시작 안 한 CARRY_OVER 카드). 반환: attempt id."""
    user_id = uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="포기 처리 테스트 유저"))
    await session.flush()

    original_id = uuid4()
    session.add(
        ActionItem(
            id=original_id,
            user_id=user_id,
            title="원본 카드",
            target_date=_FAILED_AT.date(),
            status="failed",
        )
    )
    await session.flush()
    block_id = uuid4()
    session.add(
        ScheduledBlock(
            id=block_id,
            user_id=user_id,
            action_item_id=original_id,
            start_at=_FAILED_AT,
            end_at=_FAILED_AT + timedelta(minutes=30),
            block_status="finished",
        )
    )
    await session.flush()
    execution_id = uuid4()
    session.add(
        ExecutionEvent(
            id=execution_id,
            action_item_id=original_id,
            scheduled_block_id=block_id,
            user_id=user_id,
            plan_start_at=_FAILED_AT,
            plan_end_at=_FAILED_AT + timedelta(minutes=30),
            completion_status="failed",
        )
    )
    await session.flush()

    recovery_card_id = uuid4()
    session.add(
        ActionItem(
            id=recovery_card_id,
            user_id=user_id,
            title="원본 카드 · 이어서",
            target_date=_FAILED_AT.date() + timedelta(days=1),
            status="planned",
            source="recovery_carryover",
            parent_action_item_id=original_id,
            archived_at=_FAILED_AT + timedelta(days=1) if archived else None,
            system_failure_reason=system_failure_reason,
        )
    )
    await session.flush()

    attempt_id = uuid4()
    session.add(
        RecoveryAttempt(
            id=attempt_id,
            user_id=user_id,
            execution_id=execution_id,
            recovery_option_group="CARRY_OVER",
            recovery_strategy_type="CARRYOVER_DEFAULT",
            user_decision="accepted",
            recovery_decided_at=_FAILED_AT,
            recovery_started_at=_FAILED_AT,
            resulting_action_item_id=recovery_card_id,
        )
    )
    await session.flush()
    return attempt_id


async def _result_of(session: AsyncSession, attempt_id: UUID) -> str:
    attempt = await session.get(RecoveryAttempt, attempt_id)
    assert attempt is not None
    await session.refresh(attempt)
    return attempt.recovery_result


async def test_card_cleared_by_goal_completion_is_not_abandoned(
    real_db_session: AsyncSession,
) -> None:
    """목표를 끝내며 치워진 회복 카드 — '포기'가 아니라 pending 으로 남는다(에스컬레이션 중립)."""
    attempt_id = await _seed_adopted_recovery(real_db_session, archived=True)

    await RecoveryRepo(real_db_session).abandon_stale(before=_BEFORE)

    assert await _result_of(real_db_session, attempt_id) == "pending"


async def test_card_expired_unreflected_is_still_abandoned(real_db_session: AsyncSession) -> None:
    """만료(reflection_skipped)로 보관된 카드는 정말 못 하고 지나간 것 — 종전대로 포기."""
    attempt_id = await _seed_adopted_recovery(
        real_db_session, archived=True, system_failure_reason="reflection_skipped"
    )

    await RecoveryRepo(real_db_session).abandon_stale(before=_BEFORE)

    assert await _result_of(real_db_session, attempt_id) == "abandoned"


async def test_never_started_live_card_is_still_abandoned(real_db_session: AsyncSession) -> None:
    """보관되지 않은 채 창 밖으로 지나간 회복 — 종전 동작 그대로 포기."""
    attempt_id = await _seed_adopted_recovery(real_db_session, archived=False)

    await RecoveryRepo(real_db_session).abandon_stale(before=_BEFORE)

    assert await _result_of(real_db_session, attempt_id) == "abandoned"
