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

from pydantic import BaseModel, Field

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
    # 세션 길이는 목표별 goals.session_length(최대 2시간)까지 허용 — 60 상한이면 90/120분
    # 세션이 구조적으로 불가능해 그 값들이 무시됐다(#per-goal). 240 은 여유 상한.
    estimated_minutes: int = Field(ge=1, le=240)
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
# `plan_quality_eval.v4` — **평가 후보** 검토기 출력 계약 (루브릭 §4).
#
# ⚠️ **프로덕션은 이것을 쓰지 않는다.** ④층 `review_plan` 은 여전히 `PlanReview`(v3)다.
# 이 스키마는 `scripts/l1_7b_v4_run.py` 평가 하네스 전용이고, 승격 조건은
# `docs/experiments/l1-7b-v4-results.md` 에 적혀 있다.
#
# `CamelModel` 이 아니라 순수 `BaseModel` 을 쓰는 이유 — 이 스키마는 **API 경계를 넘지
# 않는다.** camelCase 변환(api-contract §1.9)은 응답 스키마의 규칙이고, 여기서 그걸 쓰면
# 프롬프트가 요구하는 `node_id` 와 Gemini 에 넘어가는 `response_schema` 의 `nodeId` 가
# 갈린다. 같은 이름 하나로 두는 편이 프롬프트·스키마·집계를 대조하기 쉽다.
# ─────────────────────────────────────────────────────────────────────────────

DefectCode = Literal["D1", "D2", "D3", "D4", "D5"]
"""루브릭 §2 의 결함 5종. 자유 문자열을 받으면 M28a(유형 지목)를 기계가 못 센다."""


class PlanFinding(BaseModel):
    """검토기가 지목한 결함 한 건.

    v3 의 `feedback: list[str]` 이 자유 문장이라 **무엇을 왜 지적했는지 기계가 셀 수 없었다.**
    M27b(옳은 유형)·M28a(유형 지목)·M28b(위치 지목)가 전부 이 구조를 요구한다.
    """

    defect: DefectCode
    severity: int = Field(ge=1, le=3)
    """1=경계·통과 권고 / 2=결함 / 3=명백. **반려 임계값은 프롬프트가 아니라 코드가 정한다**
    (`approved_from_findings`) — 임계값을 바꿔가며 M27b/M29 를 다시 계산해 운영점을 고르기
    위해서다. LLM 이 `approved` 를 직접 내면 그 곡선을 그릴 수 없다."""

    node_id: str = Field(min_length=1)
    """지목한 leaf 의 `node_id`. **입력 계획에 실제로 있는 값인지는 스키마가 못 본다** —
    하네스가 `classify_findings` 로 대조하고, 없는 id 는 무효로 표시한다."""

    message: str = Field(min_length=1)
    """사용자 친화 제안 문장. 톤 규칙은 v3 와 동등하다(탓하지 않는 제안형, 금지어 동일)."""


class PlanReviewV4(BaseModel):
    """`plan_quality_eval.v4` 출력 — **`approved` 필드가 없다.**

    LLM 이 승인/반려를 직접 결정하지 않는다. 코드가 `approved_from_findings` 로 정한다.
    `findings` 가 비면 승인이다.
    """

    findings: list[PlanFinding] = Field(default_factory=list)


REJECT_SEVERITY_THRESHOLD = 2
"""이 값 이상인 finding 이 하나라도 있으면 반려. 운영점 탐색 시 바꿔가며 재집계한다."""


def approved_from_findings(
    findings: list[PlanFinding], *, threshold: int = REJECT_SEVERITY_THRESHOLD
) -> bool:
    """반려 판정 — **코드가 정한다.**

    `findings` 가 비면 `True`. 그 외에는 `severity >= threshold` 인 것이 하나도 없을 때만
    `True` 다. 순수 함수라 저장된 원자료에 임계값을 바꿔 다시 적용할 수 있다.
    """
    return not any(f.severity >= threshold for f in findings)


class MilestoneDraft(CamelModel):
    """중간 목표(마일스톤) 한 개 — 사용자가 확인·편집하는 계획 뼈대 단위(#milestones Phase 2).

    사용자가 이 목록을 보고 수정/재배열/추가/삭제해 방향을 확정하면, 분해(Stage B)가 각
    마일스톤을 branch 로 고정하고 그 안에서만 세션(leaf)을 만든다.
    """

    title: str
    summary: str = ""  # 이 마일스톤에서 무엇을 이루는지 한 줄


class MilestonePlan(CamelModel):
    """plan_milestones LLM 출력 — 목표를 향한 3~5개 중간 목표."""

    milestones: list[MilestoneDraft] = Field(min_length=1, max_length=6)


