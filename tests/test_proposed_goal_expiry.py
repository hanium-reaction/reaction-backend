"""잠정(proposed) 목표 만료 cron (#178) — TTL 지난 proposed goal 을 archived 로 전이 검증.

job 함수에 FakeGoalRepo 주입 — 룰만(LLM/DB 무관), idempotent 보장 확인.
`test_plan_draft_expiry.py` 와 같은 모양.
"""

from __future__ import annotations

from datetime import timedelta

from reaction_backend.db.models.goal import Goal
from reaction_backend.scheduler.expire_proposed_goals import (
    PROPOSED_GOAL_TTL_DAYS,
    proposed_goal_stale_before,
    run_expire_stale_proposed_goals,
)
from reaction_backend.schemas.common import now_kst
from tests.conftest import DEMO_USER_UUID, FakeGoalRepo, _FakeSession


async def _seed(
    repo: FakeGoalRepo,
    *,
    status: str,
    created_days_ago: float,
    archived: bool = False,
) -> Goal:
    goal = await repo.create(
        DEMO_USER_UUID,
        title="목표",
        category="study",
        goal_tier="focus",
        priority_level=1,
    )
    goal.status = status
    goal.created_at = now_kst() - timedelta(days=created_days_ago)
    if archived:
        goal.archived_at = now_kst()
    return goal


async def test_expire_marks_only_stale_proposed_goals() -> None:
    """TTL 지난 proposed 만 archived, TTL 이내인 proposed 는 유지."""
    repo = FakeGoalRepo()
    stale = await _seed(repo, status="proposed", created_days_ago=PROPOSED_GOAL_TTL_DAYS + 1)
    fresh = await _seed(repo, status="proposed", created_days_ago=1)

    count = await run_expire_stale_proposed_goals(_FakeSession(), repo=repo, now=now_kst())

    assert count == 1
    assert stale.status == "archived"
    assert stale.archived_at is not None
    assert fresh.status == "proposed"
    assert fresh.archived_at is None


async def test_expire_does_not_touch_active_completed_or_already_archived() -> None:
    """proposed 가 아닌 상태는 아무리 오래돼도 건드리지 않는다 — status 필터가 핵심 가드."""
    repo = FakeGoalRepo()
    active = await _seed(repo, status="active", created_days_ago=100)
    completed = await _seed(repo, status="completed", created_days_ago=100)
    already_archived = await _seed(repo, status="archived", created_days_ago=100, archived=True)

    count = await run_expire_stale_proposed_goals(_FakeSession(), repo=repo, now=now_kst())

    assert count == 0
    assert active.status == "active"
    assert completed.status == "completed"
    assert already_archived.status == "archived"


async def test_expire_boundary_is_strict_less_than() -> None:
    """정확히 경계 시각(`proposed_goal_stale_before(now)`)에 만들어진 목표는 아직 살아남는다."""
    repo = FakeGoalRepo()
    now = now_kst()
    boundary_goal = await repo.create(
        DEMO_USER_UUID, title="목표", category="study", goal_tier="focus", priority_level=1
    )
    boundary_goal.status = "proposed"
    boundary_goal.created_at = proposed_goal_stale_before(now)

    count = await run_expire_stale_proposed_goals(_FakeSession(), repo=repo, now=now)

    assert count == 0
    assert boundary_goal.status == "proposed"


async def test_expire_is_idempotent() -> None:
    """다회 실행해도 안전 — 두 번째 실행은 전이 0건."""
    repo = FakeGoalRepo()
    await _seed(repo, status="proposed", created_days_ago=PROPOSED_GOAL_TTL_DAYS + 1)

    first = await run_expire_stale_proposed_goals(_FakeSession(), repo=repo, now=now_kst())
    second = await run_expire_stale_proposed_goals(_FakeSession(), repo=repo, now=now_kst())

    assert first == 1
    assert second == 0
