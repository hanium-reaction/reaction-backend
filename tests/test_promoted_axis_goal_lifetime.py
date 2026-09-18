"""만다라 축에서 올린 목표의 수명 (goals-7).

- 사용자가 축에서 직접 올린 목표(`proposed`)는 14일 만료 cron 이 말없이 보관하지 않는다.
- 올린 목표를 지웠으면(보관) 만다라가 "이미 학기 목표로 올린 축" 배지를 계속 달지 않는다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.user import User
from reaction_backend.repositories.goal_repo import GoalRepo
from reaction_backend.schemas.common import now_kst
from tests.conftest import FakeGoalRepo
from tests.test_mandala_tree_route import _goal, _seed_full_tree


def _db_goal(user_id: Any, *, title: str, status: str = "proposed", **kw: Any) -> Goal:
    return Goal(
        id=uuid4(),
        user_id=user_id,
        title=title,
        category="other",
        goal_tier=kw.pop("goal_tier", "focus"),
        status=status,
        priority_level=3,
        **kw,
    )


async def test_expiry_skips_goals_promoted_from_a_live_axis(real_db_session: Any) -> None:
    s = real_db_session
    uid = uuid4()
    s.add(User(id=uid, email=f"promote+{uid}@test.local", name="axis"))
    await s.flush()
    old = now_kst() - timedelta(days=30)
    ultimate = _db_goal(uid, title="궁극", status="active", goal_tier="parked", is_ultimate=True)
    promoted = _db_goal(uid, title="축에서 올린 목표", created_at=old)
    interview_only = _db_goal(uid, title="인터뷰가 뽑은 목표", created_at=old)
    s.add_all([ultimate, promoted, interview_only])
    await s.flush()
    root = GoalNode(
        id=uuid4(),
        goal_id=ultimate.id,
        title="궁극",
        node_type="core",
        depth=0,
        order_index=0,
        is_leaf=False,
        tree_kind="mandala",
    )
    s.add(root)
    await s.flush()
    s.add(
        GoalNode(
            id=uuid4(),
            goal_id=ultimate.id,
            parent_node_id=root.id,
            title="축",
            node_type="subgoal",
            depth=1,
            order_index=0,
            is_leaf=False,
            tree_kind="mandala",
            promoted_goal_id=promoted.id,
        )
    )
    await s.flush()
    repo = GoalRepo(s)

    first = await repo.expire_stale_proposed(before=now_kst(), archived_at=now_kst())
    again = await repo.expire_stale_proposed(before=now_kst(), archived_at=now_kst())

    await s.refresh(promoted)
    await s.refresh(interview_only)
    assert (first, again) == (1, 0)  # 멱등
    assert promoted.status == "proposed" and promoted.archived_at is None
    assert interview_only.status == "archived"


def test_axis_badge_drops_when_the_promoted_goal_was_deleted(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    ultimate = _goal()
    ids = _seed_full_tree(fake_goal_repo, ultimate)
    live = _goal(is_ultimate=False, title="살아 있는 승격")
    gone = _goal(is_ultimate=False, title="지운 승격")
    gone.archived_at = now_kst()
    fake_goal_repo._items[live.id] = live
    fake_goal_repo._items[gone.id] = gone
    ids["sub1"].promoted_goal_id = live.id
    ids["sub2"].promoted_goal_id = gone.id

    tree = client.get(f"/goals/goal_{ultimate.id}/mandala").json()

    by_id = {n["nodeId"]: n for n in tree["nodes"]}
    assert by_id[f"node_{ids['sub1'].id}"]["promotedGoalId"] == f"goal_{live.id}"
    assert by_id[f"node_{ids['sub2'].id}"]["promotedGoalId"] is None


async def test_fake_expiry_matches_the_real_rule() -> None:
    """가짜 repo 도 같은 규칙 — 라우트/잡 테스트가 초록인 채로 프로덕션이 틀리지 않게."""
    repo = FakeGoalRepo()
    promoted = _goal(is_ultimate=False, title="축 승격")
    promoted.status = "proposed"
    promoted.created_at = now_kst() - timedelta(days=30)
    repo._items[promoted.id] = promoted
    ultimate = _goal()
    ids = _seed_full_tree(repo, ultimate)
    ids["sub0"].promoted_goal_id = promoted.id

    assert await repo.expire_stale_proposed(before=now_kst(), archived_at=now_kst()) == 0
    assert promoted.archived_at is None
