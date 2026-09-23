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
from reaction_backend.db.models.goal import Goal
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


async def _seed_goal(session: AsyncSession, *, user_id: UUID, status: str = "active") -> UUID:
    goal_id = uuid4()
    session.add(Goal(id=goal_id, user_id=user_id, title="재관여 테스트 목표", status=status))
    await session.flush()
    return goal_id


async def _seed_execution(
    session: AsyncSession, *, user_id: UUID, goal_id: UUID | None = None
) -> UUID:
    action_item_id = uuid4()
    session.add(
        ActionItem(
            id=action_item_id,
            user_id=user_id,
            goal_id=goal_id,
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
    recovery_result: str = "pending",
    resulting_action_item_id: UUID | None = None,
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
            recovery_result=recovery_result,
            resulting_action_item_id=resulting_action_item_id,
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


# ─────────── 더는 "다시 보러 갈까요?"가 할 말이 아닌 회복 (recovery-14 · sched-10 · data-8) ───────────

_ANCHOR = datetime(2026, 8, 2, 9, 0, tzinfo=KST)


async def _seed_resulting_card(
    session: AsyncSession,
    *,
    user_id: UUID,
    status: str = "planned",
    archived: bool = False,
    system_failure_reason: str | None = None,
) -> UUID:
    card_id = uuid4()
    session.add(
        ActionItem(
            id=card_id,
            user_id=user_id,
            title="재관여 테스트 카드 · 이어서",
            target_date=_ANCHOR.date(),
            status=status,
            source="recovery_carryover",
            archived_at=_ANCHOR - timedelta(hours=1) if archived else None,
            system_failure_reason=system_failure_reason,
        )
    )
    await session.flush()
    return card_id


async def _due_ids(repo: RecoveryRepo, user_id: UUID) -> list[UUID]:
    return [a.id for a in await repo.list_due_re_engagement(user_id, _ANCHOR.date())]


async def test_excludes_a_recovery_already_completed(real_db_session: AsyncSession) -> None:
    """아침에 이어가기 카드를 끝냈는데 08시에 그 카드를 다시 보자고 하지 않는다."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    execution_id = await _seed_execution(real_db_session, user_id=user_id)
    await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=execution_id,
        option_group="CARRY_OVER",
        anchor_at=_ANCHOR,
        recovery_result="completed",
    )

    assert await _due_ids(repo, user_id) == []


@pytest.mark.parametrize(
    ("status", "archived", "system_failure_reason", "expected_due"),
    [
        ("planned", False, None, True),  # 아직 할 일 — 대상
        ("done", False, None, False),  # 이미 끝낸 카드
        ("over_done", False, None, False),
        ("planned", True, None, False),  # 목표 완료·계획 교체로 치워진 카드
        ("failed", True, "reflection_skipped", True),  # 못 하고 만료 — 다시 챙길 이유 그대로
        ("failed", False, None, True),  # 해 보다 못 끝냄(abandoned 경로) — 다시 권한다
    ],
)
async def test_carry_over_card_state_decides_the_push(
    real_db_session: AsyncSession,
    status: str,
    archived: bool,
    system_failure_reason: str | None,
    expected_due: bool,
) -> None:
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    execution_id = await _seed_execution(real_db_session, user_id=user_id)
    card_id = await _seed_resulting_card(
        real_db_session,
        user_id=user_id,
        status=status,
        archived=archived,
        system_failure_reason=system_failure_reason,
    )
    attempt_id = await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=execution_id,
        option_group="CARRY_OVER",
        anchor_at=_ANCHOR,
        resulting_action_item_id=card_id,
    )

    assert await _due_ids(repo, user_id) == ([attempt_id] if expected_due else [])


@pytest.mark.parametrize(
    ("goal_status", "goal_archived", "expected_due"),
    [
        ("active", False, True),
        ("completed", False, False),  # 목표를 끝냈다
        ("archived", False, False),
        ("active", True, False),  # 목표를 지웠다(soft delete)
    ],
)
async def test_park_follows_the_original_cards_goal(
    real_db_session: AsyncSession, goal_status: str, goal_archived: bool, expected_due: bool
) -> None:
    """PARK 는 결과 카드가 없어 원본 카드의 목표 상태로만 가린다."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    goal_id = await _seed_goal(real_db_session, user_id=user_id, status=goal_status)
    if goal_archived:
        goal = await real_db_session.get(Goal, goal_id)
        assert goal is not None
        goal.archived_at = _ANCHOR - timedelta(days=1)
        await real_db_session.flush()
    execution_id = await _seed_execution(real_db_session, user_id=user_id, goal_id=goal_id)
    attempt_id = await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=execution_id,
        option_group="PARK",
        anchor_at=_ANCHOR,
    )

    assert await _due_ids(repo, user_id) == ([attempt_id] if expected_due else [])


async def test_park_without_a_goal_is_still_due(real_db_session: AsyncSession) -> None:
    """목표 없는 카드(인박스/수동)의 PARK 는 그대로 대상 — 결과가 영영 pending 이어도."""
    repo = RecoveryRepo(real_db_session)
    user_id = await _seed_user(real_db_session)
    execution_id = await _seed_execution(real_db_session, user_id=user_id)
    attempt_id = await _seed_attempt(
        real_db_session,
        user_id=user_id,
        execution_id=execution_id,
        option_group="PARK",
        anchor_at=_ANCHOR,
    )

    assert await _due_ids(repo, user_id) == [attempt_id]
