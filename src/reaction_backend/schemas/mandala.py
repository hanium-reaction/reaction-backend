"""만다라트(Mandala) 도메인 스키마 (§3.7, §6.2, §8.2) — 궁극목표 8축×8칸 생성/승인.

세 층으로 나뉜다:
1. **LLM Structured Output** (`MandalaSubgoalPlan`/`MandalaCellPlan`) — `aiClient.run(schema=...)`
   강제 검증. 개수를 느슨하게 둔다(§5.6 층① — 스키마를 min/max=8 로 조이면 LLM 이 7개를
   냈을 때 재시도 3회 끝에 8개 **전부**가 자리표시자가 된다).
2. **후보정 후 고정 형태** (`MandalaSubgoal`/`MandalaCell`/`MandalaGap`) — `mandala_adapter` 가
   패딩·중복제거·잘라내기(§5.6 층②)를 거쳐 항상 8개(축)/축당 ≤8개(셀)로 맞춘 결과.
3. **경계 요청/응답** — Draft Layer(HITL). AI 산출 응답은 `DraftMixin` 상속 →
   사용자 [수락] 전까지 `is_draft=True`(AGENTS §1.4, ADR-0005 §7.2).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, DraftMixin, KstDatetime
from reaction_backend.schemas.goals import GoalNode, GoalTier
from reaction_backend.schemas.habits import HabitCategory, TimePreference

MandalaSource = Literal["llm", "rule", "user"]

# ─────────────────────────────────────────────────────────────────────────────
# LLM Structured Output — 느슨한 개수 제약(§5.6 층①)
# ─────────────────────────────────────────────────────────────────────────────


class MandalaSubgoalItem(CamelModel):
    """Stage A(`planning/mandala_subgoals`) LLM 출력 원소 — 후보정 전."""

    title: str = Field(min_length=1, max_length=10)
    why_text: str | None = None


class MandalaSubgoalPlan(CamelModel):
    """Stage A LLM Structured Output — 8개를 목표로 하되 1~12개까지 허용."""

    subgoals: list[MandalaSubgoalItem] = Field(min_length=1, max_length=12)


class MandalaCellItem(CamelModel):
    """Stage B(`planning/mandala_cells`·`planning/mandala_cells_branch`) LLM 출력 원소."""

    subgoal_index: int = Field(ge=0, le=7)
    title: str = Field(min_length=1, max_length=16)


class MandalaCellPlan(CamelModel):
    """Stage B LLM Structured Output — 못 채운 칸은 그냥 적게 낸다(억지로 채우지 않음)."""

    cells: list[MandalaCellItem] = Field(default_factory=list, max_length=64)


# ─────────────────────────────────────────────────────────────────────────────
# 후보정 후 — 항상 고정 형태 (mandala_adapter.shape_* 의 출력)
# ─────────────────────────────────────────────────────────────────────────────


class MandalaSubgoal(CamelModel):
    """확정된 하위목표(축) 1개 — 항상 정확히 8개(order_index 0~7)."""

    order_index: int = Field(ge=0, le=7)
    title: str = Field(min_length=1, max_length=10)  # §7.7 — depth1 ≤10자, 서버가 상한 강제
    why_text: str | None = None
    source: MandalaSource = "llm"
    locked: bool = False  # 사용자가 인터뷰(pillars_hint)에서 직접 말한 축 — 재생성이 못 건드림


class MandalaCell(CamelModel):
    """확정된 실행 셀 1개 — 한 축(subgoal_index)당 최대 8개(order_index 0~7)."""

    subgoal_index: int = Field(ge=0, le=7)
    order_index: int = Field(ge=0, le=7)
    title: str = Field(min_length=1, max_length=16)  # §7.7 — depth2 ≤16자
    source: MandalaSource = "llm"


class MandalaGap(CamelModel):
    """못 채운 칸 — 억지 패딩 대신 사유만 남긴다(`goal_decompose.v1.md` 의 패턴과 동일)."""

    subgoal_index: int = Field(ge=0, le=7)
    order_index: int = Field(ge=0, le=7)
    reason: str


class MandalaCenterPreview(CamelModel):
    """중앙 칸(궁극목표 본문) 미리보기."""

    title: str
    why_text: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# 요청
# ─────────────────────────────────────────────────────────────────────────────


class MandalaSubgoalsRequest(CamelModel):
    """POST /plans/mandala/subgoals(U2) 요청 — Stage A. lock 없음, DB 쓰기 0."""

    goal_id: str


class MandalaGenerateRequest(CamelModel):
    """POST /plans/mandala/generate(U3) 요청 — Stage A 에서 사용자가 확인·편집한 8축 그대로.

    구조(축 개수·순서) 편집은 여기까지다 — Stage B 이후 축은 규모가 고정된다.
    """

    goal_id: str
    subgoals: list[MandalaSubgoal] = Field(min_length=8, max_length=8)


class MandalaRegenerateBranchRequest(CamelModel):
    """POST /plans/mandala/{planId}/regenerate-branch(U5) 요청 — 링(8칸) 1개만 재생성.

    `edited_subgoals`/`edited_cells` 는 재생성 대상이 아닌 나머지 칸의 **현재 편집 상태**를
    함께 실어 보낸다 — 서버는 검증 없이 그대로 되돌려줄 뿐이다(HITL, 승인 전 편집은 로컬
    상태가 권위). 비우면 저장된 draft 스냅샷을 그대로 쓴다.
    """

    subgoal_index: int = Field(ge=0, le=7)
    user_hint: str | None = None
    edited_subgoals: list[MandalaSubgoal] = Field(default_factory=list)
    edited_cells: list[MandalaCell] = Field(default_factory=list)


class MandalaApproveRequest(CamelModel):
    """POST /plans/mandala/{planId}/approve(U6) 요청 — 승인 전 편집본을 통째로 실어 보낸다.

    셀 편집(HITL 최하위 층)은 승인 전까지 서버 호출이 없다(§7.6) — 최종 제출에 실려서야
    처음 서버에 닿는다.
    """

    center_why_text: str | None = None
    subgoals: list[MandalaSubgoal] = Field(min_length=8, max_length=8)
    cells: list[MandalaCell] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 응답 — Draft Layer(DraftMixin: is_draft/ai_source 강제)
# ─────────────────────────────────────────────────────────────────────────────


class MandalaSubgoalsResponse(DraftMixin):
    """U2 응답 — Stage A. lock 없음, DB 쓰기 0."""

    goal_id: str
    center: MandalaCenterPreview
    subgoals: list[MandalaSubgoal]  # 항상 8


class MandalaDraftResponse(DraftMixin):
    """U3/U4/U5 응답 — Stage B 결과(초안) 스냅샷. `plan_drafts.payload`(kind="mandala") 대응."""

    plan_id: str
    goal_id: str
    center: MandalaCenterPreview
    subgoals: list[MandalaSubgoal]  # 8
    cells: list[MandalaCell]  # ≤64
    gaps: list[MandalaGap]
    generated_at: KstDatetime


class MandalaCarryOverSummary(CamelModel):
    """U6 응답의 승계 절 — **다시 세우기**일 때만 0이 아니다(처음 세우면 전부 0/빈 배열).

    앞의 셋은 새 트리로 **이어진** 개수, 뒤의 둘은 자리를 잃어 **끊긴** 것의 이름이다.
    끊긴 쪽도 지워지지 않는다 — 승격된 목표는 그대로 남고(축 배지만 빠짐), 습관은 링크만
    끊겨 단독 습관이 된다. FE 는 승인 직후 이 두 배열을 그대로 보여주면 된다.
    """

    completed_cells: int = 0
    promoted_axes: int = 0
    linked_habits: int = 0
    dropped_promoted_axes: list[str] = Field(default_factory=list)
    dropped_linked_habits: list[str] = Field(default_factory=list)


class MandalaApproveResponse(CamelModel):
    """U6 응답 — 명시 승인 endpoint 이므로 `is_draft=False`(ADR-0005 §7.2)."""

    plan_id: str
    is_draft: Literal[False] = False
    goal_id: str
    root_node_id: str
    activated: int  # 영속된 goal_nodes 수 (1 + 8 + 채워진 leaf 수)
    skipped: int  # gaps 로 남아 저장하지 않은 칸 수
    carried_over: MandalaCarryOverSummary = Field(default_factory=MandalaCarryOverSummary)
    activated_at: KstDatetime


# ─────────────────────────────────────────────────────────────────────────────
# 다시 세우기 사전 확인 (U13) — 승인 전 HITL 미리보기
# ─────────────────────────────────────────────────────────────────────────────


class MandalaRebuildPromotedAxis(CamelModel):
    """다시 세우기에 걸려 있는 승격된 축 1개."""

    order_index: int = Field(ge=0, le=7)
    axis_title: str
    goal_id: str
    goal_title: str
    goal_status: str
    goal_tier: GoalTier


class MandalaRebuildLinkedHabit(CamelModel):
    """다시 세우기에 걸려 있는 반복형 칸 1개(ADR-0008 §1)."""

    subgoal_index: int = Field(ge=0, le=7)
    order_index: int = Field(ge=0, le=7)
    cell_title: str
    habit_id: str
    habit_title: str
    frequency_per_week: int


class MandalaRebuildPreflightResponse(CamelModel):
    """GET /goals/{goalId}/mandala/rebuild-preflight(U13) 응답 — 읽기 전용, LLM 0콜, DB 쓰기 0.

    "다시 세우기" 버튼이 확인 시트를 띄우기 위한 자료다. 만다라트를 다시 세우면 옛 트리는
    보관되고 새 73칸이 들어서는데, 그 사이에서 **사용자가 손으로 쌓은 것**(완료 표시·축
    승격·습관 링크)은 새 트리에 **제목이 같은 자리가 있을 때만** 이어진다. 무엇이 걸려
    있는지 미리 보여주지 않으면 승인 한 번에 조용히 끊긴다 — 그걸 막는 것이 이 endpoint 다.

    아직 승인된 트리가 없으면 `hasTree=false` + 전부 0/빈 배열(404 아님) — 처음 세우는
    경우도 FE 가 같은 경로를 타고 확인 시트만 건너뛴다.
    """

    goal_id: str
    has_tree: bool
    root_node_id: str | None
    statement: str
    total_cells: int
    completed_cells: int
    promoted_axes: list[MandalaRebuildPromotedAxis]
    linked_habits: list[MandalaRebuildLinkedHabit]
    live_action_items: int
    warnings: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# 조회·편집·승격 (U8~U10, PR6)
# ─────────────────────────────────────────────────────────────────────────────


class MandalaNode(GoalNode):
    """만다라 노드 1개(U8 응답 원소, U9 응답 그대로) — `GoalNode` additive 확장(§6.2).

    `progress`/`coverage` 는 컬럼 캐시가 아니라 매 조회 시 파생한다(§7.8, `goal_nodes.progress`
    컬럼을 만들지 않는 이유는 `mandala_adapter.compute_progress` docstring 참고). U9(단일 노드
    편집) 응답에서는 롤업을 다시 계산하지 않고 둘 다 `null` — 필요하면 U8 을 다시 부른다.
    """

    why_text: str | None
    source: MandalaSource
    locked: bool
    completed_at: KstDatetime | None
    promoted_goal_id: str | None
    # 반복형 칸(ADR-0008 §1) — 링크된 활성 습관이 있으면 그 id, 없으면 null(=프로젝트형).
    # U9/U10 응답처럼 롤업을 다시 계산하지 않는 자리에서도 항상 채운다 — 링크 API 자체가
    # 반환하는 값이라 별도 조회가 필요 없다.
    habit_id: str | None
    progress: float | None
    coverage: float | None


class MandalaTreeResponse(CamelModel):
    """GET /goals/{goalId}/mandala(U8) 응답 — 73노드(≤) + 진척도.

    아직 승인된 만다라 트리가 없으면 `nodes=[]`·`rootNodeId=null`(404 아님 — `GET
    /goals/{id}/nodes` 가 미승인 계획에 대해 이미 쓰는 것과 같은 "정상, 그냥 비어 있음" 규약).
    """

    goal_id: str
    root_node_id: str | None
    statement: str
    nodes: list[MandalaNode]
    progress: float
    coverage: float


class MandalaNodeUpdateRequest(CamelModel):
    """PATCH /goals/mandala/nodes/{nodeId}(U9) 요청 — 준 필드만 갱신, 나머지는 불변.

    어떤 필드든 갱신되면 `source="user"` 로 전환된다(AI/rule 이 채운 칸을 사용자가
    손댔다는 뜻이라 FE 의 점선 렌더가 실선으로 바뀐다).
    """

    title: str | None = Field(default=None, min_length=1)
    why_text: str | None = None
    completed: bool | None = None  # true→completed_at=now, false→completed_at=null


class MandalaPromoteRequest(CamelModel):
    """POST /goals/mandala/nodes/{nodeId}/promote(U10) 요청."""

    goal_tier: GoalTier


class MandalaHabitLinkRequest(CamelModel):
    """POST /goals/mandala/nodes/{nodeId}/habit(U12) 요청 — 반복형 칸으로 전환(ADR-0008 §1).

    "코딩테스트 1일 1문제"·"쓰레기 줍기" 처럼 끝이 없는 leaf 칸에 새 `Habit` 을 만들어
    링크한다. 계획(action_item)을 만들지 않고 이후 주간 횟수(habit_instances.done_count)로만
    추적한다. `title` 을 생략하면 칸 제목을 그대로 쓴다.
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    category: HabitCategory = "other"
    frequency_per_week: int = Field(ge=1, le=7)
    minutes_per_session: int = Field(ge=1)
    time_preference: TimePreference = "anytime"
    priority_level: int = Field(ge=1, le=5, default=3)


__all__ = [
    "MandalaApproveRequest",
    "MandalaApproveResponse",
    "MandalaCarryOverSummary",
    "MandalaCell",
    "MandalaCellItem",
    "MandalaCellPlan",
    "MandalaCenterPreview",
    "MandalaDraftResponse",
    "MandalaGap",
    "MandalaGenerateRequest",
    "MandalaHabitLinkRequest",
    "MandalaNode",
    "MandalaNodeUpdateRequest",
    "MandalaPromoteRequest",
    "MandalaRebuildLinkedHabit",
    "MandalaRebuildPreflightResponse",
    "MandalaRebuildPromotedAxis",
    "MandalaRegenerateBranchRequest",
    "MandalaSubgoal",
    "MandalaSubgoalItem",
    "MandalaSubgoalPlan",
    "MandalaSubgoalsRequest",
    "MandalaSubgoalsResponse",
    "MandalaTreeResponse",
]
