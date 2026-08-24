"""`mandala_adapter` — 결정적 후보정(층②) + 완전 폴백(층③) + 영속화(persist_mandala) 순수 테스트.

LLM 호출 0회 — `shape_*`/`rule_*` 는 입력→출력이 결정적이라 표로 검증 가능. `persist_mandala`
만 fake session 을 쓴다(DB 쓰기가 핵심이라 순수 함수가 아니다).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.orchestrator import mandala_adapter
from reaction_backend.orchestrator.interview_catalog import ULTIMATE_DOMAIN_OPTIONS
from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.mandala import (
    MandalaCell,
    MandalaCellItem,
    MandalaSubgoal,
    MandalaSubgoalItem,
)
from reaction_backend.schemas.ultimate_goal import UltimateGoalOutcome

GOAL_ID = UUID("66666666-6666-4666-8666-666666666666")


def _outcome(**overrides: Any) -> UltimateGoalOutcome:
    base: dict[str, Any] = {
        "session_id": "iv_x",
        "generated_at": now_kst(),
        "end_reason": "completed",
        "ambiguity_final": 0.1,
        "analysis_source": "llm",
        "statement": "메이저리그 8구단 드래프트 1순위",
        "domain": "체력·컨디션",
        "horizon_years": 5,
        "measure": "드래프트 1라운드 지명",
        "success_image": "구단 유니폼을 입고 첫 공을 던지는 순간",
        "identity_note": "프로 지망생",
        "current_position": "고교 3학년",
        "constraints": ["부상 이력"],
        "values": [],
        "assets": None,
        "pillars_hint": [],
        "unresolved_slots": [],
    }
    base.update(overrides)
    return UltimateGoalOutcome(**base)


# ───────────────────── shape_subgoals (층②) ─────────────────────


def test_shape_subgoals_pads_with_domain_catalog_when_llm_underdelivers() -> None:
    """LLM 이 3개만 냈으면 나머지 5개는 도메인 축 카탈로그로 패딩되고 source='rule'."""
    raw = [MandalaSubgoalItem(title=t) for t in ("체력", "기술", "멘탈")]
    result = mandala_adapter.shape_subgoals(raw, pillars_hint=[], fell_back=False)

    assert len(result) == 8
    assert [sg.order_index for sg in result] == list(range(8))
    llm_titles = {sg.title for sg in result[:3]}
    assert llm_titles == {"체력", "기술", "멘탈"}
    assert all(sg.source == "llm" for sg in result[:3])
    padded = result[3:]
    assert all(sg.source == "rule" for sg in padded)
    assert all(sg.title in ULTIMATE_DOMAIN_OPTIONS for sg in padded)


def test_shape_subgoals_truncates_when_llm_overdelivers() -> None:
    """LLM 이 12개(스키마 상한)를 내도 앞 8개만 남는다."""
    raw = [MandalaSubgoalItem(title=f"축{i}") for i in range(12)]
    result = mandala_adapter.shape_subgoals(raw, pillars_hint=[], fell_back=False)
    assert len(result) == 8
    assert [sg.title for sg in result] == [f"축{i}" for i in range(8)]


def test_shape_subgoals_pillars_hint_always_locked_and_present() -> None:
    """사용자가 인터뷰에서 직접 말한 축은 LLM 출력에 없어도 강제로 포함되고 locked=True."""
    raw = [MandalaSubgoalItem(title="LLM축1"), MandalaSubgoalItem(title="LLM축2")]
    result = mandala_adapter.shape_subgoals(raw, pillars_hint=["구위", "멘탈"], fell_back=False)

    by_title = {sg.title: sg for sg in result}
    assert by_title["구위"].locked is True
    assert by_title["구위"].source == "user"
    assert by_title["멘탈"].locked is True
    # pillars_hint 가 먼저 배치되므로 order_index 0, 1.
    assert by_title["구위"].order_index == 0
    assert by_title["멘탈"].order_index == 1
    assert len(result) == 8


def test_shape_subgoals_dedupes_titles() -> None:
    """LLM 이 pillars_hint 와 같은 제목을 또 내도 중복 슬롯을 만들지 않는다."""
    raw = [MandalaSubgoalItem(title="구위"), MandalaSubgoalItem(title="새 축")]
    result = mandala_adapter.shape_subgoals(raw, pillars_hint=["구위"], fell_back=False)
    titles = [sg.title for sg in result]
    assert titles.count("구위") == 1


def test_shape_subgoals_marks_rule_source_on_fallback() -> None:
    """LLM 호출 자체가 폴백(fell_back=True)이면 원본 항목도 source='rule'."""
    raw = [MandalaSubgoalItem(title="체력")]
    result = mandala_adapter.shape_subgoals(raw, pillars_hint=[], fell_back=True)
    assert result[0].source == "rule"


# ───────────────────── shape_cells / shape_branch_cells (층②) ─────────────────────


def _subgoals() -> list[MandalaSubgoal]:
    return [
        MandalaSubgoal(order_index=i, title=f"축{i}", source="llm", locked=False) for i in range(8)
    ]


def test_shape_cells_groups_by_axis_and_fills_gaps_when_short() -> None:
    """축0 은 2개만, 축1 은 0개 — 부족분은 억지로 채우지 않고 gaps 로 남는다."""
    raw = [
        MandalaCellItem(subgoal_index=0, title="셀A"),
        MandalaCellItem(subgoal_index=0, title="셀B"),
    ]
    cells, gaps = mandala_adapter.shape_cells(raw, subgoals=_subgoals(), fell_back=False)

    axis0 = [c for c in cells if c.subgoal_index == 0]
    assert [c.title for c in axis0] == ["셀A", "셀B"]
    assert len(cells) == 2  # 다른 축엔 원본이 없으므로 전부 gap
    assert len(gaps) == 8 * 8 - 2
    # 축0 의 남은 6칸도 gap 이어야 한다.
    axis0_gaps = [g for g in gaps if g.subgoal_index == 0]
    assert len(axis0_gaps) == 6


def test_shape_cells_ignores_items_pointing_at_unknown_axis() -> None:
    """넘겨받은 `subgoals` 에 없는 subgoal_index 를 가리키는 항목은 조용히 버려진다.

    LLM 스키마 자체는 0~7 만 허용하지만(`MandalaCellItem.subgoal_index`), 이번 호출의
    `subgoals` 가 그보다 적을 수 있다 — 예: 방어적 재시도 경로에서 축이 일부만 확정된 경우.
    """
    raw = [MandalaCellItem(subgoal_index=7, title="유령 셀")]
    cells, _ = mandala_adapter.shape_cells(raw, subgoals=_subgoals()[:5], fell_back=False)
    assert cells == []


def test_shape_cells_dedupes_within_axis_but_not_across_axes() -> None:
    raw = [
        MandalaCellItem(subgoal_index=0, title="러닝"),
        MandalaCellItem(subgoal_index=0, title="러닝"),  # 같은 축 중복 — 하나만 남는다
        MandalaCellItem(subgoal_index=1, title="러닝"),  # 다른 축은 같은 제목이어도 무방
    ]
    cells, _ = mandala_adapter.shape_cells(raw, subgoals=_subgoals(), fell_back=False)
    axis0 = [c for c in cells if c.subgoal_index == 0]
    axis1 = [c for c in cells if c.subgoal_index == 1]
    assert len(axis0) == 1
    assert len(axis1) == 1


def test_shape_branch_cells_preserves_locked_cells_and_fills_rest() -> None:
    """`locked_cells`(source='user')는 절대 안 바뀌고, 새 후보가 나머지를 채운다."""
    locked = [MandalaCell(subgoal_index=2, order_index=0, title="사용자 편집", source="user")]
    raw = [MandalaCellItem(subgoal_index=2, title=f"새 후보{i}") for i in range(10)]

    cells, gaps = mandala_adapter.shape_branch_cells(
        raw, subgoal_index=2, locked_cells=locked, fell_back=False
    )

    assert cells[0].title == "사용자 편집"
    assert cells[0].source == "user"
    assert len(cells) == 8  # locked 1 + 새 후보 7
    assert gaps == []
    assert all(c.subgoal_index == 2 for c in cells)


def test_shape_branch_cells_ignores_locked_cells_from_other_axis() -> None:
    """`locked_cells` 에 다른 축 셀이 섞여 들어와도(방어적 호출) 이 축엔 영향 없다."""
    locked = [MandalaCell(subgoal_index=5, order_index=0, title="다른 축 편집", source="user")]
    raw = [MandalaCellItem(subgoal_index=2, title="새 후보")]

    cells, _ = mandala_adapter.shape_branch_cells(
        raw, subgoal_index=2, locked_cells=locked, fell_back=False
    )
    titles = [c.title for c in cells]
    assert "다른 축 편집" not in titles
    assert "새 후보" in titles


# ───────────────────── 완전 폴백 (층③) ─────────────────────


def test_rule_subgoals_always_yields_eight() -> None:
    plan = mandala_adapter.rule_subgoals(_outcome(pillars_hint=["구위"]))
    assert len(plan.subgoals) == 8
    assert plan.subgoals[0].title == "구위"


def test_rule_subgoals_pure_catalog_when_no_pillars_hint() -> None:
    plan = mandala_adapter.rule_subgoals(_outcome(pillars_hint=[]))
    titles = {s.title for s in plan.subgoals}
    assert titles == set(ULTIMATE_DOMAIN_OPTIONS)


def test_rule_cells_generates_eight_per_axis_with_stepped_titles() -> None:
    plan = mandala_adapter.rule_cells(_subgoals())
    assert len(plan.cells) == 64
    axis0 = [c.title for c in plan.cells if c.subgoal_index == 0]
    assert axis0 == [f"축0 {i}단계" for i in range(1, 9)]


def test_rule_branch_cells_skips_locked_titles() -> None:
    subgoal = MandalaSubgoal(order_index=3, title="축3", source="llm", locked=False)
    locked = [MandalaCell(subgoal_index=3, order_index=0, title="축3 1단계", source="user")]
    plan = mandala_adapter.rule_branch_cells(subgoal, locked)
    titles = [c.title for c in plan.cells]
    assert "축3 1단계" not in titles  # 잠긴 제목과 겹치는 룰 생성분은 스킵
    assert len(titles) == 7


# ───────────────────── context_from_ultimate ─────────────────────


def test_context_from_ultimate_formats_horizon_and_lists() -> None:
    ctx = mandala_adapter.context_from_ultimate(
        _outcome(horizon_years=None, constraints=["부상", "체중"], pillars_hint=["구위"])
    )
    assert ctx["horizon"] == "기한 없음"
    assert ctx["constraints"] == "부상, 체중"
    assert "구위" in ctx["pillars_hint"]
    assert ctx["locked_axes"] == ctx["pillars_hint"]


def test_context_from_ultimate_defaults_missing_fields() -> None:
    ctx = mandala_adapter.context_from_ultimate(_outcome(measure="", success_image=""))
    assert ctx["measure"] == "(미입력)"
    assert ctx["success_image"] == "(미입력)"


# ───────────────────── persist_mandala ─────────────────────


class _NodeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _NodeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _NodeSession:
    """`select(GoalNode)` 만 라우팅하는 fake session — `persist_mandala`/`_archive_previous_mandala`."""

    def __init__(self, *, nodes: list[GoalNode] | None = None) -> None:
        self._nodes = list(nodes or [])
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _NodeResult:  # noqa: ARG002
        return _NodeResult(self._nodes)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _goal(*, goal_id: UUID = GOAL_ID, title: str = "궁극목표") -> Goal:
    g = Goal()
    g.id = goal_id
    g.title = title
    return g


async def test_persist_mandala_writes_root_subgoals_and_cells() -> None:
    subgoals = _subgoals()
    cells = [MandalaCell(subgoal_index=0, order_index=0, title="러닝", source="llm")]
    session = _NodeSession()

    root, activated = await mandala_adapter.persist_mandala(
        session,  # type: ignore[arg-type]
        goal=_goal(),
        center_why_text="왜냐면",
        subgoals=subgoals,
        cells=cells,
    )

    assert root.tree_kind == "mandala"
    assert root.node_type == "core"
    assert root.depth == 0
    assert root.parent_node_id is None
    assert root.why_text == "왜냐면"
    assert activated == 1 + 8 + 1  # root + 8축 + 셀 1개

    subgoal_nodes = [n for n in session.added if n.node_type == "subgoal"]
    leaf_nodes = [n for n in session.added if n.node_type == "leaf"]
    assert len(subgoal_nodes) == 8
    assert len(leaf_nodes) == 1
    assert all(n.parent_node_id == root.id for n in subgoal_nodes)
    assert leaf_nodes[0].parent_node_id in {n.id for n in subgoal_nodes}
    assert leaf_nodes[0].tree_kind == "mandala"


async def test_persist_mandala_archives_previous_active_tree() -> None:
    """이 goal 아래 기존 활성 만다라 트리는 새로 승인할 때 보관된다(재승인 누적 방지)."""
    old_root = GoalNode()
    old_root.id = uuid4()
    old_root.goal_id = GOAL_ID
    old_root.tree_kind = "mandala"
    old_root.archived_at = None
    session = _NodeSession(nodes=[old_root])

    await mandala_adapter.persist_mandala(
        session,  # type: ignore[arg-type]
        goal=_goal(),
        center_why_text=None,
        subgoals=_subgoals(),
        cells=[],
    )

    assert old_root.archived_at is not None


# ───────────────────── compute_progress (U8, §7.8) ─────────────────────


def _tree_node(
    *,
    node_id: UUID | None = None,
    parent_id: UUID | None = None,
    depth: int,
    completed_at: Any = None,
    title: str = "노드",
) -> GoalNode:
    n = GoalNode()
    n.id = node_id or uuid4()
    n.parent_node_id = parent_id
    n.depth = depth
    n.completed_at = completed_at
    n.title = title
    return n


def _action(*, node_id: UUID, status: str) -> ActionItem:
    a = ActionItem()
    a.id = uuid4()
    a.goal_node_id = node_id
    a.status = status
    return a


def test_compute_progress_leaf_uses_completed_at_directly() -> None:
    leaf = _tree_node(depth=2, completed_at=now_kst())
    progress, coverage = mandala_adapter.compute_progress([leaf], [])[leaf.id]
    assert progress == 1.0
    assert coverage is None  # leaf 는 coverage 개념이 없다


def test_compute_progress_leaf_uses_action_success_rate() -> None:
    leaf = _tree_node(depth=2)
    actions = [
        _action(node_id=leaf.id, status="done"),
        _action(node_id=leaf.id, status="over_done"),
        _action(node_id=leaf.id, status="failed"),
        _action(node_id=leaf.id, status="in_progress"),  # 종결 아님 — 분모에서 제외
    ]
    progress, _ = mandala_adapter.compute_progress([leaf], actions)[leaf.id]
    assert progress == 2 / 3  # done+over_done 2개 / 종결 3개


def test_compute_progress_leaf_none_without_terminal_actions() -> None:
    leaf = _tree_node(depth=2)
    actions = [_action(node_id=leaf.id, status="planned")]
    progress, _ = mandala_adapter.compute_progress([leaf], actions)[leaf.id]
    assert progress is None  # 착수했지만 아직 종결 카드 없음 — 0%가 아니라 '판단 불가'


def test_compute_progress_subgoal_divides_by_fixed_eight() -> None:
    """축 아래 leaf 가 실제로는 2개뿐이어도(나머지는 gaps 라 행 자체가 없음) 8로 나눈다."""
    subgoal = _tree_node(depth=1)
    leaf1 = _tree_node(depth=2, parent_id=subgoal.id, completed_at=now_kst())  # progress=1.0
    leaf2 = _tree_node(depth=2, parent_id=subgoal.id)
    actions2 = [_action(node_id=leaf2.id, status="done")]  # progress=1.0

    progress_map = mandala_adapter.compute_progress([subgoal, leaf1, leaf2], actions2)
    sub_progress, sub_coverage = progress_map[subgoal.id]

    assert sub_progress == (1.0 + 1.0) / 8
    assert sub_coverage == 2 / 8  # 실제 leaf 행이 2개뿐이라도 분모는 고정 8


def test_compute_progress_subgoal_zero_when_nothing_filled() -> None:
    subgoal = _tree_node(depth=1)
    leaf = _tree_node(depth=2, parent_id=subgoal.id)  # completed_at 없음, 카드도 없음
    progress, coverage = mandala_adapter.compute_progress([subgoal, leaf], [])[subgoal.id]
    assert progress == 0.0
    assert coverage == 0.0


def test_compute_progress_root_averages_all_subgoals() -> None:
    root = _tree_node(depth=0)
    sub_full = _tree_node(depth=1, parent_id=root.id)
    sub_empty = _tree_node(depth=1, parent_id=root.id)
    leaf = _tree_node(depth=2, parent_id=sub_full.id, completed_at=now_kst())

    progress_map = mandala_adapter.compute_progress([root, sub_full, sub_empty, leaf], [])
    root_progress, root_coverage = progress_map[root.id]

    # sub_full = 1/8, sub_empty = 0/8 → 평균 (1/8 + 0)/2
    assert root_progress == (1 / 8) / 2
    assert root_coverage == (1 / 8) / 2


# ─────────────── compute_progress — 반복형 칸(ADR-0008 §1.2) ───────────────


def _habit(*, habit_id: UUID | None = None) -> Habit:
    h = Habit()
    h.id = habit_id or uuid4()
    return h


def _instance(*, habit_id: UUID, done_count: int) -> HabitInstance:
    i = HabitInstance()
    i.id = uuid4()
    i.habit_id = habit_id
    i.done_count = done_count
    return i


def test_compute_progress_repeat_leaf_has_no_progress_or_coverage_of_its_own() -> None:
    """반복형은 완료 개념이 없다 — completed_at 이 찍혀 있어도 leaf 자신은 null."""
    leaf = _tree_node(depth=2, completed_at=now_kst())
    habit = _habit()

    progress, coverage = mandala_adapter.compute_progress(
        [leaf], [], habits_by_node={leaf.id: habit}
    )[leaf.id]

    assert progress is None
    assert coverage is None


def test_compute_progress_subgoal_excludes_repeat_leaf_from_progress_numerator() -> None:
    """프로젝트형 1개(완료) + 반복형 1개 — 반복형은 분자에서 아예 빠진다(0으로도 안 잡음)."""
    subgoal = _tree_node(depth=1)
    project_leaf = _tree_node(depth=2, parent_id=subgoal.id, completed_at=now_kst())
    repeat_leaf = _tree_node(depth=2, parent_id=subgoal.id)
    habit = _habit()

    progress, coverage = mandala_adapter.compute_progress(
        [subgoal, project_leaf, repeat_leaf],
        [],
        habits_by_node={repeat_leaf.id: habit},
    )[subgoal.id]

    assert progress == 1.0 / 8  # 반복형이 분모(8)엔 남지만 분자엔 안 낀다
    assert coverage == 1 / 8  # 반복형이 이번 주 미착수라 coverage 도 안 낌


def test_compute_progress_subgoal_progress_is_null_when_only_repeat_leaves() -> None:
    """축의 leaf 가 전부 반복형이면 progress 는 0.0 이 아니라 null(판단 불가)."""
    subgoal = _tree_node(depth=1)
    leaf1 = _tree_node(depth=2, parent_id=subgoal.id)
    leaf2 = _tree_node(depth=2, parent_id=subgoal.id)
    habit1, habit2 = _habit(), _habit()

    progress, coverage = mandala_adapter.compute_progress(
        [subgoal, leaf1, leaf2],
        [],
        habits_by_node={leaf1.id: habit1, leaf2.id: habit2},
    )[subgoal.id]

    assert progress is None
    assert coverage == 0.0  # 둘 다 이번 주 미착수


def test_compute_progress_repeat_leaf_counts_toward_coverage_when_done_this_week() -> None:
    subgoal = _tree_node(depth=1)
    leaf = _tree_node(depth=2, parent_id=subgoal.id)
    habit = _habit()
    instance = _instance(habit_id=habit.id, done_count=1)

    _, coverage = mandala_adapter.compute_progress(
        [subgoal, leaf],
        [],
        habits_by_node={leaf.id: habit},
        instances_by_habit={habit.id: instance},
    )[subgoal.id]

    assert coverage == 1 / 8


def test_compute_progress_repeat_leaf_not_covered_without_this_week_instance() -> None:
    """이번 주 instance 자체가 없으면(예: cron 지연) 미착수로 안전하게 판정."""
    subgoal = _tree_node(depth=1)
    leaf = _tree_node(depth=2, parent_id=subgoal.id)
    habit = _habit()

    _, coverage = mandala_adapter.compute_progress(
        [subgoal, leaf], [], habits_by_node={leaf.id: habit}, instances_by_habit={}
    )[subgoal.id]

    assert coverage == 0.0


def test_compute_progress_backward_compatible_without_habit_args() -> None:
    """habits_by_node/instances_by_habit 생략 — 기존 호출부와 100% 동일 동작."""
    leaf = _tree_node(depth=2, completed_at=now_kst())
    progress, coverage = mandala_adapter.compute_progress([leaf], [])[leaf.id]
    assert progress == 1.0
    assert coverage is None


# ───────────── compute_weekly_stat (ADR-0008 §8 "E", GET /reviews/weekly) ─────────────


WEEK_START = date(2026, 6, 15)  # 월요일


def _dt_in_week(day_offset: int) -> datetime:
    return datetime.combine(
        WEEK_START + timedelta(days=day_offset), datetime.min.time(), tzinfo=KST
    )


def test_compute_weekly_stat_counts_completion_within_week_only() -> None:
    """이번 주에 완료 체크한 leaf 만 `completed_this_week` 에 잡히고, 지난주 완료도 누적엔 포함."""
    subgoal = _tree_node(depth=1, title="축0")
    this_week_leaf = _tree_node(
        depth=2, parent_id=subgoal.id, completed_at=_dt_in_week(3), title="이번주완료"
    )
    last_week_leaf = _tree_node(
        depth=2,
        parent_id=subgoal.id,
        completed_at=_dt_in_week(-1),  # 지난주 일요일
        title="지난주완료",
    )
    incomplete_leaf = _tree_node(depth=2, parent_id=subgoal.id, title="미완료")

    stat = mandala_adapter.compute_weekly_stat(
        [subgoal, this_week_leaf, last_week_leaf, incomplete_leaf],
        week_start=WEEK_START,
        habits_by_node={},
        instances_by_habit={},
    )

    assert stat.completed_this_week == 1
    assert stat.completed_total == 2
    assert stat.total_leaves == 3


def test_compute_weekly_stat_completion_at_week_boundary_is_exclusive() -> None:
    """`week_start + 7일`(다음 주 월요일 00:00)은 이번 주가 아니다 — week_window() 와 동일 경계."""
    subgoal = _tree_node(depth=1, title="축0")
    next_monday_leaf = _tree_node(
        depth=2, parent_id=subgoal.id, completed_at=_dt_in_week(7), title="다음주월요일"
    )

    stat = mandala_adapter.compute_weekly_stat(
        [subgoal, next_monday_leaf],
        week_start=WEEK_START,
        habits_by_node={},
        instances_by_habit={},
    )

    assert stat.completed_this_week == 0
    assert stat.completed_total == 1  # 누적엔 포함(전체 완료 여부는 주 무관)


def test_compute_weekly_stat_touched_includes_completion_and_habit_checkin() -> None:
    subgoal = _tree_node(depth=1, title="축0")
    completed_leaf = _tree_node(
        depth=2, parent_id=subgoal.id, completed_at=_dt_in_week(0), title="완료칸"
    )
    habit_leaf = _tree_node(depth=2, parent_id=subgoal.id, title="반복칸")
    habit = _habit()
    instance = _instance(habit_id=habit.id, done_count=2)

    stat = mandala_adapter.compute_weekly_stat(
        [subgoal, completed_leaf, habit_leaf],
        week_start=WEEK_START,
        habits_by_node={habit_leaf.id: habit},
        instances_by_habit={habit.id: instance},
    )

    assert stat.touched_this_week == 2
    assert stat.untouched_axis_titles == []


def test_compute_weekly_stat_axis_untouched_when_nothing_happened() -> None:
    """완료도 체크인도 없는 축 하나 + 활동 있는 축 하나 — 앞의 축만 손 못 댄 축."""
    untouched_axis = _tree_node(depth=1, title="손못댄축")
    untouched_leaf = _tree_node(depth=2, parent_id=untouched_axis.id, title="미완료칸")
    touched_axis = _tree_node(depth=1, title="굴린축")
    touched_leaf = _tree_node(
        depth=2, parent_id=touched_axis.id, completed_at=_dt_in_week(0), title="완료칸"
    )

    stat = mandala_adapter.compute_weekly_stat(
        [untouched_axis, untouched_leaf, touched_axis, touched_leaf],
        week_start=WEEK_START,
        habits_by_node={},
        instances_by_habit={},
    )

    assert stat.untouched_axis_titles == ["손못댄축"]


def test_compute_weekly_stat_habit_stats_report_axis_title_and_counts() -> None:
    axis = _tree_node(depth=1, title="체력")
    leaf = _tree_node(depth=2, parent_id=axis.id, title="코테 하루마다")
    habit = _habit()
    habit.target_count = 7
    instance = _instance(habit_id=habit.id, done_count=5)

    stat = mandala_adapter.compute_weekly_stat(
        [axis, leaf],
        week_start=WEEK_START,
        habits_by_node={leaf.id: habit},
        instances_by_habit={habit.id: instance},
    )

    assert len(stat.habits) == 1
    h = stat.habits[0]
    assert h.axis_title == "체력"
    assert h.cell_title == "코테 하루마다"
    assert h.done_count == 5
    assert h.target_count == 7


def test_compute_weekly_stat_habit_without_instance_counts_as_zero_and_untouched() -> None:
    """이번 주 instance 자체가 없으면(cron 지연 등) 0회로 안전하게 판정 — compute_progress 와 동일 규약."""
    axis = _tree_node(depth=1, title="축0")
    leaf = _tree_node(depth=2, parent_id=axis.id, title="반복칸")
    habit = _habit()

    stat = mandala_adapter.compute_weekly_stat(
        [axis, leaf], week_start=WEEK_START, habits_by_node={leaf.id: habit}, instances_by_habit={}
    )

    assert stat.habits[0].done_count == 0
    assert stat.touched_this_week == 0
    assert stat.untouched_axis_titles == ["축0"]


def test_compute_weekly_stat_repeat_leaf_completed_at_ignored_for_completion_counts() -> None:
    """반복형(habit 링크)인 leaf 는 completed_at 이 찍혀 있어도 완료 개념이 없다(compute_progress 와 동일)."""
    axis = _tree_node(depth=1, title="축0")
    leaf = _tree_node(
        depth=2, parent_id=axis.id, completed_at=_dt_in_week(0), title="반복칸(오염된 completed_at)"
    )
    habit = _habit()

    stat = mandala_adapter.compute_weekly_stat(
        [axis, leaf], week_start=WEEK_START, habits_by_node={leaf.id: habit}, instances_by_habit={}
    )

    assert stat.completed_this_week == 0
    assert stat.completed_total == 0
    assert len(stat.habits) == 1  # completion 이 아니라 habit 통계로만 잡힌다


def test_compute_weekly_stat_total_leaves_counts_both_project_and_repeat_types() -> None:
    axis = _tree_node(depth=1, title="축0")
    project_leaf = _tree_node(depth=2, parent_id=axis.id, title="프로젝트칸")
    repeat_leaf = _tree_node(depth=2, parent_id=axis.id, title="반복칸")
    habit = _habit()

    stat = mandala_adapter.compute_weekly_stat(
        [axis, project_leaf, repeat_leaf],
        week_start=WEEK_START,
        habits_by_node={repeat_leaf.id: habit},
        instances_by_habit={},
    )

    assert stat.total_leaves == 2


# ───────────────────── 만다라 → 오늘/브리프 연결 (PR7) ─────────────────────


def _axis_node(*, promoted_goal_id: UUID, title: str = "축") -> GoalNode:
    n = GoalNode()
    n.id = uuid4()
    n.depth = 1
    n.tree_kind = "mandala"
    n.title = title
    n.promoted_goal_id = promoted_goal_id
    n.archived_at = None
    return n


async def test_fetch_promoted_axis_titles_maps_goal_id_to_axis_title() -> None:
    goal_id = uuid4()
    node = _axis_node(promoted_goal_id=goal_id, title="구위")
    session = _NodeSession(nodes=[node])

    titles = await mandala_adapter.fetch_promoted_axis_titles(
        session,
        [goal_id],  # type: ignore[arg-type]
    )

    assert titles == {goal_id: "구위"}


async def test_fetch_promoted_axis_titles_empty_when_no_goal_ids() -> None:
    """빈 목록이면 쿼리 없이 빈 dict — `session.execute` 조차 안 부른다."""
    session = _NodeSession(nodes=[_axis_node(promoted_goal_id=uuid4())])
    titles = await mandala_adapter.fetch_promoted_axis_titles(session, [])  # type: ignore[arg-type]
    assert titles == {}


async def test_find_active_axis_label_returns_title_when_present() -> None:
    node = _axis_node(promoted_goal_id=uuid4(), title="체력")
    session = _NodeSession(nodes=[node])

    label = await mandala_adapter.find_active_axis_label(session, GOAL_ID)  # type: ignore[arg-type]

    assert label == "체력"


async def test_find_active_axis_label_none_when_nothing_active() -> None:
    session = _NodeSession(nodes=[])
    label = await mandala_adapter.find_active_axis_label(session, GOAL_ID)  # type: ignore[arg-type]
    assert label is None


# ───────────── fetch_promoted_active_goals_for_user (ADR-0008 §8 "G") ─────────────


def _promoted_goal(*, title: str = "승격목표") -> Goal:
    g = Goal()
    g.id = uuid4()
    g.title = title
    return g


async def test_fetch_promoted_active_goals_returns_seeded_rows() -> None:
    goal = _promoted_goal(title="영어 회화")
    session = _NodeSession(nodes=[goal])

    goals = await mandala_adapter.fetch_promoted_active_goals_for_user(
        session,  # type: ignore[arg-type]
        uuid4(),
    )

    assert goals == [goal]


async def test_fetch_promoted_active_goals_empty_when_none() -> None:
    session = _NodeSession(nodes=[])
    goals = await mandala_adapter.fetch_promoted_active_goals_for_user(
        session,  # type: ignore[arg-type]
        uuid4(),
    )
    assert goals == []