class MilestoneListResponse(CamelModel):
    """POST /plans/milestones 응답 — 사용자 확인용 마일스톤 초안(Stage A)."""

    milestones: list[MilestoneDraft]
    # "saved" = 이 목표에 이미 확정·영속된 뼈대를 그대로 돌려준 것(LLM 0콜, ADR-0007 PR-2.5).
    # 2주기 이후의 정상 경로다 — 뼈대는 마감까지 살아남는 층이고 주기마다 바뀌는 건 leaf 뿐이다.
    ai_source: Literal["llm", "rule", "saved"] = "llm"


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
    # 사용자가 확인·편집해 확정한 중간 목표(#milestones Stage B). 있으면 분해가 이걸 branch 로
    # 고정하고 각 안에서만 세션을 만든다. 없으면 현행(자동 전체 분해) — 하위호환.
    milestones: list[MilestoneDraft] | None = None
    target_date: str | None = None  # "YYYY-MM-DD" — 미지정 시 오늘(KST) 기준
    # 배치 범위: "horizon"(기본, 마감까지 전 구간 — 실행이 마감 전 여러 날에 분배되고, 주간
    # 재계획이 이후를 다시 씀) | "week"(target_date 가 속한 달력 주만 — 가벼운 단기 계획).
    scope: Literal["week", "horizon"] = "horizon"
    # 계획 분량(밀도) — 사용자가 재생성 시 조절. 분해(LLM) 프롬프트에 '주당 목표 세션 수'로
    # 전달돼 생성되는 action_item 수의 하한 가이드가 된다. light≈3 / standard≈5 / intense≈8 세션/주.
    density: Literal["light", "standard", "intense"] = "standard"


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

    `warnings`(#371, additive) — Focus≤3/Maintain≤5 한도를 넘겨 parked 로 내린 목표가
    있으면 실린다. 대개 빈 리스트.
    """

    plan_id: str
    is_draft: Literal[False] = False
    activated_goals: int
    activated_goal_nodes: int
    activated_action_items: int
    activated_blocks: int
    activated_at: KstDatetime
    warnings: list[str] = Field(default_factory=list)


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
    # 승인 요청에 실어 보낸 확정 마일스톤을 그대로 되비춘다(additive) — 이걸 승인
    # (approve_plan)이 읽어 node_type='milestone' 로 영속한다(ADR-0007 PR-2).
    milestones: list[MilestoneDraft] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 주간 forward 재계획 (POST /plans/replan) — 남은 작업을 이후로 다시 배치.
# 기존 goal/node/action 재사용, 미래 미착수 블록만 교체(중복 0). 항상 Draft.
# ─────────────────────────────────────────────────────────────────────────────


class ReplanBlockPreview(CamelModel):
    """재계획 미리보기 블록 — 기존 ActionItem 에 연결(actionId). 시각은 KST.

    `replacesBlockId` 는 이 새 블록이 교체하는 옛 미래 블록의 **대표 id**(미리보기용, 없으면
    백로그라 null). 실제 승인 재조정은 payload 의 `oldBlocks`(액션당 옛 블록 **전부**)를 권위로
    삼아 액션 단위로 취소·생성한다 — 한 액션이 여러 세션으로 쪼개진 경우까지 정확히(#117).
    """

    action_id: str  # action_<uuid>
    title: str
    category: str
    start: KstDatetime
    end: KstDatetime
    replaces_block_id: str | None = None  # block_<uuid> 대표 1개 | null (백로그)


class ReplanResponse(DraftMixin):
    """주간 재계획 미리보기 — 항상 Draft. 승인은 `/plans/replan/{planId}/approve`."""

    plan_id: str
    window_start: str  # "YYYY-MM-DD" — 재배치 시작(다음 주 월요일)
    horizon: str | None
    blocks: list[ReplanBlockPreview]
    warnings: list[str] = Field(default_factory=list)
    generated_at: KstDatetime


class WeeklyReplanApproveResponse(CamelModel):
    """주간 forward 재계획 승인 결과 — 재조정 카운트. `is_draft=False`.

    started/finished 로 바뀐 옛 블록·다른 계획 승인분은 건드리지 않으므로(재조정),
    `skipped` 는 그렇게 보존된 항목 수다.

    ⚠️ 이름 주의: `schemas/recovery.py` 의 `ReplanApproveResponse`(S20 실행단위 replan,
    `POST /replan/{executionId}/approve`)와 **다른 것**이다. 같은 이름을 쓰면 FastAPI 가
    중복 모델명을 양쪽 다 full-qualify 로 바꿔(`reaction_backend__schemas__recovery__...`)
    이 PR 이 건드리지도 않은 회복 endpoint 의 OpenAPI 컴포넌트명이 변하고 FE 생성
    클라이언트가 깨진다. 'Weekly' 접두사는 그 충돌을 피하기 위한 것 — 지우지 말 것.
    """

    plan_id: str
    is_draft: Literal[False] = False
    cancelled_blocks: int
    created_blocks: int
    skipped_blocks: int
    activated_at: KstDatetime


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
