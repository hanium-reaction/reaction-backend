"""Goals 도메인 스키마 (api-contract §6) — S26."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel

GoalTier = Literal["focus", "maintain", "parked"]


class Goal(CamelModel):
    """목표 — GET 응답 항목, POST/PATCH/park 응답."""

    goal_id: str
    title: str
    category: str
    goal_tier: str  # focus | maintain | parked
    priority_level: int
    deadline: str | None  # YYYY-MM-DD
    estimated_minutes: int | None
    status: str  # active | archived | completed
    # PR7 additive — S26 이 parked 목표 카드에 만다라 진입점을, 승격 목표 카드에 축 배지를
    # 달 수 있게. `GET /goals/{id}/mandala` 를 매 카드마다 probe 하는 N+1 을 피한다.
    is_ultimate: bool = False
    # 이 goal 이 만다라 축(하위목표) 승격으로 생겼으면 그 축 제목, 아니면 null. `GET /goals`
    # 와 `POST .../promote` 응답에서만 채운다(둘 다 조회 시점에 이미 알고 있어 쿼리가 싸다) —
    # create/update/park 응답은 매번 역조회하지 않고 null 로 둔다(다음 목록 새로고침이 채움).
    promoted_from_axis: str | None = None


class GoalsByTier(CamelModel):
    """GET /goals 응답 — tier 별 그룹."""

    focus: list[Goal]
    maintain: list[Goal]
    parked: list[Goal]


class GoalCreateRequest(CamelModel):
    """POST /goals 요청."""

    title: str = Field(min_length=1)
    category: str
    goal_tier: GoalTier
    priority_level: int = Field(ge=1, le=5)
    deadline: str | None = None
    estimated_minutes: int | None = None


class GoalUpdateRequest(CamelModel):
    """PATCH /goals/{id} 요청 — 제목·마감·우선순위·tier·category 변경.

    `category` 는 `GoalCreateRequest.category` 와 같은 허용값·정규화 규칙을 쓴다(#326,
    FE #216 차단 해소). 생략하면 기존 category 유지 — 재인터뷰 제안 트리거는 FE 가 저장
    성공 후 실제로 값이 달라졌을 때만 띄운다(자동 재계획·강제 이동 없음).
    """

    title: str | None = None
    category: str | None = None
    deadline: str | None = None
    priority_level: int | None = Field(default=None, ge=1, le=5)
    goal_tier: GoalTier | None = None


GoalNodeType = Literal["core", "subgoal", "milestone", "leaf"]


class GoalNode(CamelModel):
    """decompose 응답의 노드 (api-contract §6).

    `order_index`/`node_type`/`is_leaf` 는 PR6 에서 additive 로 추가됐다(만다라 렌더의 전제 —
    `orderIndex` 없이는 FE 가 8칸 중 몇 번째인지 알 수 없다). 기존 소비 코드는 무변경.
    """

    node_id: str
    parent_id: str | None
    title: str
    depth: int
    order_index: int
    node_type: GoalNodeType
    is_leaf: bool


class GoalDecomposition(CamelModel):
    """GET /goals/{id}/nodes 응답 — 계획 승인 시 영속된 실제 분해 트리.

    계획을 아직 승인하지 않은 목표는 트리가 없다 → `nodes=[]`, `root_node_id=None`.
    """

    goal_id: str
    root_node_id: str | None
    nodes: list[GoalNode]
