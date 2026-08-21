"""`RecoveryRepo` 의 L1/L2 에스컬레이션 이력 조회 3종 — 실 Postgres (근거 대장 §5.1/§5.2).

- `list_lineage_outcomes_for_tag` — L2 "동일 (계보, tag_code) 3회 연속 실패"
- `list_same_card_outcomes` — L1 "동일 카드 2회 연속 실패"
- `list_recovery_results` — L1 "회복 1회 abandoned"

`orchestrator/escalation.py` 의 순수 함수는 이미 이력 리스트만 있으면 검증됐다
(`tests/test_escalation.py`). 여기서 검증하는 건 그 리스트를 **실제로 어떻게 만드는가** —
"계보" = goal_id 그룹(§5.16 SQL과 같은 정의), 다른 태그의 failed 는 동결(partial_done
취급), goal_id 가 없으면 계보는 자기 자신 하나뿐이라는 판단(L2), `recovery_abandoned_streak`
는 §5.1 표에 "동일 카드" 한정이 없어 사용자 전체 이력으로 본다는 판단(L1).

DATABASE_URL 이 없으면(로컬 기본값) 스킵 — `tests/test_recovery_evidence_sql.py` 와 같은
게이트. 시드 헬퍼도 그 파일과 같은 정신(각 테스트가 필요한 만큼만, 진짜 INSERT)으로
독립적으로 구성한다 — 테스트 파일 간에 private 헬퍼를 공유하지 않는 기존 관례를 따른다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.orchestrator.escalation import (
    L1_CONSECUTIVE_FAILURE_THRESHOLD,
    L1_RECOVERY_ABANDONED_THRESHOLD,
    L2_SAME_TAG_FAILURE_THRESHOLD,
    compute_escalation_state,
)
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.schemas.common import KST
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

_BASE_AT = datetime(2026, 8, 1, 9, 0, tzinfo=KST)


async def _seed_user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="계보 테스트 유저"))
    await session.flush()
    return user_id


async def _seed_goal(session: AsyncSession, *, user_id: UUID) -> UUID:
    goal_id = uuid4()
    session.add(Goal(id=goal_id, user_id=user_id, title="계보 테스트 목표"))
    await session.flush()
    return goal_id


async def _seed_action_item(
    session: AsyncSession, *, user_id: UUID, goal_id: UUID | None = None
) -> UUID:
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            title="계보 테스트 카드",
            target_date=_BASE_AT.date(),
            goal_id=goal_id,
        )
    )
    await session.flush()
    return action_item_id


async def _seed_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    action_item_id: UUID,
    completion_status: str,
    plan_start_at: datetime,
    tag_code: str | None = None,
) -> UUID:
    """실행 1건 + (있으면) 실패 태그 1건 시드. `plan_start_at` 으로 순서를 직접 제어한다.

    ⚠️ `created_at`(server_default `now()`) 으로 정렬하지 않는 이유가 바로 이거다 — 이
    픽스처의 모든 시드가 **한 트랜잭션 안**에서 일어나 `now()` 가 전부 같은 값을 준다.
    `plan_start_at` 은 애플리케이션이 명시적으로 주는 값이라 테스트에서 결정적이다.
    """
    block_id = uuid4()
    plan_end_at = plan_start_at + timedelta(minutes=30)
    session.add(
        ScheduledBlock(
            id=block_id,
            user_id=user_id,
            action_item_id=action_item_id,
            start_at=plan_start_at,
            end_at=plan_end_at,
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
            plan_end_at=plan_end_at,
            completion_status=completion_status,
        )
    )
    await session.flush()

    if tag_code is not None:
        session.add(ExecutionFailureTag(id=uuid4(), execution_id=execution_id, tag_code=tag_code))
        await session.flush()

    return execution_id


async def _seed_recovery_attempt(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    recovery_result: str,
    recovery_decided_at: datetime,
) -> None:
    """회복 결정 1건 시드 — `list_recovery_results` 는 카드/전략 무관하게 결과만 본다.

    `recovery_strategy_type` 은 마이그레이션이 이미 커밋해 둔 마스터 시드(NANO_STEP)를
    그대로 참조한다(FK) — 이 테스트의 관심사가 아니라 뭐든 상관없다.
    """
    session.add(
        RecoveryAttempt(
            id=uuid4(),
            user_id=user_id,
            execution_id=execution_id,
            recovery_option_group="DOWNSCOPE",
            recovery_strategy_type="NANO_STEP",
            user_decision="accepted",
            recovery_decided_at=recovery_decided_at,
            recovery_result=recovery_result,
        )
    )
    await session.flush()


async def test_same_action_item_consecutive_same_tag_failures(
    real_db_session: AsyncSession,
) -> None:
    """goal_id 없는 카드 — 계보는 자기 자신뿐. 3건 모두 같은 태그 failed → 그대로 3건."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    for i in range(3):
        await _seed_execution(
            real_db_session,
            user_id=user_id,
            action_item_id=action_item_id,
            completion_status="failed",
            plan_start_at=_BASE_AT + timedelta(days=i),
            tag_code="DISTRACTION",
        )

    outcomes = await repo.list_lineage_outcomes_for_tag(user_id, action_item_id, "DISTRACTION")

    assert outcomes == ["failed", "failed", "failed"]


