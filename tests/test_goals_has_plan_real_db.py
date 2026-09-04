"""목표에 **이번 주기 계획이 있는가** — 실 Postgres (#440).

계획 승인은 인터뷰가 뽑은 목표를 **전부** `active` 로 승격하는데
(`first_plan_adapter.materialize_goals`), 계획은 heaviest **하나**에만 만들어진다.
그래서 `status == "active"` 는 "계획이 있다" 를 뜻하지 않는다 — 로컬 실측으로
**계획 없는 active 목표가 24건**이었고, 그래서 "미계획" 배지가 사라졌다.

판정을 상태가 아니라 **계획 트리의 존재**로 옮긴 것이 이 파일이 지키는 계약이다.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.user import User
from reaction_backend.repositories.goal_repo import GoalRepo
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="계획 유무 테스트 유저"))
    await session.flush()
    return user_id


async def _seed_goal(session: AsyncSession, *, user_id: uuid.UUID, status: str) -> uuid.UUID:
    goal = Goal()
    goal.user_id = user_id
    goal.title = f"목표-{uuid.uuid4().hex[:6]}"
    goal.category = "study"
    goal.goal_tier = "focus"
    goal.status = status
    session.add(goal)
    await session.flush()
    return goal.id


async def _seed_node(
    session: AsyncSession,
    *,
    goal_id: uuid.UUID,
    tree_kind: str = "plan",
    archived: bool = False,
) -> None:
    node = GoalNode()
    node.goal_id = goal_id
    node.title = "노드"
    # CHECK 제약: depth 0 = core(비leaf) / 1 = subgoal(비leaf) / 2 = leaf(leaf).
    node.node_type = "core"
    node.depth = 0
    node.order_index = 0
    node.is_leaf = False
    node.tree_kind = tree_kind
    if archived:
        from datetime import UTC, datetime

        node.archived_at = datetime.now(UTC)
    session.add(node)
    await session.flush()


# ── 판정 ────────────────────────────────────────────────────────────────────


async def test_goal_with_a_plan_tree_counts_as_planned(real_db_session: AsyncSession) -> None:
    user_id = await _seed_user(real_db_session)
    goal_id = await _seed_goal(real_db_session, user_id=user_id, status="active")
    await _seed_node(real_db_session, goal_id=goal_id)

    assert await GoalRepo(real_db_session).goal_ids_with_plan([goal_id]) == {goal_id}


async def test_active_goal_without_a_plan_is_unplanned(real_db_session: AsyncSession) -> None:
    """⚠️ **이 케이스가 이 이슈의 전부다.** 승인이 계획 없는 목표까지 active 로 올린다."""
    user_id = await _seed_user(real_db_session)
    goal_id = await _seed_goal(real_db_session, user_id=user_id, status="active")

    assert await GoalRepo(real_db_session).goal_ids_with_plan([goal_id]) == set()


async def test_proposed_goal_is_unplanned(real_db_session: AsyncSession) -> None:
    """`proposed` 는 정의상 계획이 없다 — 같은 판정이 두 경우를 모두 덮는다."""
    user_id = await _seed_user(real_db_session)
    goal_id = await _seed_goal(real_db_session, user_id=user_id, status="proposed")

    assert await GoalRepo(real_db_session).goal_ids_with_plan([goal_id]) == set()


async def test_archived_tree_only_counts_as_unplanned(real_db_session: AsyncSession) -> None:
    """보관된 트리만 있으면 **미계획이 맞다.**

    재승인 시 이전 트리가 보관된다(`list_nodes` 주석). 그 상태는 "이전 주기 계획은 끝났고
    이번 주기 계획은 아직" 이므로 사용자에게는 계획이 없는 것과 같다.
    """
    user_id = await _seed_user(real_db_session)
    goal_id = await _seed_goal(real_db_session, user_id=user_id, status="active")
    await _seed_node(real_db_session, goal_id=goal_id, archived=True)

    assert await GoalRepo(real_db_session).goal_ids_with_plan([goal_id]) == set()


async def test_mandala_tree_does_not_count_as_a_plan(real_db_session: AsyncSession) -> None:
    """만다라 73칸(`tree_kind='mandala'`)은 계획 트리가 아니다."""
    user_id = await _seed_user(real_db_session)
    goal_id = await _seed_goal(real_db_session, user_id=user_id, status="active")
    await _seed_node(real_db_session, goal_id=goal_id, tree_kind="mandala")

    assert await GoalRepo(real_db_session).goal_ids_with_plan([goal_id]) == set()


async def test_mixed_batch_separates_planned_from_unplanned(real_db_session: AsyncSession) -> None:
    """한 사용자의 여러 목표 중 계획 있는 것만 골라낸다 — 실제 화면의 모양."""
    user_id = await _seed_user(real_db_session)
    planned = await _seed_goal(real_db_session, user_id=user_id, status="active")
    await _seed_node(real_db_session, goal_id=planned)
    unplanned_active = await _seed_goal(real_db_session, user_id=user_id, status="active")
    unplanned_proposed = await _seed_goal(real_db_session, user_id=user_id, status="proposed")

    got = await GoalRepo(real_db_session).goal_ids_with_plan(
        [planned, unplanned_active, unplanned_proposed]
    )
    assert got == {planned}


async def test_empty_input_does_not_query(real_db_session: AsyncSession) -> None:
    assert await GoalRepo(real_db_session).goal_ids_with_plan([]) == set()


# ── N+1 회귀 ────────────────────────────────────────────────────────────────


async def test_query_count_is_constant_in_the_number_of_goals(
    real_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """목표가 몇 개든 **쿼리는 한 번**이다.

    카드마다 묻는 N+1 은 목록 화면에서 바로 체감된다 — 축 배지가 같은 이유로 배치 조회다.
    """
    user_id = await _seed_user(real_db_session)
    goal_ids = [
        await _seed_goal(real_db_session, user_id=user_id, status="active") for _ in range(6)
    ]
    await _seed_node(real_db_session, goal_id=goal_ids[0])

    repo = GoalRepo(real_db_session)
    calls = 0
    original = real_db_session.execute

    async def _counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(real_db_session, "execute", _counting)
    got = await repo.goal_ids_with_plan(goal_ids)

    assert got == {goal_ids[0]}
    assert calls == 1, f"목표 {len(goal_ids)}개에 쿼리 {calls}회 — N+1 이다"
