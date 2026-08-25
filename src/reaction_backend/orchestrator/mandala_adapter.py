"""만다라트 결정적 후보정 + 영속화 (§5.6, §3.4) — LLM 산출을 항상 고정 형태로 맞춘다.

**층 ②** — 스키마를 느슨하게 둔 LLM 원본(층①, `MandalaSubgoalPlan`/`MandalaCellPlan`)을
받아 8축/축당 ≤8칸으로 패딩·중복제거·잘라내기 한다. 8축 전부를 폴백 처리하는 대신
`min_length=1` 정도의 느슨한 스키마 + 여기서의 결정적 보정을 쓰는 이유는
`first_plan_adapter.py:341-342` 의 교훈과 같다 — "일부만 자리표시자인 것보다
스키마 위반으로 전부 자리표시자가 되는 게 훨씬 나쁘다".

**층 ③** — `rule_subgoals`/`rule_cells` 는 LLM 호출 자체가 완전히 실패했을 때(`aiClient.run`
의 `fallback=`)만 쓰이는 완전 결정적 생성기다.

`persist_mandala` 는 승인(U6) 시 `goal_nodes` 에 `tree_kind='mandala'` 로 73행(≤)을 쓴다 —
PR3 의 오염 차단 축(R1/W1/W2/W3, `1ee508b967ba`)이 이미 이 값을 전제로 계획 트리와
분리해 둔 자리다.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.orchestrator.interview_catalog import ULTIMATE_DOMAIN_OPTIONS
from reaction_backend.repositories.habit_repo import current_week_start_kst
from reaction_backend.schemas.common import now_kst, to_kst
from reaction_backend.schemas.mandala import (
    MandalaCell,
    MandalaCellItem,
    MandalaCellPlan,
    MandalaGap,
    MandalaSource,
    MandalaSubgoal,
    MandalaSubgoalItem,
    MandalaSubgoalPlan,
)
from reaction_backend.schemas.ultimate_goal import UltimateGoalOutcome

_SUBGOAL_TITLE_MAX = 10  # §7.7 — depth1 ≤10자
_CELL_TITLE_MAX = 16  # §7.7 — depth2 ≤16자
_RING_SIZE = 8


def format_titles(titles: Sequence[str]) -> str:
    """프롬프트에 넣을 제목 목록 — 없으면 '(없음)'."""
    return "\n".join(f"- {t}" for t in titles) if titles else "(없음)"


def format_subgoals_list(subgoals: Sequence[MandalaSubgoal]) -> str:
    """Stage B(`planning/mandala_cells`) 의 `{{subgoals}}` — 확정된 8축을 인덱스와 함께."""
    return "\n".join(f"{sg.order_index}: {sg.title}" for sg in subgoals)


def context_from_ultimate(outcome: UltimateGoalOutcome) -> dict[str, str]:
    """`UltimateGoalOutcome` → Stage A/B 공용 프롬프트 변수(P4/P5, §8.2).

    `locked_axes` 는 `pillars_hint` 와 같은 원천 데이터를 담는다 — 사용자가 인터뷰에서
    직접 말한 축이 곧 "제목·순서 유지, 개명 금지" 대상이라 별도로 관리할 값이 없다.
    """
    horizon = f"{outcome.horizon_years}년" if outcome.horizon_years else "기한 없음"
    pillars = format_titles(outcome.pillars_hint)
    return {
        "statement": outcome.statement,
        "domain": outcome.domain or "(미입력)",
        "horizon": horizon,
        "measure": outcome.measure or "(미입력)",
        "success_image": outcome.success_image or "(미입력)",
        "current_position": outcome.current_position or "(미입력)",
        "constraints": ", ".join(outcome.constraints) if outcome.constraints else "(없음)",
        "pillars_hint": pillars,
        "locked_axes": pillars,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 층② — 결정적 후보정 (LLM 원본 또는 층③ 폴백 원본을 공통으로 통과)
# ─────────────────────────────────────────────────────────────────────────────


def shape_subgoals(
    raw: Sequence[MandalaSubgoalItem], *, pillars_hint: Sequence[str], fell_back: bool
) -> list[MandalaSubgoal]:
    """LLM(또는 룰) 원본 → 항상 정확히 8개(`order_index` 0~7).

    우선순위: ① `pillars_hint`(사용자가 직접 말한 축, `locked=True` · `source="user"`) →
    ② LLM/룰 원본(중복 제거) → ③ 그래도 모자라면 `ULTIMATE_DOMAIN_OPTIONS` 카탈로그 패딩.
    `pillars_hint` 는 몇 개를 말했든 절대 빠지지 않는다 — 이게 곧 "재생성이 못 건드리는 축"
    보장의 원천이다.
    """
    seen: set[str] = set()
    result: list[MandalaSubgoal] = []

    def _add(title: str, why_text: str | None, source: MandalaSource, locked: bool) -> bool:
        key = title.strip()[:_SUBGOAL_TITLE_MAX]
        if not key or key in seen:
            return False
        seen.add(key)
        result.append(
            MandalaSubgoal(
                order_index=len(result), title=key, why_text=why_text, source=source, locked=locked
            )
        )
        return len(result) >= _RING_SIZE

    for hint in pillars_hint:
        if _add(hint, None, "user", True):
            break
    if len(result) < _RING_SIZE:
        llm_source: MandalaSource = "rule" if fell_back else "llm"
        for item in raw:
            if _add(item.title, item.why_text, llm_source, False):
                break
    if len(result) < _RING_SIZE:
        for axis in ULTIMATE_DOMAIN_OPTIONS:
            if _add(axis, None, "rule", False):
                break
    return result


def _shape_one_axis_cells(
    raw_titles: Sequence[str],
    *,
    subgoal_index: int,
    locked_titles: Sequence[str],
    fell_back: bool,
) -> tuple[list[MandalaCell], list[MandalaGap]]:
    seen: set[str] = set()
    cells: list[MandalaCell] = []

    def _add(title: str, source: MandalaSource) -> bool:
        key = title.strip()[:_CELL_TITLE_MAX]
        if not key or key in seen:
            return False
        seen.add(key)
        cells.append(
            MandalaCell(
                subgoal_index=subgoal_index, order_index=len(cells), title=key, source=source
            )
        )
        return len(cells) >= _RING_SIZE

    for t in locked_titles:
        if _add(t, "user"):
            break
    if len(cells) < _RING_SIZE:
        cell_source: MandalaSource = "rule" if fell_back else "llm"
        for t in raw_titles:
            if _add(t, cell_source):
                break
    # 못 채운 칸은 억지로 채우지 않는다(§5.6) — gaps 로 남겨 FE 가 점선 렌더.
    gaps = [
        MandalaGap(
            subgoal_index=subgoal_index, order_index=i, reason="AI가 이 칸을 채우지 못했어요"
        )
        for i in range(len(cells), _RING_SIZE)
    ]
    return cells, gaps


def shape_cells(
    raw: Sequence[MandalaCellItem], *, subgoals: Sequence[MandalaSubgoal], fell_back: bool
) -> tuple[list[MandalaCell], list[MandalaGap]]:
    """LLM(또는 룰) 원본 → 축별로 묶어 각 축 ≤8칸 + 못 채운 칸은 `gaps`."""
    by_axis: dict[int, list[str]] = {sg.order_index: [] for sg in subgoals}
    for item in raw:
        if item.subgoal_index in by_axis:
            by_axis[item.subgoal_index].append(item.title)

    all_cells: list[MandalaCell] = []
    all_gaps: list[MandalaGap] = []
    for sg in subgoals:
        cells, gaps = _shape_one_axis_cells(
            by_axis.get(sg.order_index, []),
            subgoal_index=sg.order_index,
            locked_titles=(),  # Stage B 최초 생성 시점엔 사용자가 편집한 셀이 아직 없다.
            fell_back=fell_back,
        )
        all_cells.extend(cells)
        all_gaps.extend(gaps)
    return all_cells, all_gaps


def shape_branch_cells(
    raw: Sequence[MandalaCellItem],
    *,
    subgoal_index: int,
    locked_cells: Sequence[MandalaCell],
    fell_back: bool,
) -> tuple[list[MandalaCell], list[MandalaGap]]:
    """링(8칸) 1개 재생성(U5) 후보정 — `locked_cells`(source="user") 는 절대 안 바뀐다."""
    raw_titles = [item.title for item in raw if item.subgoal_index == subgoal_index]
    locked_titles = [c.title for c in locked_cells if c.subgoal_index == subgoal_index]
    return _shape_one_axis_cells(
        raw_titles, subgoal_index=subgoal_index, locked_titles=locked_titles, fell_back=fell_back
    )


# ─────────────────────────────────────────────────────────────────────────────
# 층③ — 완전 폴백 (LLM 호출 자체가 실패했을 때만, `aiClient.run(fallback=...)`)
# ─────────────────────────────────────────────────────────────────────────────


def rule_subgoals(outcome: UltimateGoalOutcome) -> MandalaSubgoalPlan:
    """Stage A 완전 폴백 — `pillars_hint` + 도메인 축 카탈로그로 8개를 만든다."""
    items = [MandalaSubgoalItem(title=t[:_SUBGOAL_TITLE_MAX]) for t in outcome.pillars_hint]
    seen = {i.title for i in items}
    for axis in ULTIMATE_DOMAIN_OPTIONS:
        if len(items) >= _RING_SIZE:
            break
        if axis not in seen:
            items.append(MandalaSubgoalItem(title=axis))
            seen.add(axis)
    return MandalaSubgoalPlan(subgoals=items[:_RING_SIZE])


def rule_cells(subgoals: Sequence[MandalaSubgoal]) -> MandalaCellPlan:
    """Stage B 완전 폴백 — 축별 "{축} N단계"(`first_plan.py` 의 "N회차" 패턴 차용)."""
    items = [
        MandalaCellItem(
            subgoal_index=sg.order_index, title=f"{sg.title} {j + 1}단계"[:_CELL_TITLE_MAX]
        )
        for sg in subgoals
        for j in range(_RING_SIZE)
    ]
    return MandalaCellPlan(cells=items)


def rule_branch_cells(
    subgoal: MandalaSubgoal, locked_cells: Sequence[MandalaCell]
) -> MandalaCellPlan:
    """링 재생성(U5) 완전 폴백 — 잠긴 칸은 그대로, 나머지는 "{축} N단계" 로 채운다."""
    locked_titles = {c.title for c in locked_cells if c.subgoal_index == subgoal.order_index}
    items = [
        MandalaCellItem(
            subgoal_index=subgoal.order_index,
            title=f"{subgoal.title} {j + 1}단계"[:_CELL_TITLE_MAX],
        )
        for j in range(_RING_SIZE)
        if f"{subgoal.title} {j + 1}단계"[:_CELL_TITLE_MAX] not in locked_titles
    ]
    return MandalaCellPlan(cells=items)


# ─────────────────────────────────────────────────────────────────────────────
# 영속화 (U6 승인) — tree_kind='mandala' (PR3 오염 차단 축 전제)
# ─────────────────────────────────────────────────────────────────────────────


async def _archive_previous_mandala(session: AsyncSession, *, goal_id: uuid.UUID) -> None:
    """이 goal 의 기존 활성 만다라 트리를 보관 — `_archive_goal_nodes`(계획 트리)의 만다라판.

    부분 유니크 인덱스 `uq_goal_nodes_mandala_root`(goal_id, tree_kind='mandala' AND
    archived_at IS NULL)가 이전 트리를 보관하지 않으면 재승인 시 새 root INSERT 를 막는다
    (재생성→재승인을 반복해도 옛 73칸이 쌓이지 않게).
    """
    stmt = select(GoalNode).where(
        GoalNode.goal_id == goal_id,
        GoalNode.tree_kind == "mandala",
        GoalNode.archived_at.is_(None),
    )
    rows = (await session.execute(stmt)).scalars().all()
    now = now_kst()
    for n in rows:
        n.archived_at = now


async def persist_mandala(
    session: AsyncSession,
    *,
    goal: Goal,
    center_why_text: str | None,
    subgoals: Sequence[MandalaSubgoal],
    cells: Sequence[MandalaCell],
) -> tuple[GoalNode, int]:
    """편집본 → `goal_nodes` 73행(≤). 반환: (root 노드, 영속 행 수 = 1 + 8 + len(cells)).

    셀이 없는 칸은 만들지 않는다(억지 패딩 금지, §5.6) — `activated` 가 73보다 작을 수 있다.
    """
    await _archive_previous_mandala(session, goal_id=goal.id)

    # id 는 flush 로 받지 않고 여기서 미리 채운다(`first_plan_adapter.py` 의 GoalNode 생성과
    # 같은 이유) — 같은 트랜잭션 안에서 자식의 parent_node_id 로 곧바로 써야 하고, DB
    # server_default 왕복(flush) 없이도(테스트의 fake session 포함) 항상 값이 있어야 한다.
    root = GoalNode()
    root.id = uuid.uuid4()
    root.goal_id = goal.id
    root.parent_node_id = None
    root.title = goal.title
    root.node_type = "core"
    root.depth = 0
    root.order_index = 0
    root.is_leaf = False
    root.tree_kind = "mandala"
    root.source = "llm"
    root.why_text = center_why_text
    session.add(root)

    subgoal_nodes: dict[int, GoalNode] = {}
    for sg in subgoals:
        node = GoalNode()
        node.id = uuid.uuid4()
        node.goal_id = goal.id
        node.parent_node_id = root.id
        node.title = sg.title
        node.node_type = "subgoal"
        node.depth = 1
        node.order_index = sg.order_index
        node.is_leaf = False
        node.tree_kind = "mandala"
        node.source = sg.source
        node.why_text = sg.why_text
        node.locked = sg.locked
        session.add(node)
        subgoal_nodes[sg.order_index] = node

    persisted_cells = 0
    for cell in cells:
        parent = subgoal_nodes.get(cell.subgoal_index)
        if parent is None:  # 방어적 — subgoals 밖의 index 는 무시(있을 수 없지만 조용히 스킵)
            continue
        node = GoalNode()
        node.id = uuid.uuid4()
        node.goal_id = goal.id
        node.parent_node_id = parent.id
        node.title = cell.title
        node.node_type = "leaf"
        node.depth = 2
        node.order_index = cell.order_index
        node.is_leaf = True
        node.tree_kind = "mandala"
        node.source = cell.source
        session.add(node)
        persisted_cells += 1

    await session.flush()
    activated = 1 + len(subgoal_nodes) + persisted_cells
    return root, activated


# ─────────────────────────────────────────────────────────────────────────────
# 진척도 롤업 (U8, §7.8) — 컬럼 캐시 금지
# ─────────────────────────────────────────────────────────────────────────────

# 성공 정의는 기존 것을 그대로 쓴다(`orchestrator/weekly_review.py` 와 동일 상수) — 새 정의를
# 만들면 만다라 진척도와 주간 리포트 adherence 가 서로 다른 숫자를 말하게 된다.
_TERMINAL_ACTION_STATUSES = ("done", "partial_done", "failed", "over_done")
_SUCCESS_ACTION_STATUSES = ("done", "over_done")


async def fetch_actions_for_nodes(
    session: AsyncSession, node_ids: Sequence[uuid.UUID]
) -> list[ActionItem]:
    """만다라 leaf 노드에 매달린(archived 제외) action_item 전체 — 진척도 롤업 입력."""
    if not node_ids:
        return []
    stmt = select(ActionItem).where(
        ActionItem.goal_node_id.in_(node_ids), ActionItem.archived_at.is_(None)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def fetch_habits_for_nodes(
    session: AsyncSession, node_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Habit]:
    """만다라 leaf 노드에 링크된 활성 습관(반복형 칸, ADR-0008 §1) — node_id → Habit.

    칸 하나당 활성 습관은 최대 1개(DB 부분 유니크 인덱스가 강제) — dict 로 안전하게 접는다.
    """
    if not node_ids:
        return {}
    stmt = select(Habit).where(Habit.goal_node_id.in_(node_ids), Habit.archived_at.is_(None))
    result = await session.execute(stmt)
    return {h.goal_node_id: h for h in result.scalars().all() if h.goal_node_id is not None}


async def fetch_habit_instances_for_week(
    session: AsyncSession, habit_ids: Sequence[uuid.UUID], week_start: date
) -> dict[uuid.UUID, HabitInstance]:
    """반복형 칸에 링크된 습관의 **지정한 주** 인스턴스 — habit_id → HabitInstance.

    `fetch_current_week_habit_instances` 와 쿼리는 같고 주만 파라미터화한 버전이다. 주간
    리포트(`GET /reviews/weekly?weekStart=`, ADR-0008 §8 "E")는 과거 주도 조회하므로,
    "이번 주" 로 고정된 버전을 쓰면 조회 대상 주와 다른(오늘 기준) 주의 습관 데이터가 섞인다.
    """
    if not habit_ids:
        return {}
    stmt = select(HabitInstance).where(
        HabitInstance.habit_id.in_(habit_ids), HabitInstance.week_start == week_start
    )
    result = await session.execute(stmt)
    return {i.habit_id: i for i in result.scalars().all()}


async def fetch_current_week_habit_instances(
    session: AsyncSession, habit_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, HabitInstance]:
    """반복형 칸에 링크된 습관의 **이번 주** 인스턴스 — coverage 판정 입력(ADR-0008 §1.2).

    "이번 주 1회 이상(`done_count > 0`)"이 착수 판정 기준이다. 주 경계는
    `habit_repo.current_week_start_kst()` 단일 소스(월요일 KST)를 그대로 쓴다 — 습관
    등록·체크·조회가 전부 이 함수 하나를 쓰는 것과 같은 이유(어긋난 주에 행이 생기면 안 됨).
    """
    return await fetch_habit_instances_for_week(session, habit_ids, current_week_start_kst())


def _leaf_progress(node: GoalNode, actions: Sequence[ActionItem]) -> float | None:
    """leaf 1개의 실적 — 카드가 없어도 직접 완료 체크(`completed_at`)만으로 100%.

    카드가 있으면 성공률(성공/종결)로 본다. 종결(terminal) 카드가 아직 없으면(전부
    planned/in_progress) `None` — "0%가 아니라 아직 판단할 수 없음"을 구분해야 상위(축)
    롤업이 착수 여부(`coverage`)를 정확히 셀 수 있다.
    """
    if node.completed_at is not None:
        return 1.0
    terminal = [a for a in actions if a.status in _TERMINAL_ACTION_STATUSES]
    if not terminal:
        return None
    success = sum(1 for a in terminal if a.status in _SUCCESS_ACTION_STATUSES)
    return success / len(terminal)


def compute_progress(
    nodes: Sequence[GoalNode],
    actions: Sequence[ActionItem],
    habits_by_node: Mapping[uuid.UUID, Habit] | None = None,
    instances_by_habit: Mapping[uuid.UUID, HabitInstance] | None = None,
) -> dict[uuid.UUID, tuple[float | None, float | None]]:
    """만다라 노드별 (progress, coverage) — leaf/subgoal/core 전부. LLM 무관·DB 쓰기 0.

    **분모를 8로 고정하는 게 핵심**(§7.8) — 축당 leaf 가 8개보다 적게 저장돼 있어도
    (승인 시 `gaps` 로 남아 leaf 행 자체가 없는 칸이 있을 수 있다, `persist_mandala` 참고)
    실제 존재하는 leaf 수가 아니라 **고정 8** 로 나눈다. 착수한 셀만으로 나누면 1칸 하고
    100% 가 뜬다. `progress`(깊이: 실제 완료 비율)와 `coverage`(폭: 몇 칸이나 착수했는지)를
    함께 내려 "한 축만 파고 있다"가 드러나게 한다.

    `goal_nodes.progress` 컬럼을 두지 않는 이유: 두면 카드 상태가 바뀔 때마다 상위 노드를
    UPDATE 하는 쓰기 경로가 생기고, 오늘 체크인(`routes/today.py`)이 만다라 트리에 쓰기를
    하게 되며, 회복 경로의 "원본 status 불변" 원칙(AGENTS §2)과 뒤엉킨다. 매 조회 시
    파생하면 그 문제 자체가 없다.

    **반복형 칸(ADR-0008 §1.2)** — `habits_by_node` 에 있는(=이 칸에 활성 습관이 링크된)
    leaf 는 완료 개념이 없다: 자기 자신의 `progress` 는 항상 `null` 이고, 축의 `progress`
    분자에서도 아예 빠진다(0으로도 안 잡는다 — "안 채웠다"가 아니라 "이 지표가 안 맞는
    칸"이라서). 대신 `coverage`(착수 여부)는 "이번 주 습관을 1회 이상 했는가"
    (`instances_by_habit` 의 `done_count > 0`)로 판정한다. 한 축의 leaf 가 전부 반복형이면
    (프로젝트형이 하나도 없으면) 그 축의 `progress` 는 `0.0` 이 아니라 `null` —
    `_leaf_progress` 가 종결 카드 없을 때 `None` 을 내는 것과 같은 "판단 불가" 규약이다.
    두 인자를 생략하면(기본값 `None`→`{}`) 반복형 칸이 아예 없던 것처럼 동작해 기존 호출부는
    무변경으로 안전하다.
    """
    habits_by_node = habits_by_node or {}
    instances_by_habit = instances_by_habit or {}

    actions_by_node: dict[uuid.UUID, list[ActionItem]] = {}
    for a in actions:
        if a.goal_node_id is not None:
            actions_by_node.setdefault(a.goal_node_id, []).append(a)

    leaves = [n for n in nodes if n.depth == 2]
    subgoals = [n for n in nodes if n.depth == 1]
    cores = [n for n in nodes if n.depth == 0]

    result: dict[uuid.UUID, tuple[float | None, float | None]] = {}
    leaf_progress: dict[uuid.UUID, float] = {}
    repeat_started: dict[uuid.UUID, bool] = {}
    for leaf in leaves:
        habit = habits_by_node.get(leaf.id)
        if habit is not None:
            instance = instances_by_habit.get(habit.id)
            result[leaf.id] = (None, None)  # 반복형 — 완료 개념이 없다(§1)
            repeat_started[leaf.id] = instance is not None and instance.done_count > 0
            continue
        p = _leaf_progress(leaf, actions_by_node.get(leaf.id, []))
        result[leaf.id] = (p, None)  # leaf 는 coverage 개념이 없다(자기 자신이 최소 단위)
        if p is not None:
            leaf_progress[leaf.id] = p

    for sg in subgoals:
        children = [n for n in leaves if n.parent_node_id == sg.id]
        project_children = [n for n in children if n.id not in habits_by_node]
        repeat_children = [n for n in children if n.id in habits_by_node]
        filled = [leaf_progress[n.id] for n in project_children if n.id in leaf_progress]
        started = sum(1 for n in repeat_children if repeat_started.get(n.id, False))

        if project_children:
            sg_progress: float | None = sum(filled) / _RING_SIZE
        elif repeat_children:
            sg_progress = None  # 이 축엔 프로젝트형 leaf 가 하나도 없다 — 판단 불가
        else:
            sg_progress = 0.0
        result[sg.id] = (sg_progress, (len(filled) + started) / _RING_SIZE)

    if cores:
        sub_progress = [result[sg.id][0] for sg in subgoals if sg.id in result]
        sub_coverage = [result[sg.id][1] for sg in subgoals if sg.id in result]
        count = len(sub_progress) or 1
        core_progress = sum(p for p in sub_progress if p is not None) / count
        core_coverage = sum(c for c in sub_coverage if c is not None) / count
        result[cores[0].id] = (core_progress, core_coverage)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 주간 리포트용 스냅샷 (ADR-0008 §8 "E") — 조회 시점 파생, `period_summaries` 에 저장 안 함
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MandalaHabitWeeklyStat:
    """반복형 칸 1개의 이번 주 체크인 현황 — `GET /reviews/weekly` '반복 중' 절."""

    axis_title: str | None
    cell_title: str
    done_count: int
    target_count: int


@dataclass(frozen=True)
class MandalaWeeklyStat:
    """이번 주 만다라트 스냅샷. `completed_total`/`total_leaves` 는 누적치라 주와 무관하다.

    `goal_nodes.progress` 컬럼을 두지 않는 이유(`compute_progress` 참고)와 같은 이유로
    이 스냅샷도 저장하지 않고 매 조회 시 파생한다.
    """

    completed_this_week: int
    completed_total: int
    total_leaves: int
    touched_this_week: int
    untouched_axis_titles: list[str] = field(default_factory=list)
    untouched_axis_ids: list[uuid.UUID] = field(default_factory=list)
    habits: list[MandalaHabitWeeklyStat] = field(default_factory=list)


def compute_weekly_stat(
    nodes: Sequence[GoalNode],
    *,
    week_start: date,
    habits_by_node: Mapping[uuid.UUID, Habit],
    instances_by_habit: Mapping[uuid.UUID, HabitInstance],
) -> MandalaWeeklyStat:
    """만다라 leaf 를 "이번 주 끝낸 칸 / 굴린 칸 / 손 못 댄 축 / 반복 체크인"으로 집계.

    "활동"(굴린 칸 · 손 못 댄 축 판정 입력)을 완료 체크·습관 체크인으로만 좁힌다 — 셀은
    ActionItem 에 직결되지 않는다는 결정(§11 항목 6, `fetch_promoted_goal_titles_for_user`
    docstring)이라 프로젝트형 칸의 "작업 중"을 신뢰성 있게 잡을 다른 신호가 없다.
    `updated_at`(제목 오타 수정 등)을 쓰면 편집 자체가 "활동"으로 잡혀 지표가 흐려진다.

    `week_start`/`habits_by_node`/`instances_by_habit` 는 전부 호출자가 미리 구해 넘긴다
    (이 모듈의 다른 순수 함수와 같은 "DB 무관" 규약).
    """
    week_end = week_start + timedelta(days=7)  # exclusive — week_window() 와 동일 규약
    subgoals_by_id = {n.id: n for n in nodes if n.depth == 1}
    leaves = [n for n in nodes if n.depth == 2]

    completed_this_week = 0
    completed_total = 0
    touched_leaf_ids: set[uuid.UUID] = set()
    habit_stats: list[MandalaHabitWeeklyStat] = []

    for leaf in leaves:
        habit = habits_by_node.get(leaf.id)
        if habit is not None:
            instance = instances_by_habit.get(habit.id)
            done = instance.done_count if instance is not None else 0
            axis = subgoals_by_id.get(leaf.parent_node_id) if leaf.parent_node_id else None
            habit_stats.append(
                MandalaHabitWeeklyStat(
                    axis_title=axis.title if axis is not None else None,
                    cell_title=leaf.title,
                    done_count=done,
                    target_count=habit.target_count,
                )
            )
            if done > 0:
                touched_leaf_ids.add(leaf.id)
            continue
        if leaf.completed_at is not None:
            completed_total += 1
            if week_start <= to_kst(leaf.completed_at).date() < week_end:
                completed_this_week += 1
                touched_leaf_ids.add(leaf.id)

    untouched_axes = [
        sg
        for sg in subgoals_by_id.values()
        if all(leaf.id not in touched_leaf_ids for leaf in leaves if leaf.parent_node_id == sg.id)
    ]

    return MandalaWeeklyStat(
        completed_this_week=completed_this_week,
        completed_total=completed_total,
        total_leaves=len(leaves),
        touched_this_week=len(touched_leaf_ids),
        untouched_axis_titles=[sg.title for sg in untouched_axes],
        untouched_axis_ids=[sg.id for sg in untouched_axes],
        habits=habit_stats,
    )


def compute_stale_axes(
    nodes: Sequence[GoalNode],
    untouched_axis_id_sets: Sequence[set[uuid.UUID]],
    *,
    earliest_week_start: date,
) -> list[GoalNode]:
    """N주(호출자가 넘긴 주 수) 연속 손 못 댄 축 — "큰 목표 수정" 제안 대상(ADR-0008 §6, §8 "H").

    `untouched_axis_id_sets` 는 최근 N개 주(예: 이번 주·지난 주·2주 전) 각각의
    `compute_weekly_stat(...).untouched_axis_ids` 를 호출자가 모아 넘긴다 — 이 함수는 그
    교집합만 구하는 순수 판정이다. 제목이 아니라 id 로 맞춘다 — `PATCH .../nodes/{id}` 로
    축 제목을 바꿔도(§6 이 "수정 수단"으로 나열하는 바로 그 endpoint) 판정이 끊기지 않는다.

    `earliest_week_start` **이후에** 만들어진 축은 뺀다 — 막 만든 축은 아직 그 주 데이터가
    없어(=습관 체크인·완료 이력 자체가 없어) "손 못 댐"으로 잡히는데, 이건 방치가 아니라
    갓 생긴 축일 뿐이다. 새로 만든 축을 방치 취급하면 신뢰를 잃는다(§6 의 "비난 없는" 톤).
    """
    if not untouched_axis_id_sets:
        return []
    stale_ids = set.intersection(*(set(s) for s in untouched_axis_id_sets))
    subgoals_by_id = {n.id: n for n in nodes if n.depth == 1}
    result: list[GoalNode] = []
    for axis_id in stale_ids:
        axis = subgoals_by_id.get(axis_id)
        if axis is None or axis.created_at is None:
            continue
        if to_kst(axis.created_at).date() <= earliest_week_start:
            result.append(axis)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 만다라 → 오늘/브리프 연결 (PR7) — "만다라는 만들고 끝나면 죽은 문서가 된다"
# ─────────────────────────────────────────────────────────────────────────────


async def fetch_promoted_axis_titles(
    session: AsyncSession, goal_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """goal_id → 그 목표가 승격되어 온 만다라 축(하위목표) 제목. 승격 아닌 goal 은 dict 에 없음.

    `GET /goals`(S26) 가 이걸로 카드에 "축 배지" 를 단다 — 카드마다 `GET /goals/{id}/mandala`
    를 따로 불러 확인하는 N+1 을 피하려고 goal_id 목록을 한 번에 묻는다.
    """
    if not goal_ids:
        return {}
    stmt = select(GoalNode).where(
        GoalNode.tree_kind == "mandala",
        GoalNode.promoted_goal_id.in_(goal_ids),
        GoalNode.archived_at.is_(None),
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {n.promoted_goal_id: n.title for n in rows if n.promoted_goal_id is not None}


async def fetch_promoted_goal_titles_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> list[str]:
    """이 사용자가 만다라 축에서 승격한 목표들의 제목(ADR-0008 §8 "B", "핵심 접합점").

    계획 인터뷰(`routes/interview.py`)의 `goals.heaviest` 동적 보기 입력 — 승격만 해두고
    `goals.list` 에 다시 타이핑하지 않은 축도 "가장 무거운 목표" 후보로 바로 고를 수 있게
    한다(`docs/ultimate-goal-mandalart-strategy.md:71`). 셀을 ActionItem 에 직결하지 않는다는
    결정(같은 문서 §11 항목 6)은 그대로 지킨다 — 여기서 만드는 건 인터뷰 질문의 "보기"일
    뿐, 실행 트리·카드는 여전히 `/plans/generate` 가 LLM 분해로 새로 만든다.

    `Goal.title`(축 자체의 `GoalNode.title` 이 아니라)을 돌려준다 — 승격 후 사용자가
    `PATCH /goals/{id}` 로 제목을 고쳤을 수 있고, `materialize_goals` 의 재사용 매칭도
    `Goal.title` 기준이라(§3.4 W3) 여기서 어긋나면 같은 목표가 중복 생성된다.

    `status IN ('proposed', 'active')` — 승격 직후엔 항상 `'proposed'`(U10)라 이걸 빼면 막
    승격한 축이 절대 안 보인다. `'archived'`(만료·재승격)는 제외.
    """
    stmt = (
        select(Goal)
        .join(GoalNode, GoalNode.promoted_goal_id == Goal.id)
        .where(
            GoalNode.tree_kind == "mandala",
            GoalNode.depth == 1,
            GoalNode.archived_at.is_(None),
            Goal.user_id == user_id,
            Goal.status.in_(("proposed", "active")),
            Goal.archived_at.is_(None),
        )
        .order_by(Goal.updated_at.desc())
    )
    result = await session.execute(stmt)
    return [g.title for g in result.scalars().all()]


async def fetch_promoted_active_goals_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> list[Goal]:
    """이 사용자가 만다라 축에서 승격해 **지금 실행 중인**(status='active') 목표들.

    `fetch_promoted_goal_titles_for_user` 와 같은 판정(승격된 축 → Goal)이지만 이건
    `status IN ('proposed','active')` 대신 `'active'` 만 통과시키고 `Goal.title` 이 아니라
    행 자체를 돌려준다 — "다음 2주 열기" 제안(ADR-0008 §8 "G")은 실제로 계획을 승인해
    실행 중인 목표에만 의미가 있다(`proposed` 는 아직 `/plans/generate` 조차 안 거쳐
    action_item 이 없다). `.id` 로 `cycle_proposal.should_propose_next_cycle` 입력을 모은다.
    """
    stmt = (
        select(Goal)
        .join(GoalNode, GoalNode.promoted_goal_id == Goal.id)
        .where(
            GoalNode.tree_kind == "mandala",
            GoalNode.depth == 1,
            GoalNode.archived_at.is_(None),
            Goal.user_id == user_id,
            Goal.status == "active",
            Goal.archived_at.is_(None),
        )
        .order_by(Goal.updated_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_active_axis_label(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """ "이번 주 굴리는 축" — 승격된 축 중 그 Goal 이 실제로 `active` 인 것의 제목.

    여러 축이 동시에 active 로 승격돼 있으면 가장 최근에 손댄(Goal.updated_at 최신) 것
    하나만 — 모닝 브리프 한 줄에는 하나만 들어간다. 없으면 `None`(모닝 브리프가 아예
    언급하지 않는다 — `morning_brief.py` 의 "못 채우는 변수는 언급 금지" 원칙과 동일).
    """
    stmt = (
        select(GoalNode)
        .join(Goal, GoalNode.promoted_goal_id == Goal.id)
        .where(
            GoalNode.tree_kind == "mandala",
            GoalNode.depth == 1,
            GoalNode.promoted_goal_id.is_not(None),
            GoalNode.archived_at.is_(None),
            Goal.user_id == user_id,
            Goal.status == "active",
            Goal.archived_at.is_(None),
        )
        .order_by(Goal.updated_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    node = result.scalar_one_or_none()
    return node.title if node is not None else None


__all__ = [
    "MandalaHabitWeeklyStat",
    "MandalaWeeklyStat",
    "compute_progress",
    "compute_stale_axes",
    "compute_weekly_stat",
    "context_from_ultimate",
    "fetch_actions_for_nodes",
    "fetch_current_week_habit_instances",
    "fetch_habit_instances_for_week",
    "fetch_habits_for_nodes",
    "fetch_promoted_active_goals_for_user",
    "fetch_promoted_axis_titles",
    "fetch_promoted_goal_titles_for_user",
    "find_active_axis_label",
    "format_subgoals_list",
    "format_titles",
    "persist_mandala",
    "rule_branch_cells",
    "rule_cells",
    "rule_subgoals",
    "shape_branch_cells",
    "shape_cells",
    "shape_subgoals",
]