async def test_done_resets_and_is_most_recent_first(real_db_session: AsyncSession) -> None:
    """`plan_start_at` 내림차순 — 가장 최근(done)이 먼저 오고, 그게 스트릭을 리셋한다."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT,
        tag_code="DISTRACTION",
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=1),
        tag_code="DISTRACTION",
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="done",
        plan_start_at=_BASE_AT + timedelta(days=2),
    )

    outcomes = await repo.list_lineage_outcomes_for_tag(user_id, action_item_id, "DISTRACTION")

    assert outcomes == ["done", "failed", "failed"]


async def test_different_tag_failure_is_frozen_not_reset_or_counted(
    real_db_session: AsyncSession,
) -> None:
    """다른 태그의 failed 는 이 태그 관점에서 무관한 사건 — `partial_done` 처럼 동결.

    가장 최근(t3, 매칭) → 중간(t2, 다른 태그 → 동결) → 가장 오래됨(t1, 매칭). 동결이라
    카운트도 리셋도 안 하므로 `compute_consecutive_failure_count` 로는 여전히 2 다.
    """
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT,
        tag_code="DISTRACTION",
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=1),
        tag_code="HARD_TO_START",
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=2),
        tag_code="DISTRACTION",
    )

    outcomes = await repo.list_lineage_outcomes_for_tag(user_id, action_item_id, "DISTRACTION")

    assert outcomes == ["failed", "partial_done", "failed"]

    from reaction_backend.orchestrator.escalation import compute_consecutive_failure_count

    assert compute_consecutive_failure_count(outcomes) == 2


async def test_goal_lineage_spans_multiple_action_items(real_db_session: AsyncSession) -> None:
    """같은 goal_id 를 가진 다른 action_item 의 실행도 계보에 포함된다."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    goal_id = await _seed_goal(real_db_session, user_id=user_id)
    card_a = await _seed_action_item(real_db_session, user_id=user_id, goal_id=goal_id)
    card_b = await _seed_action_item(real_db_session, user_id=user_id, goal_id=goal_id)
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=card_a,
        completion_status="failed",
        plan_start_at=_BASE_AT,
        tag_code="DISTRACTION",
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=card_b,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=1),
        tag_code="DISTRACTION",
    )

    outcomes = await repo.list_lineage_outcomes_for_tag(user_id, card_a, "DISTRACTION")

    assert outcomes == ["failed", "failed"]


