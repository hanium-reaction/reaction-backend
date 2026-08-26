"""`cycle_proposal` 을 **실 Postgres 스키마 위에서** 검증 (ADR-0008 §8 "G").

가장 위험한 지점 하나만 real DB 로 확인한다: **과거 주기(archived 된 계획 트리)의 종결
action_item 이 새 주기 판정에 섞이지 않는가.** `ActionItem` 은 원본 status 불변(AGENTS §2)
이라 재생성돼도 archive 되지 않고 영구히 남는다 — `goal_node_id` 로 **현재 활성** leaf
집합에 좁히지 않으면, 목표가 한 번이라도 뭔가를 끝낸 뒤로는 "종결 카드가 있다" 가드가
영원히 참이 돼버려 판정이 무의미해진다(`cycle_proposal.py` docstring 참고).

fake session 으로는 이 버그를 못 잡는다 — `_NodeSession`/`_EntitySession` 은 seed 된 행을
그대로 돌려줄 뿐 `goal_node_id IN (...)` 필터를 실행하지 않는다.

DATABASE_URL 이 없으면 스킵 — 다른 real-DB 테스트와 같은 게이트.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.user import User
from reaction_backend.orchestrator import cycle_proposal
from reaction_backend.repositories.goal_repo import GoalRepo
from reaction_backend.schemas.common import now_kst
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _seed_goal(session: AsyncSession) -> Goal:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="주기 제안 테스트"))
    goal = Goal()
    goal.id = uuid.uuid4()
    goal.user_id = user_id
    goal.title = "영어 회화"
    goal.category = "other"
    goal.goal_tier = "focus"
    goal.status = "active"
    goal.is_ultimate = False
    session.add(goal)
    await session.flush()
    return goal


async def _seed_leaf(session: AsyncSession, goal: Goal, *, archived: bool) -> GoalNode:
    archived_at = now_kst() if archived else None
    root = GoalNode()
    root.id = uuid.uuid4()
    root.goal_id = goal.id
    root.title = goal.title
    root.node_type = "core"
    root.depth = 0
    root.order_index = 0
    root.is_leaf = False
    root.tree_kind = "plan"
    root.source = "llm"
    root.archived_at = archived_at
    session.add(root)
    leaf = GoalNode()
    leaf.id = uuid.uuid4()
    leaf.goal_id = goal.id
    leaf.parent_node_id = root.id
    leaf.title = "리듬 잡기"
    leaf.node_type = "leaf"
    leaf.depth = 1
    leaf.order_index = 0
    leaf.is_leaf = True
    leaf.tree_kind = "plan"
    leaf.source = "llm"
    leaf.archived_at = archived_at
    session.add(leaf)
    await session.flush()
    return leaf


async def _seed_action(
    session: AsyncSession,
    goal: Goal,
    leaf: GoalNode,
    *,
    status: str,
    target_date: date | None = None,
) -> ActionItem:
    a = ActionItem()
    a.id = uuid.uuid4()
    a.user_id = goal.user_id
    a.goal_id = goal.id
    a.goal_node_id = leaf.id
    a.title = "카드"
    a.target_date = target_date or now_kst().date()
    a.category = "other"
    a.status = status
    a.source = "goal"
    session.add(a)
    await session.flush()
    return a


async def test_past_cycle_terminal_action_does_not_leak_into_current_cycle_judgment(
    real_db_session: AsyncSession,
) -> None:
    """옛 주기(archived leaf)의 done 카드는 안 보이고, 새 주기의 planned 카드만 잡힌다."""
    goal = await _seed_goal(real_db_session)
    old_leaf = await _seed_leaf(real_db_session, goal, archived=True)
    await _seed_action(real_db_session, goal, old_leaf, status="done")
    new_leaf = await _seed_leaf(real_db_session, goal, archived=False)
    await _seed_action(real_db_session, goal, new_leaf, status="planned")

    repo = GoalRepo(real_db_session)
    nodes = await repo.list_nodes(goal.id, tree_kind="plan")
    leaf_ids = [n.id for n in nodes if n.node_type == "leaf"]
    assert leaf_ids == [new_leaf.id]  # archived leaf 는 list_nodes 자체가 이미 걸러낸다

    action_items = await cycle_proposal.fetch_action_items_for_leaf_nodes(real_db_session, leaf_ids)

    statuses = {a.status for a in action_items}
    assert statuses == {"planned"}  # 옛 주기의 'done' 카드가 섞이지 않았다
    assert (
        cycle_proposal.should_propose_next_cycle(action_items, today=now_kst().date()) is False
    )  # 남은 카드가 있다(target_date 가 오늘이라 아직 안 지났다)


async def test_current_cycle_all_terminal_proposes_next_cycle(
    real_db_session: AsyncSession,
) -> None:
    goal = await _seed_goal(real_db_session)
    old_leaf = await _seed_leaf(real_db_session, goal, archived=True)
    await _seed_action(real_db_session, goal, old_leaf, status="done")
    new_leaf = await _seed_leaf(real_db_session, goal, archived=False)
    await _seed_action(real_db_session, goal, new_leaf, status="done")

    repo = GoalRepo(real_db_session)
    nodes = await repo.list_nodes(goal.id, tree_kind="plan")
    leaf_ids = [n.id for n in nodes if n.node_type == "leaf"]

    action_items = await cycle_proposal.fetch_action_items_for_leaf_nodes(real_db_session, leaf_ids)

    assert len(action_items) == 1  # 옛 주기의 done 카드는 제외, 새 주기 것만
    assert cycle_proposal.should_propose_next_cycle(action_items, today=now_kst().date()) is True


async def test_overdue_planned_card_does_not_block_next_cycle(
    real_db_session: AsyncSession,
) -> None:
    """밀린 `planned` 카드가 다음 주기 제안을 막지 않는다 — 실 DB 경로로 확인.

    이 카드는 한 번도 [▶시작] 하지 않아 execution_event 가 없으므로 `expire_unreflected` cron
    (=`completion_status='in_progress'` 인 실행만 대상)이 **영원히 못 쓸어낸다.** 즉 archive 로
    입력에서 빠지길 기대할 수 없고, 판정 함수가 날짜로 걸러야 한다.
    """
    goal = await _seed_goal(real_db_session)
    leaf = await _seed_leaf(real_db_session, goal, archived=False)
    today = now_kst().date()
    await _seed_action(
        real_db_session, goal, leaf, status="done", target_date=today - timedelta(days=14)
    )
    await _seed_action(
        real_db_session, goal, leaf, status="planned", target_date=today - timedelta(days=3)
    )

    repo = GoalRepo(real_db_session)
    nodes = await repo.list_nodes(goal.id, tree_kind="plan")
    leaf_ids = [n.id for n in nodes if n.node_type == "leaf"]
    action_items = await cycle_proposal.fetch_action_items_for_leaf_nodes(real_db_session, leaf_ids)

    assert len(action_items) == 2  # 밀린 카드는 archive 되지 않아 그대로 조회된다
    assert cycle_proposal.should_propose_next_cycle(action_items, today=today) is True


# ── fetch_goals_with_milestones (ADR-0007 PR-4 일반형) ──


async def _seed_milestone(
    session: AsyncSession,
    goal: Goal,
    *,
    order_index: int,
    completed: bool = False,
    archived: bool = False,
) -> GoalNode:
    n = GoalNode()
    n.id = uuid.uuid4()
    n.goal_id = goal.id
    n.parent_node_id = None
    n.title = f"마일스톤 {order_index}"
    n.node_type = "milestone"
    n.depth = 1
    n.order_index = order_index
    n.is_leaf = False
    n.tree_kind = "plan"
    n.source = "llm"
    n.completed_at = now_kst() if completed else None
    n.archived_at = now_kst() if archived else None
    session.add(n)
    await session.flush()
    return n


async def test_fetch_goals_with_milestones_scoped_to_user_and_active_status(
    real_db_session: AsyncSession,
) -> None:
    """다른 사용자·비활성(status!='active') 목표의 마일스톤은 안 섞인다."""
    mine = await _seed_goal(real_db_session)
    await _seed_milestone(real_db_session, mine, order_index=0)

    other = await _seed_goal(real_db_session)
    await _seed_milestone(real_db_session, other, order_index=0)

    proposed = await _seed_goal(real_db_session)
    proposed.status = "proposed"
    await real_db_session.flush()
    await _seed_milestone(real_db_session, proposed, order_index=0)

    result = await cycle_proposal.fetch_goals_with_milestones(real_db_session, mine.user_id)

    assert set(result.keys()) == {mine.id}


async def test_fetch_goals_with_milestones_excludes_archived_milestones(
    real_db_session: AsyncSession,
) -> None:
    """보관된 마일스톤(재승인으로 빠진 것)은 안 보인다 — 지금 뼈대만."""
    goal = await _seed_goal(real_db_session)
    await _seed_milestone(real_db_session, goal, order_index=0, archived=True)
    live = await _seed_milestone(real_db_session, goal, order_index=1)

    result = await cycle_proposal.fetch_goals_with_milestones(real_db_session, goal.user_id)

    assert [n.id for n in result[goal.id]] == [live.id]


async def test_fetch_goals_with_milestones_orders_by_order_index(
    real_db_session: AsyncSession,
) -> None:
    """반환 순서는 확정 순서(`order_index`) — 커서 판정이 "가장 이른 미완료"를 신뢰하는 전제."""
    goal = await _seed_goal(real_db_session)
    m2 = await _seed_milestone(real_db_session, goal, order_index=2)
    m0 = await _seed_milestone(real_db_session, goal, order_index=0)
    m1 = await _seed_milestone(real_db_session, goal, order_index=1)

    result = await cycle_proposal.fetch_goals_with_milestones(real_db_session, goal.user_id)

    assert [n.id for n in result[goal.id]] == [m0.id, m1.id, m2.id]


async def test_fetch_goals_with_milestones_omits_goals_without_any(
    real_db_session: AsyncSession,
) -> None:
    """마일스톤이 아예 없는 목표는 dict 에 나타나지 않는다(리듬형 등)."""
    goal = await _seed_goal(real_db_session)

    result = await cycle_proposal.fetch_goals_with_milestones(real_db_session, goal.user_id)

    assert goal.id not in result
