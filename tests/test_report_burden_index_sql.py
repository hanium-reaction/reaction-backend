"""`report_burden_index.py` 의 쿼리 조립 — 실 Postgres (근거 대장 §7.2, E6).

판정 로직(`_rejection_rate`/`_reflection_non_response_rate`)은 순수 함수 테스트가
고정한다. 여기서는 두 fetch 함수가 시간 창·상태를 fake 로는 안 잡히는 WHERE 절/조인
회귀 없이 올바르게 거르는지 검증한다 — 특히 `reflectable_from()` 은 `plan_start_at`
과 `actual_start_at` 중 나중 값을 쓰는 계산식이라, 현재 `completion_status` 로
필터링하면 안 된다는 게 핵심 회귀 지점이다.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from scripts.report_burden_index import _fetch_recent_decisions, _fetch_recent_reflectable
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

_NOW = datetime(2026, 8, 8, 9, 0, tzinfo=KST)
_SINCE = _NOW - timedelta(days=7)


async def _seed_user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="부담 지표 테스트 유저"))
    await session.flush()
    return user_id


async def _seed_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    plan_start_at: datetime,
    completion_status: str = "in_progress",
    system_failure_reason: str | None = None,
) -> UUID:
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="부담 지표 테스트 카드",
            target_date=plan_start_at.date(),
            system_failure_reason=system_failure_reason,
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
    execution_id = uuid4()
    session.add(
        ExecutionEvent(
            id=execution_id,
            action_item_id=action_item_id,
            scheduled_block_id=block_id,
            user_id=user_id,
            plan_start_at=plan_start_at,
            plan_end_at=plan_start_at + timedelta(minutes=30),
            completion_status=completion_status,
        )
    )
    await session.flush()
    return execution_id


async def _seed_attempt(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    user_decision: str,
    recovery_decided_at: datetime | None,
) -> None:
    session.add(
        RecoveryAttempt(
            id=uuid4(),
            user_id=user_id,
            execution_id=execution_id,
            recovery_option_group="DOWNSCOPE",
            recovery_strategy_type="NANO_STEP",
            user_decision=user_decision,
            recovery_decided_at=recovery_decided_at,
        )
    )
    await session.flush()


async def test_fetch_recent_decisions_excludes_pending_and_out_of_window(
    real_db_session: AsyncSession,
) -> None:
    """창 밖·미결정은 빼고, 창 안의 확정 결정만 센다.

    ⚠️ 이 fetch 는 **전 사용자**를 집계하는 리포트 쿼리다(그게 정상이다). 결과 전체를
    내 시드와 정확히 일치시키면 **깨끗한 CI DB 에서만 통과하고 데이터가 남은 개발 DB
    에서는 실패한다.** 시드 **전후의 증분**만 본다.
    """
    before = Counter(await _fetch_recent_decisions(real_db_session, since=_SINCE))
    user_id = await _seed_user(real_db_session)
    in_window = await _seed_execution(real_db_session, user_id=user_id, plan_start_at=_NOW)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=in_window,
        user_decision="rejected",
        recovery_decided_at=_NOW - timedelta(days=1),
    )

    before_window = await _seed_execution(
        real_db_session, user_id=user_id, plan_start_at=_NOW - timedelta(days=10)
    )
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=before_window,
        user_decision="rejected",
        recovery_decided_at=_NOW - timedelta(days=10),
    )

    still_pending = await _seed_execution(real_db_session, user_id=user_id, plan_start_at=_NOW)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=still_pending,
        user_decision="pending",
        recovery_decided_at=None,
    )

    decisions = await _fetch_recent_decisions(real_db_session, since=_SINCE)

    assert Counter(decisions) - before == Counter(["rejected"])


async def test_fetch_recent_reflectable_uses_reflectable_from_not_current_status(
    real_db_session: AsyncSession,
) -> None:
    """체크인으로 completion_status 가 바뀐 카드도 분모에 남아야 한다.

    `reflectable_from()` = greatest(plan_start_at, actual_start_at) 는 그 실행이
    회고 창에 들어온 시각이지 지금 상태가 아니다 — 성공적으로 체크인된(=완료) 카드를
    현재 상태로 걸러내면 분모가 무응답 카드로만 쏠려 무응답률이 과대평가된다.
    """
    before = Counter(await _fetch_recent_reflectable(real_db_session, since=_SINCE))
    user_id = await _seed_user(real_db_session)
    checked_in = await _seed_execution(
        real_db_session, user_id=user_id, plan_start_at=_NOW, completion_status="done"
    )
    skipped = await _seed_execution(
        real_db_session,
        user_id=user_id,
        plan_start_at=_NOW,
        completion_status="in_progress",
        system_failure_reason="reflection_skipped",
    )

    reasons = await _fetch_recent_reflectable(real_db_session, since=_SINCE)
    added = Counter(reasons) - before

    assert sum(added.values()) == 2
    assert added["reflection_skipped"] == 1
    assert added[None] == 1
    assert checked_in and skipped  # 시드가 실제로 둘 다 만들어졌다는 참조(미사용 변수 방지)


async def test_fetch_recent_reflectable_excludes_before_window(
    real_db_session: AsyncSession,
) -> None:
    """창 이전의 실행은 분모에 들어오지 않는다.

    ⚠️ 이 fetch 는 **전 사용자**를 집계하는 리포트 쿼리다(그게 정상이다). 결과 전체를
    내 시드와 정확히 일치시키면 **깨끗한 CI DB 에서만 통과하고 데이터가 남은 개발 DB
    에서는 실패한다.** 시드 **전후의 증분**만 본다.
    """
    before = Counter(await _fetch_recent_reflectable(real_db_session, since=_SINCE))
    user_id = await _seed_user(real_db_session)
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        plan_start_at=_NOW - timedelta(days=10),
        system_failure_reason="reflection_skipped",
    )

    reasons = await _fetch_recent_reflectable(real_db_session, since=_SINCE)

    assert Counter(reasons) - before == Counter()