async def test_no_goal_id_lineage_is_self_only(real_db_session: AsyncSession) -> None:
    """goal_id 가 없으면 계보가 없다 — 태그가 매칭돼도 다른 카드의 실행은 안 섞인다."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    target = await _seed_action_item(real_db_session, user_id=user_id)
    sibling = await _seed_action_item(real_db_session, user_id=user_id)
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=target,
        completion_status="failed",
        plan_start_at=_BASE_AT,
        tag_code="DISTRACTION",
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=sibling,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=1),
        tag_code="DISTRACTION",
    )

    outcomes = await repo.list_lineage_outcomes_for_tag(user_id, target, "DISTRACTION")

    assert outcomes == ["failed"]


async def test_in_progress_execution_is_excluded(real_db_session: AsyncSession) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="in_progress",
        plan_start_at=_BASE_AT + timedelta(days=1),
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT,
        tag_code="DISTRACTION",
    )

    outcomes = await repo.list_lineage_outcomes_for_tag(user_id, action_item_id, "DISTRACTION")

    assert outcomes == ["failed"]


async def test_unknown_or_foreign_action_item_returns_empty(
    real_db_session: AsyncSession,
) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    other_user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=other_user_id)

    assert await repo.list_lineage_outcomes_for_tag(user_id, uuid4(), "DISTRACTION") == []
    assert await repo.list_lineage_outcomes_for_tag(user_id, action_item_id, "DISTRACTION") == []


async def test_limit_caps_the_window(real_db_session: AsyncSession) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    for i in range(5):
        await _seed_execution(
            real_db_session,
            user_id=user_id,
            action_item_id=action_item_id,
            completion_status="failed",
            plan_start_at=_BASE_AT + timedelta(days=i),
            tag_code="DISTRACTION",
        )

    outcomes = await repo.list_lineage_outcomes_for_tag(
        user_id, action_item_id, "DISTRACTION", limit=3
    )

    assert len(outcomes) == 3


async def test_wires_into_escalation_state_as_l2_at_the_threshold(
    real_db_session: AsyncSession,
) -> None:
    """리포지토리 출력이 실제로 `compute_escalation_state` 에 흘러 L2 를 낸다 — 배선 확인.

    §5.2 임계값(3회 연속)을 그대로 채워 경계에서 정확히 L2 가 되는지 본다.
    """
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    for i in range(L2_SAME_TAG_FAILURE_THRESHOLD):
        await _seed_execution(
            real_db_session,
            user_id=user_id,
            action_item_id=action_item_id,
            completion_status="failed",
            plan_start_at=_BASE_AT + timedelta(days=i),
            tag_code="DISTRACTION",
        )

    outcomes = await repo.list_lineage_outcomes_for_tag(user_id, action_item_id, "DISTRACTION")
    state = compute_escalation_state(
        same_card_outcomes_most_recent_first=[],
        same_tag_outcomes_most_recent_first=outcomes,
        recovery_decisions_most_recent_first=[],
        recovery_results_most_recent_first=[],
    )

    assert state.level == "L2"


# ═══════════════════ list_same_card_outcomes — L1 "동일 카드" ═══════════════════


async def test_same_card_outcomes_ignores_tags_and_other_cards(
    real_db_session: AsyncSession,
) -> None:
    """계보·태그 무관 — 이 action_item_id 자기 자신의 실행만, 다른 태그 실패도 그대로 센다."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    other_card = await _seed_action_item(real_db_session, user_id=user_id)
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT,
        tag_code="DISTRACTION",
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=1),
        tag_code="HARD_TO_START",  # list_lineage_outcomes_for_tag 와 달리 동결 없이 그대로 count
    )
    await _seed_execution(  # 다른 카드 — 안 섞여야 한다
        real_db_session,
        user_id=user_id,
        action_item_id=other_card,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=2),
    )

    outcomes = await repo.list_same_card_outcomes(user_id, action_item_id)

    assert outcomes == ["failed", "failed"]


