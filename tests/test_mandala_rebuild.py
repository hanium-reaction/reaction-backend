"""만다라트 **다시 세우기** — 사전 확인(U13)과 승계(`persist_mandala`).

다시 세우기는 새 endpoint 가 아니라 이미 있는 U2~U6 을 한 번 더 타는 것이다. 그래서 위험한
쪽은 "생성"이 아니라 **교체**다 — 옛 트리를 보관하는 순간 사용자가 손으로 쌓은 셋(완료 표시·
축 승격·습관 링크)이 archived 노드에 매달린 채 화면에서 사라진다. 여기서 고정하는 계약은
둘이다:

1. 승인 **전** — `GET /goals/{id}/mandala/rebuild-preflight` 가 무엇이 걸려 있는지 보여준다.
2. 승인 **때** — 제목이 같은 자리면 이어지고, 없으면 링크만 끊긴다(습관·목표 자체는 산다).

엔티티별로 라우팅하는 fake session 을 쓴다 — `test_mandala_adapter.py::_NodeSession` 은 모든
select 에 같은 노드 목록을 돌려주므로 습관·목표 조회가 섞이는 이 경로를 태울 수 없다.
DB 제약(FK·부분 유니크)까지 거치는 검증은 `test_mandala_persist_real_db.py` 쪽이다.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.orchestrator import mandala_adapter
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.mandala import MandalaCell, MandalaSubgoal
from tests.conftest import DEMO_USER_UUID, FakeGoalRepo

# ─────────────────────────── 시드 헬퍼 ───────────────────────────


def _goal(*, is_ultimate: bool = True, title: str = "대기업 개발자로 입사") -> Goal:
    g = Goal()
    g.id = uuid4()
    g.user_id = DEMO_USER_UUID
    g.title = title
    g.category = "other"
    g.goal_tier = "parked"
    g.status = "active"
    g.is_ultimate = is_ultimate
    g.archived_at = None
    return g


def _node(
    *,
    goal_id: Any,
    parent_id: Any = None,
    title: str,
    node_type: str,
    depth: int,
    order_index: int = 0,
    completed_at: Any = None,
    promoted_goal_id: Any = None,
) -> GoalNode:
    n = GoalNode()
    n.id = uuid4()
    n.goal_id = goal_id
    n.parent_node_id = parent_id
    n.title = title
    n.node_type = node_type
    n.depth = depth
    n.order_index = order_index
    n.is_leaf = depth == 2
    n.tree_kind = "mandala"
    n.source = "llm"
    n.why_text = None
    n.locked = False
    n.completed_at = completed_at
    n.promoted_goal_id = promoted_goal_id
    n.archived_at = None
    return n


def _habit(*, node_id: UUID, title: str, frequency_per_week: int = 5) -> Habit:
    h = Habit()
    h.id = uuid4()
    h.user_id = DEMO_USER_UUID
    h.title = title
    h.category = "other"
    h.frequency_per_week = frequency_per_week
    h.minutes_per_session = 30
    h.time_preference = "anytime"
    h.priority_level = 3
    h.goal_node_id = node_id
    h.archived_at = None
    return h


def _tree(goal: Goal, *, axis_titles: list[str], cell_titles: list[str]) -> list[GoalNode]:
    """root + 축 N + 축마다 셀 M. 승계 키가 (축 제목, 칸 제목) 이라 제목만으로 시드한다."""
    root = _node(goal_id=goal.id, title=goal.title, node_type="core", depth=0)
    nodes = [root]
    for i, axis in enumerate(axis_titles):
        sub = _node(
            goal_id=goal.id,
            parent_id=root.id,
            title=axis,
            node_type="subgoal",
            depth=1,
            order_index=i,
        )
        nodes.append(sub)
        for j, cell in enumerate(cell_titles):
            nodes.append(
                _node(
                    goal_id=goal.id,
                    parent_id=sub.id,
                    title=cell,
                    node_type="leaf",
                    depth=2,
                    order_index=j,
                )
            )
    return nodes


class _RoutingSession:
    """엔티티별로 다른 결과를 돌려주는 fake session — `select(X)` 의 X 로 라우팅한다."""

    def __init__(
        self,
        *,
        nodes: list[GoalNode] | None = None,
        habits: list[Habit] | None = None,
        goals: list[Goal] | None = None,
        actions: list[ActionItem] | None = None,
    ) -> None:
        self._by_entity: dict[Any, list[Any]] = {
            GoalNode: list(nodes or []),
            Habit: list(habits or []),
            Goal: list(goals or []),
            ActionItem: list(actions or []),
        }
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        entity = stmt.column_descriptions[0]["entity"]
        rows = self._by_entity.get(entity, [])

        class _R:
            def scalars(self) -> Any:
                return self

            def all(self) -> list[Any]:
                return list(rows)

            def scalar_one_or_none(self) -> Any:
                return rows[0] if rows else None

        return _R()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


# ─────────────────── 승계 (persist_mandala) ───────────────────


async def test_rebuild_carries_completion_promotion_and_habit_by_title() -> None:
    """제목이 그대로면 완료 표시·축 승격·습관 링크가 새 트리로 이어진다."""
    goal = _goal()
    promoted_goal_id = uuid4()
    old = _tree(goal, axis_titles=["개발 실력"], cell_titles=["1일 1커밋", "사이드 배포"])
    old_axis = next(n for n in old if n.depth == 1)
    old_axis.promoted_goal_id = promoted_goal_id
    old_commit = next(n for n in old if n.title == "1일 1커밋")
    old_commit.completed_at = now_kst()
    habit = _habit(node_id=old_commit.id, title="1일 1커밋")
    session = _RoutingSession(nodes=old, habits=[habit])

    _, _, carried = await mandala_adapter.persist_mandala(
        session,  # type: ignore[arg-type]
        goal=goal,
        center_why_text=None,
        # 축 제목·칸 제목이 그대로인 새 트리(= 다시 세웠는데 이 축은 살아남은 경우).
        subgoals=[
            MandalaSubgoal(order_index=0, title="개발 실력"),
            *[MandalaSubgoal(order_index=i, title=f"축{i}") for i in range(1, 8)],
        ],
        cells=[
            MandalaCell(subgoal_index=0, order_index=0, title="1일 1커밋"),
            MandalaCell(subgoal_index=0, order_index=1, title="사이드 배포"),
        ],
    )

    assert carried.completed_cells == 1
    assert carried.promoted_axes == 1
    assert carried.linked_habits == 1
    assert carried.dropped_promoted_axes == ()
    assert carried.dropped_linked_habits == ()

    new_axis = next(n for n in session.added if n.depth == 1 and n.title == "개발 실력")
    new_commit = next(n for n in session.added if n.depth == 2 and n.title == "1일 1커밋")
    assert new_axis.promoted_goal_id == promoted_goal_id
    assert new_commit.completed_at is not None
    assert habit.goal_node_id == new_commit.id, "습관은 새 칸으로 옮겨 붙어야 한다"
    assert old_axis.archived_at is not None and old_commit.archived_at is not None


async def test_rebuild_unlinks_habit_and_reports_drop_when_cell_is_gone() -> None:
    """새 트리에 자리가 없으면 습관은 **지우지 않고** 링크만 끊는다(단독 습관으로 생존)."""
    goal = _goal()
    old = _tree(goal, axis_titles=["개발 실력"], cell_titles=["1일 1커밋"])
    old_axis = next(n for n in old if n.depth == 1)
    old_axis.promoted_goal_id = uuid4()
    old_cell = next(n for n in old if n.depth == 2)
    habit = _habit(node_id=old_cell.id, title="1일 1커밋")
    session = _RoutingSession(nodes=old, habits=[habit])

    _, _, carried = await mandala_adapter.persist_mandala(
        session,  # type: ignore[arg-type]
        goal=goal,
        center_why_text=None,
        # 축 이름이 통째로 바뀐 새 만다라트 — 옛 축·칸이 전부 사라졌다.
        subgoals=[MandalaSubgoal(order_index=i, title=f"새축{i}") for i in range(8)],
        cells=[MandalaCell(subgoal_index=0, order_index=0, title="새 칸")],
    )

    assert carried.linked_habits == 0
    assert carried.dropped_linked_habits == ("1일 1커밋",)
    assert carried.dropped_promoted_axes == ("개발 실력",)
    assert habit.goal_node_id is None, "링크만 끊긴다 — 습관 행 자체는 살아 있어야 한다"
    assert habit.archived_at is None


async def test_rebuild_does_not_move_same_cell_title_under_a_different_axis() -> None:
    """칸 제목만 같고 축이 다르면 남이다 — '매일 30분' 같은 흔한 칸이 엉뚱한 축으로 안 건너간다."""
    goal = _goal()
    old = _tree(goal, axis_titles=["영어"], cell_titles=["매일 30분"])
    old_cell = next(n for n in old if n.depth == 2)
    old_cell.completed_at = now_kst()
    habit = _habit(node_id=old_cell.id, title="영어 30분")
    session = _RoutingSession(nodes=old, habits=[habit])

    _, _, carried = await mandala_adapter.persist_mandala(
        session,  # type: ignore[arg-type]
        goal=goal,
        center_why_text=None,
        subgoals=[MandalaSubgoal(order_index=i, title=f"축{i}") for i in range(8)],
        cells=[MandalaCell(subgoal_index=0, order_index=0, title="매일 30분")],
    )

    assert carried.completed_cells == 0
    assert carried.linked_habits == 0
    assert carried.dropped_linked_habits == ("영어 30분",)


async def test_first_build_reports_empty_carry_over() -> None:
    """처음 세우는 경우엔 승계할 게 없다 — 전부 0/빈 튜플(응답에서 승계 절이 조용히 0)."""
    goal = _goal()
    session = _RoutingSession()

    _, activated, carried = await mandala_adapter.persist_mandala(
        session,  # type: ignore[arg-type]
        goal=goal,
        center_why_text=None,
        subgoals=[MandalaSubgoal(order_index=i, title=f"축{i}") for i in range(8)],
        cells=[MandalaCell(subgoal_index=0, order_index=0, title="셀")],
    )

    assert activated == 1 + 8 + 1
    assert carried == mandala_adapter.MandalaCarryOver()


# ─────────────────── 사전 확인 (U13 route) ───────────────────


def test_preflight_is_empty_when_no_tree_yet(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """아직 트리가 없으면 404 가 아니라 `hasTree=false` — 처음 세우기도 같은 경로를 탄다."""
    goal = _goal()
    fake_goal_repo._items[goal.id] = goal

    resp = client.get(f"/goals/goal_{goal.id}/mandala/rebuild-preflight")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hasTree"] is False
    assert body["rootNodeId"] is None
    assert body["totalCells"] == 0
    assert body["promotedAxes"] == [] and body["linkedHabits"] == []
    assert body["warnings"] == [], "세울 트리가 없으면 경고할 것도 없다"


def test_preflight_rejects_non_ultimate_goal(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    goal = _goal(is_ultimate=False)
    fake_goal_repo._items[goal.id] = goal

    resp = client.get(f"/goals/goal_{goal.id}/mandala/rebuild-preflight")

    assert resp.status_code == 404, resp.text


def test_preflight_counts_completed_cells_and_warns(
    client: TestClient, fake_goal_repo: FakeGoalRepo
) -> None:
    """트리가 있으면 완료 칸 수와 승계 규칙 안내가 실린다(확인 시트에 그대로 얹는 문장)."""
    goal = _goal()
    fake_goal_repo._items[goal.id] = goal
    nodes = _tree(goal, axis_titles=["개발 실력", "영어"], cell_titles=["칸A", "칸B"])
    for n in nodes:
        if n.title == "칸A":
            n.completed_at = now_kst()
    fake_goal_repo._nodes[goal.id] = nodes

    resp = client.get(f"/goals/goal_{goal.id}/mandala/rebuild-preflight")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hasTree"] is True
    assert body["statement"] == goal.title
    assert body["totalCells"] == 4
    assert body["completedCells"] == 2  # 축마다 '칸A' 하나씩
    assert any("완료" in w for w in body["warnings"])
    assert any("2칸" in w for w in body["warnings"])
