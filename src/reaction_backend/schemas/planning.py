"""Planning 도메인 스키마 (api-contract §8) — First Plan / Goal Structuring (#32).

두 종류:
1. **LLM Structured Output 스키마** (`GoalDecomposition` 등) — `aiClient.run(schema=...)` 강제
   검증. `prompts/planning/goal_decompose.v1.md` 의 JSON 출력 형식과 1:1 대응.
   룰 fallback (`orchestrator/goal_structuring.py`) 도 동일 schema 로 환원된다.
2. **경계/응답 스키마** — Deep Interview(#6) 의 `InterviewOutcome` 을 입력으로 받아
   First Plan 오케스트레이터를 실행하고, Draft Layer 로 미리보기를 반환한다.

모든 AI 산출 응답은 `DraftMixin` 을 상속 → 사용자 [수락] 전까지 `is_draft=True`
(AGENTS.md §1.4 잠금, ADR-0005 §7.2). 실제 영속화는 `/plans/{id}/approve` 이후.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, DraftMixin, KstDatetime
from reaction_backend.schemas.interview import InterviewOutcome

# ─────────────────────────────────────────────────────────────────────────────
# LLM Structured Output (LLM ②③) — goal_decompose.v1.md 출력 형식과 1:1 대응.
# node_id 는 LLM 이 만드는 temp_uuid (DB UUID 아님). SAVING 단계에서 실제 UUID 로 치환.
# ─────────────────────────────────────────────────────────────────────────────

GoalNodeType = Literal["root", "branch", "leaf"]


class GoalNodeDraft(CamelModel):
    """분해된 goal_node 한 개 (root → branch → leaf 트리)."""

    node_id: str  # temp_uuid (LLM 생성, SAVING 에서 실 UUID 치환)
    parent_id: str | None
    title: str
    node_type: GoalNodeType
    order_index: int = Field(ge=0)
    is_leaf: bool


class ActionItemDraft(CamelModel):
    """leaf 노드에 매달리는 실행 항목 — SMART + tiny_first_step."""

    node_id: str  # 소속 leaf 의 temp_uuid
    title: str
    estimated_minutes: int = Field(ge=1, le=60)  # leaf 는 60분 이내 (goal_decompose 규칙)
    category: str
    first_step: str  # 5분 내 시작 가능한 tiny first step


class PolicyViolation(CamelModel):
    """정책 위반으로 제외된 노드 + 사유 (cap / 충돌 등)."""

    node_id: str
    reason: str  # cap_exceeded | conflict | ...


class GoalDecomposition(CamelModel):
    """LLM ②③ 통합 결과 — goal_node 트리 + action_item + 정책 위반 목록.

    `prompts/planning/goal_decompose.v1.md` Structured Output 형식.
    """

    goal_nodes: list[GoalNodeDraft] = Field(min_length=1)
    action_items: list[ActionItemDraft] = Field(default_factory=list)
    policy_violations: list[PolicyViolation] = Field(default_factory=list)


class PlanReview(CamelModel):
    """LLM ④ — `prompts/planning/plan_quality` 독립 검토 결과."""

    approved: bool
    feedback: list[str] = Field(default_factory=list)  # 미승인 시 재계획 이슈 목록


# ─────────────────────────────────────────────────────────────────────────────
# 경계 입력 — First Plan 트리거 요청.
# ─────────────────────────────────────────────────────────────────────────────


class FirstPlanGenerateRequest(CamelModel):
    """POST /plans/generate (첫 계획) 요청.

    `interview_session_id` 로 확정된 `InterviewOutcome` 을 참조하거나(서버가 로드),
    온보딩 흐름에서 outcome 을 인라인 전달할 수 있다(`outcome`). 둘 중 하나는 필수 —
    검증은 라우터/오케스트레이터 VALIDATING 단계에서 수행.
    """

    interview_session_id: str | None = None
    outcome: InterviewOutcome | None = None
    target_date: str | None = None  # "YYYY-MM-DD" — 미지정 시 오늘(KST) 기준
    # 배치 범위: "horizon"(기본, 마감까지 전 구간 — 실행이 마감 전 여러 날에 분배되고, 주간
    # 재계획이 이후를 다시 씀) | "week"(target_date 가 속한 달력 주만 — 가벼운 단기 계획).
    scope: Literal["week", "horizon"] = "horizon"


# ─────────────────────────────────────────────────────────────────────────────
# 응답 — Draft Layer 미리보기 (DraftMixin: is_draft / ai_source 강제).
# ─────────────────────────────────────────────────────────────────────────────


class ScheduledBlockPreview(CamelModel):
    """미리보기용 스케줄 블록 — DB scheduled_blocks 대응(미영속). 시각은 KST 응답."""

    start: KstDatetime
    end: KstDatetime
    title: str
    category: str
    origin: Literal["habit", "goal"]
    origin_id: str | None = None


class FirstPlanApproveResponse(CamelModel):
    """승인 결과 — 활성화 완료. 명시 승인 endpoint 이므로 `is_draft=False` (ADR-0005 §7.2).

    #62: `plan_id` 로 저장된 Draft 를 로드해 goal 트리까지 영속화한 결과 카운트.
    """

    plan_id: str
    is_draft: Literal[False] = False
    activated_goals: int
    activated_goal_nodes: int
    activated_action_items: int
    activated_blocks: int
    activated_at: KstDatetime


class FirstPlanResponse(DraftMixin):
    """First Plan 미리보기 응답 — 항상 Draft (사용자 [수락] 전).

    `is_draft=True` 고정, `ai_source` 는 오케스트레이터 `used_fallback` 에 따라 라우터가 set.
    """

    plan_id: str  # draft plan 식별자 (승인 시 /plans/{plan_id}/approve)
    target_date: str  # "YYYY-MM-DD"
    horizon: str | None
    goal_nodes: list[GoalNodeDraft]
    action_items: list[ActionItemDraft]
    blocks: list[ScheduledBlockPreview]
    warnings: list[str] = Field(default_factory=list)
    policy_violations: list[PolicyViolation] = Field(default_factory=list)
    generated_at: KstDatetime


# ─────────────────────────────────────────────────────────────────────────────
# S14 Weekly Plan View + S15 직접 편집 (#21-B). 영속 scheduled_blocks 를 읽고/옮긴다.
# Plan 테이블은 없음 — planId 는 주(週) 논리 식별자(`plan_<weekStart>`), 편집 권한은 blockId.
# ─────────────────────────────────────────────────────────────────────────────


class WeeklyBlock(CamelModel):
    """주간 그리드의 스케줄 블록 한 칸."""

    block_id: str  # block_<uuid>
    action_id: str  # action_<uuid>
    title: str
    category: str
    # goal_<uuid> — 블록이 속한 목표 (action_item.goal_id 경유). 목표 미연결이면 null.
    # FE 주간 그리드가 블록을 목표 분류(집중/유지)·색상과 연결할 수 있게 한다.
    goal_id: str | None = None
    start_at: KstDatetime
    end_at: KstDatetime
    block_status: str
    source: str


class WeeklyPlanDay(CamelModel):
    """하루치 — 그리드/네비게이터 단위."""

    date: date
    weekday: str  # monday..sunday
    blocks: list[WeeklyBlock] = Field(default_factory=list)


class WeeklyPlanResponse(CamelModel):
    """GET /plans/weekly — 7일 블록 그리드 (모바일=1일 그리드+7일 네비게이터)."""

    plan_id: str
    week_start: date
    week_end: date
    days: list[WeeklyPlanDay]


class BlockEditRequest(CamelModel):
    """PATCH /plans/{planId}/blocks/{blockId} — 15분 snap 이동 + 목표(category)/제목 수정.

    `endAt` 생략 시 기존 길이를 보존한 채 시작만 옮긴다. 시각은 KST ISO 8601.
    `category`/`title` 을 주면 블록이 매달린 action_item 을 갱신한다 — 같은 액션의 모든
    세션 블록에 반영되며, 미지원 category 는 'other' 로 정규화한다. 미지정 필드는 유지.
    """

    start_at: str  # ISO 8601 (KST)
    end_at: str | None = None
    category: str | None = None  # 목표 카테고리 변경 (블록 색/분류) — 없으면 유지
    title: str | None = None  # 카드 제목 변경 — 없으면 유지


class BlockEditResponse(WeeklyBlock):
    """편집 결과 — 스냅 적용된 최종 블록."""