async def test_same_card_outcomes_orders_by_plan_start_at_desc_and_excludes_in_progress(
    real_db_session: AsyncSession,
) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT,
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="in_progress",
        plan_start_at=_BASE_AT + timedelta(days=1),
    )
    await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="done",
        plan_start_at=_BASE_AT + timedelta(days=2),
    )

    outcomes = await repo.list_same_card_outcomes(user_id, action_item_id)

    assert outcomes == ["done", "failed"]


async def test_same_card_outcomes_wires_into_escalation_state_as_l1_at_the_threshold(
    real_db_session: AsyncSession,
) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    for i in range(L1_CONSECUTIVE_FAILURE_THRESHOLD):
        await _seed_execution(
            real_db_session,
            user_id=user_id,
            action_item_id=action_item_id,
            completion_status="failed",
            plan_start_at=_BASE_AT + timedelta(days=i),
        )

    outcomes = await repo.list_same_card_outcomes(user_id, action_item_id)
    state = compute_escalation_state(
        same_card_outcomes_most_recent_first=outcomes,
        same_tag_outcomes_most_recent_first=[],
        recovery_decisions_most_recent_first=[],
        recovery_results_most_recent_first=[],
    )

    assert state.level == "L1"


# ═══════════════════ list_recovery_results — L1 "회복 1회 abandoned" ═══════════════════


async def test_recovery_results_is_user_global_not_scoped_to_one_card(
    real_db_session: AsyncSession,
) -> None:
    """§5.1 표에 "동일 카드" 한정이 없다 — 서로 다른 카드/실행에 걸친 결정도 다 같이 본다."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    card_a = await _seed_action_item(real_db_session, user_id=user_id)
    card_b = await _seed_action_item(real_db_session, user_id=user_id)
    exec_a = await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=card_a,
        completion_status="failed",
        plan_start_at=_BASE_AT,
    )
    exec_b = await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=card_b,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=1),
    )
    await _seed_recovery_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=exec_a,
        recovery_result="abandoned",
        recovery_decided_at=_BASE_AT,
    )
    await _seed_recovery_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=exec_b,
        recovery_result="abandoned",
        recovery_decided_at=_BASE_AT + timedelta(days=1),
    )

    outcomes = await repo.list_recovery_results(user_id)

    assert outcomes == ["abandoned", "abandoned"]


async def test_recovery_results_excludes_pending_and_orders_by_decided_at_desc(
    real_db_session: AsyncSession,
) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)

    exec1 = await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT,
    )
    exec2 = await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=1),
    )
    await _seed_execution(  # pending 대조군 — 이 실행엔 attempt 자체를 안 만든다
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT + timedelta(days=2),
    )
    await _seed_recovery_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=exec1,
        recovery_result="completed",
        recovery_decided_at=_BASE_AT,
    )
    await _seed_recovery_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=exec2,
        recovery_result="abandoned",
        recovery_decided_at=_BASE_AT + timedelta(days=1),
    )

    outcomes = await repo.list_recovery_results(user_id)

    assert outcomes == ["abandoned", "completed"]


async def test_recovery_results_wires_into_escalation_state_as_l1_at_the_threshold(
    real_db_session: AsyncSession,
) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    action_item_id = await _seed_action_item(real_db_session, user_id=user_id)
    exec_id = await _seed_execution(
        real_db_session,
        user_id=user_id,
        action_item_id=action_item_id,
        completion_status="failed",
        plan_start_at=_BASE_AT,
    )
    for _ in range(L1_RECOVERY_ABANDONED_THRESHOLD):
        await _seed_recovery_attempt(
            real_db_session,
            user_id=user_id,
            execution_id=exec_id,
            recovery_result="abandoned",
            recovery_decided_at=_BASE_AT,
        )

    outcomes = await repo.list_recovery_results(user_id)
    state = compute_escalation_state(
        same_card_outcomes_most_recent_first=[],
        same_tag_outcomes_most_recent_first=[],
        recovery_decisions_most_recent_first=[],
        recovery_results_most_recent_first=outcomes,
    )

    assert state.level == "L1"
