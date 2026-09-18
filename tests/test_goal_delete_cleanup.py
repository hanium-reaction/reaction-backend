"""목표 삭제가 **그 목표가 남긴 것**까지 멈추는가 — `DELETE /goals/{goalId}`.

예전엔 목표 행만 보관했다. 오늘 화면·주간 캘린더·아침 브리프·`pre_card` 알림은 전부
`action_items` 를 목표 상태와 무관하게 읽으므로, "정말 삭제" 한 목표의 카드가 다음 날에도
그대로 떴고 취소마저 거절됐다(goals-1 / data-1). 궁극목표를 지우면 만다라 칸에서 만든
반복형 습관도 매주 오늘 화면에 남았다(critic-4), 지운 만다라의 칸은 여전히 승격·편집됐다
(goals-21).

라우트가 보장하는 건 "무엇을 부르는가" 다 — 카드 정리 **판정**(예정 카드만, 사용자가 옮긴
것은 보존)은 완료 경로와 같은 함수라 `test_goal_completion_cards_real_db.py` 가 이미 실 DB 로
고정한다. 여기서는 삭제가 그 함수를 **같은 두 축으로** 부르는지, 그리고 만다라·습관 정리가
실제 SQL 에서도 도는지를 본다.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.schemas.common import now_kst
from tests.conftest import DB_AVAILABLE, DEMO_USER_UUID, FakeGoalRepo, FakeHabitRepo


def _goal(*, is_ultimate: bool = False) -> Goal:
    g = Goal()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = "궁극목표" if is_ultimate else "웹 개발"
    g.category = "other"
    g.goal_tier = "parked" if is_ultimate else "focus"
    g.status = "active"
    g.priority_level = 3
    g.is_ultimate = is_ultimate
    g.archived_at = None
    return g


def _mandala_node(goal: Goal, *, depth: int, parent: GoalNode | None = None) -> GoalNode:
    n = GoalNode()
    n.id = uuid4()
    n.goal_id = goal.id
    n.parent_node_id = parent.id if parent is not None else None
    n.title = {0: goal.title, 1: "축", 2: "칸"}[depth]
    n.node_type = {0: "core", 1: "subgoal", 2: "leaf"}[depth]
    n.depth = depth
    n.order_index = 0
    n.is_leaf = depth == 2
    n.tree_kind = "mandala"
    n.source = "llm"
    n.why_text = None
    n.locked = False
    n.completed_at = None
    n.promoted_goal_id = None
    n.archived_at = None
    return n


def _habit(repo: FakeHabitRepo, *, goal_node_id: Any = None, title: str = "습관") -> Habit:
    h = Habit()
    h.id = uuid4()
    h.user_id = DEMO_USER_UUID
    h.title = title
    h.category = "other"
    h.frequency_per_week = 3
    h.target_count = 3
    h.minutes_per_session = 30
    h.time_preference = "anytime"
    h.priority_level = 3
    h.goal_node_id = goal_node_id
    h.archived_at = None
    repo.seed(h)
    return h


def _spy_cleanup(monkeypatch: Any) -> list[tuple[Any, bool, bool]]:
    from reaction_backend.orchestrator import first_plan_adapter

    calls: list[tuple[Any, bool, bool]] = []

    async def spy(
        session: Any,
        *,
        user_id: Any,
        goal_id: Any,
        include_mandala: bool = False,
        include_recovery: bool = False,
    ) -> int:
        calls.append((goal_id, include_mandala, include_recovery))
        return 0

    monkeypatch.setattr(first_plan_adapter, "supersede_previous_plan", spy)
    return calls


def test_delete_cleans_up_cards_with_the_completion_axes(
    client: TestClient, fake_goal_repo: FakeGoalRepo, monkeypatch: Any
) -> None:
    """삭제는 완료와 **같은 두 축**(만다라·회복 포함)으로 카드를 정리한다.

    기본값(승인 경로)으로 부르면 만다라 유래 카드 — 궁극목표는 전부 이것 — 와 회복 카드가
    안 멈춘다. 그 차이는 실 DB 에서만 드러나 여기서 축까지 고정한다(#367 과 같은 이유).
    """
    calls = _spy_cleanup(monkeypatch)
    goal = _goal()
    fake_goal_repo._items[goal.id] = goal

    res = client.delete(f"/goals/goal_{goal.id}")

    assert res.status_code == 204
    assert calls == [(goal.id, True, True)]
    assert goal.archived_at is not None


def test_delete_missing_goal_cleans_nothing(client: TestClient, monkeypatch: Any) -> None:
    calls = _spy_cleanup(monkeypatch)

    res = client.delete(f"/goals/goal_{uuid4()}")

    assert res.status_code == 404
    assert calls == []


def test_delete_ultimate_archives_its_mandala_and_the_habits_made_from_it(
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_habit_repo: FakeHabitRepo,
    monkeypatch: Any,
) -> None:
    """궁극목표를 지우면 그 만다라 칸에서 만든 습관이 오늘 화면에서 빠진다(critic-4).

    볼 화면이 없는 습관이 매주 뜨고 3주 뒤 빈도 조정 제안까지 오던 경로다. 만다라와 무관한
    습관은 그대로다.
    """
    _spy_cleanup(monkeypatch)
    goal = _goal(is_ultimate=True)
    root = _mandala_node(goal, depth=0)
    axis = _mandala_node(goal, depth=1, parent=root)
    leaf = _mandala_node(goal, depth=2, parent=axis)
    fake_goal_repo._items[goal.id] = goal
    fake_goal_repo._nodes[goal.id] = [root, axis, leaf]
    linked = _habit(fake_habit_repo, goal_node_id=leaf.id, title="코테 1일 1문제")
    plain = _habit(fake_habit_repo, title="스트레칭")

    res = client.delete(f"/goals/goal_{goal.id}")

    assert res.status_code == 204
    assert linked.archived_at is not None
    assert plain.archived_at is None
    assert [h["title"] for h in client.get("/habits").json()] == ["스트레칭"]
    assert all(n.archived_at is not None for n in (root, axis, leaf))


def test_delete_plain_goal_leaves_habits_alone(
    client: TestClient,
    fake_goal_repo: FakeGoalRepo,
    fake_habit_repo: FakeHabitRepo,
    monkeypatch: Any,
) -> None:
    _spy_cleanup(monkeypatch)
    goal = _goal()
    fake_goal_repo._items[goal.id] = goal
    plain = _habit(fake_habit_repo)

    assert client.delete(f"/goals/goal_{goal.id}").status_code == 204
    assert plain.archived_at is None


def test_cells_of_a_deleted_ultimate_goal_are_gone(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """보관된 궁극목표의 칸은 편집·승격·반복형 전환 대상이 아니다(goals-21).

    노드가 아직 살아 있어도(삭제 이전 데이터) 목표가 보관됐으면 404 — 열려 있던 만다라
    화면에서 사라진 만다라로 새 목표를 만들지 못하게.
    """
    goal = _goal(is_ultimate=True)
    root = _mandala_node(goal, depth=0)
    axis = _mandala_node(goal, depth=1, parent=root)
    fake_goal_repo._items[goal.id] = goal
    fake_goal_repo._nodes[goal.id] = [root, axis]
    goal.archived_at = now_kst()
    goal.status = "archived"

    patch = client.patch(f"/goals/mandala/nodes/node_{axis.id}", json={"title": "새 축"})
    promote = client.post(
        f"/goals/mandala/nodes/node_{axis.id}/promote", json={"goalTier": "focus"}
    )

    assert patch.status_code == 404
    assert promote.status_code == 404
    assert axis.title == "축"


# ── 실 Postgres — 삭제 핸들러를 실제 SQL 로 한 번 태운다 ──────────────────────────


async def _seed_user(session: AsyncSession) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.local", name="목표 삭제")
    session.add(user)
    await session.flush()
    return user


async def _seed_goal(session: AsyncSession, user: User, *, is_ultimate: bool = False) -> Goal:
    goal = Goal()
    goal.id = uuid.uuid4()
    goal.user_id = user.id
    goal.title = "궁극목표" if is_ultimate else "웹 개발"
    goal.category = "other"
    goal.goal_tier = "parked" if is_ultimate else "focus"
    goal.status = "active"
    goal.is_ultimate = is_ultimate
    session.add(goal)
    await session.flush()
    return goal


async def _seed_card(
    session: AsyncSession, goal: Goal, *, status: str = "planned", block_source: str = "ai_plan"
) -> tuple[ActionItem, ScheduledBlock]:
    a = ActionItem()
    a.id = uuid.uuid4()
    a.user_id = goal.user_id
    a.goal_id = goal.id
    a.title = "3주차 세션"
    a.target_date = now_kst().date() + timedelta(days=2)
    a.category = "study"
    a.status = status
    a.source = "goal"
    session.add(a)
    await session.flush()
    b = ScheduledBlock()
    b.id = uuid.uuid4()
    b.user_id = goal.user_id
    b.action_item_id = a.id
    b.start_at = now_kst() + timedelta(days=2)
    b.end_at = b.start_at + timedelta(minutes=50)
    b.block_status = "scheduled"
    b.source = block_source
    session.add(b)
    await session.flush()
    return a, b


async def _delete_via_route(session: AsyncSession, user: User, goal: Goal) -> None:
    """라우트 핸들러를 실 세션으로 직접 부른다 — `commit` 만 flush 로 바꿔 격리를 지킨다.

    `real_db_session` 은 바깥 트랜잭션 롤백으로 격리하므로 commit 하면 안 된다(conftest 참고).
    """
    from reaction_backend.api.routes import goals as goals_route
    from reaction_backend.repositories.goal_repo import GoalRepo
    from reaction_backend.repositories.habit_repo import HabitRepo

    session.commit = session.flush  # type: ignore[method-assign]
    await goals_route.delete_goal(
        goal_id=f"goal_{goal.id}",
        user=user,
        repo=GoalRepo(session),
        habit_repo=HabitRepo(session),
        session=session,
    )
    await session.flush()


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_delete_stops_planned_cards_but_keeps_what_the_user_touched(
    real_db_session: AsyncSession,
) -> None:
    """예정 카드·블록은 멈추고, 시작한 카드와 사용자가 옮긴 카드는 남는다 — 실 SQL."""
    user = await _seed_user(real_db_session)
    goal = await _seed_goal(real_db_session, user)
    planned, planned_block = await _seed_card(real_db_session, goal)
    started, _ = await _seed_card(real_db_session, goal, status="in_progress")
    moved, moved_block = await _seed_card(real_db_session, goal, block_source="user_edit")

    await _delete_via_route(real_db_session, user, goal)
    for obj in (goal, planned, planned_block, started, moved, moved_block):
        await real_db_session.refresh(obj)

    assert goal.archived_at is not None
    assert planned.archived_at is not None  # soft — 행은 남는다(AGENTS §2)
    assert planned_block.block_status == "cancelled"
    assert started.archived_at is None
    assert moved.archived_at is None
    assert moved_block.block_status == "scheduled"


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_delete_ultimate_closes_the_mandala_and_its_habits(
    real_db_session: AsyncSession,
) -> None:
    """궁극목표 삭제 → 만다라 노드 보관 + 칸에 링크된 습관만 보관 + 그 칸은 더는 안 열린다."""
    from reaction_backend.repositories.goal_repo import GoalRepo
    from reaction_backend.repositories.habit_repo import HabitRepo

    user = await _seed_user(real_db_session)
    goal = await _seed_goal(real_db_session, user, is_ultimate=True)
    root = _mandala_node(goal, depth=0)
    real_db_session.add(root)
    await real_db_session.flush()
    axis = _mandala_node(goal, depth=1, parent=root)
    real_db_session.add(axis)
    await real_db_session.flush()
    leaf = _mandala_node(goal, depth=2, parent=axis)
    real_db_session.add(leaf)
    await real_db_session.flush()
    linked = Habit(
        user_id=user.id,
        title="코테 1일 1문제",
        category="other",
        frequency_per_week=5,
        minutes_per_session=30,
        time_preference="anytime",
        priority_level=3,
        goal_node_id=leaf.id,
    )
    plain = Habit(
        user_id=user.id,
        title="스트레칭",
        category="health",
        frequency_per_week=3,
        minutes_per_session=10,
        time_preference="anytime",
        priority_level=3,
    )
    real_db_session.add_all([linked, plain])
    await real_db_session.flush()

    await _delete_via_route(real_db_session, user, goal)
    for obj in (root, axis, leaf, linked, plain):
        await real_db_session.refresh(obj)

    assert all(n.archived_at is not None for n in (root, axis, leaf))
    assert linked.archived_at is not None
    assert plain.archived_at is None
    assert [h.title for h in await HabitRepo(real_db_session).list_active(user.id)] == ["스트레칭"]
    assert await GoalRepo(real_db_session).get_mandala_node(user.id, axis.id) is None


@pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")
async def test_get_mandala_node_rejects_live_node_of_archived_goal(
    real_db_session: AsyncSession,
) -> None:
    """노드는 살아 있어도 목표가 보관됐으면 없는 칸이다 — WHERE 에 `Goal.archived_at` 이 있어야."""
    from reaction_backend.repositories.goal_repo import GoalRepo

    user = await _seed_user(real_db_session)
    goal = await _seed_goal(real_db_session, user, is_ultimate=True)
    root = _mandala_node(goal, depth=0)
    real_db_session.add(root)
    await real_db_session.flush()
    repo = GoalRepo(real_db_session)
    assert await repo.get_mandala_node(user.id, root.id) is not None

    await repo.soft_delete(goal)

    assert await repo.get_mandala_node(user.id, root.id) is None
