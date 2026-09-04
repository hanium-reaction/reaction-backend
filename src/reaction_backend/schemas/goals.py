"""Goals 도메인 스키마 (api-contract §6) — S26."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime

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
    status: str  # active | archived | completed | proposed
    # 이 목표에 **이번 주기 계획 트리가 있는가**. `GET /goals` 에서만 채운다(목록 조회 시점에
    # 한 번에 묻는다 — `GoalRepo.goal_ids_with_plan`). 그 외 응답은 기본값 `True` 로 둬서
    # 단건 응답이 카드를 **미계획으로 잘못 칠하지 않게** 한다(다음 목록 새로고침이 채운다).
    #
    # ⚠️ `status` 로 대신할 수 없다. 계획 승인은 인터뷰가 뽑은 목표를 **전부** `active` 로
    # 승격하는데 계획은 heaviest **하나**에만 생긴다 — 실측으로 계획 없는 active 가 24건.
    # "미계획" 배지는 이 값 하나로 판정한다: `proposed` 는 정의상 계획이 없으므로
    # `hasPlan=false` 가 **두 경우를 모두 덮는다**(인터뷰만 한 목표 + 계획을 못 받은 목표).
    has_plan: bool = True
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
    # 마일스톤 완료 표시(ADR-0007 §3 의 유일한 저장 예외).
    # `GET /goals/{goalId}/nodes`(계획 트리)에서는 `nodeType="milestone"` 이 아니면 항상
    # null — 세션 수행 여부는 `action_items.status` 가 진실 소스라 여기 복사본을 두지
    # 않는다. `PATCH /goals/{goalId}/nodes/{nodeId}` 로만 바뀐다.
    # ⚠️ 이 클래스를 상속하는 `MandalaNode` 는 다르다 — 만다라 칸은 subgoal/leaf 인 채로
    # 완료가 찍힌다(U9). 그래서 "milestone 이 아니면 null" 은 계획 트리 한정 사실이다.
    # `KstDatetime` 이어야 한다 — DB 는 UTC-aware 를 돌려주므로 그냥 datetime 이면
    # `...Z` 로 나가, 같은 컬럼을 읽는 만다라 응답(`+09:00`)과 표기가 갈린다(ADR-0002 §2.4).
    completed_at: KstDatetime | None = None


class GoalCompletionRequest(CamelModel):
    """POST /goals/{goalId}/complete 요청 — 목표 완료 확정/해제 (ADR-0007 6b).

    마일스톤 완료 표시(`PATCH /goals/{goalId}/nodes/{nodeId}`)와 **같은 모양**으로 뒀다 —
    둘 다 "끝났다" 를 사용자가 확정하는 HITL 이고, 둘 다 오조작을 되돌릴 수 있어야 한다.
    """

    completed: bool  # true → status="completed", false → "active"(되돌리기)


class MilestoneCompletionRequest(CamelModel):
    """PATCH /goals/{goalId}/nodes/{nodeId} 요청 — 마일스톤 완료 표시 (ADR-0007 §3 예외).

    제목·요약은 여기서 못 고친다. 뼈대 편집은 마일스톤 확인 화면 → `generate` →
    `approve` 경로 하나로 모여 있고(ADR-0007 PR-6a), 여기서도 고칠 수 있게 하면 같은
    사실을 바꾸는 길이 둘이 된다.
    """

    completed: bool  # true → completed_at=now(KST), false → null(완료 취소)


class GoalDecomposition(CamelModel):
    """GET /goals/{id}/nodes 응답 — 계획 승인 시 영속된 실제 분해 트리.

    계획을 아직 승인하지 않은 목표는 트리가 없다 → `nodes=[]`, `root_node_id=None`.
    """

    goal_id: str
    root_node_id: str | None
    nodes: list[GoalNode]
