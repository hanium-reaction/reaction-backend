"""Planning 도메인 스키마 (api-contract §8) — S06·S14·S15·S16.

#3-B 단계는 정적 mock 스텁. Goal Structuring Orchestrator·정책 검증·15분 snap
재배치·LLM 자연어 편집의 실구현은 #5/#6.

응답 규약 (common.py 진실 소스):
- 성공 응답은 envelope 없이 도메인 객체(`Plan` / `WeeklyGrid` / `PlanDiff` / …)를
  **직접** 반환한다.
- datetime 필드는 `KstDatetime` — JSON 직렬화 시 KST(+09:00)로 자동 변환된다.
- 필드는 camelCase 로 입출력 (`CamelModel`).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime

# ──────────────────────────────────────────────────────────────────────────
# 도메인 객체
# ──────────────────────────────────────────────────────────────────────────


class ActionItem(CamelModel):
    """계획이 만든 실행 단위 — goal 을 잘게 쪼갠 작업 (DB: action_items)."""

    action_item_id: str
    goal_id: str
    title: str
    estimated_minutes: int = Field(ge=0)
    deadline: date | None
    status: str = Field(description="DRAFT | PLANNED | ACTIVE | DONE")


class ScheduledBlock(CamelModel):
    """주간 그리드에 놓이는 시간 블록 (DB: scheduled_blocks).

    `blockType`: focus(집중) | habit(습관) | fixed(고정일정) | break(휴식).
    시작/종료는 15분 격자에 정렬된다 (S15 직접 편집 규칙).
    """

    block_id: str
    action_item_id: str | None
    title: str
    block_type: str = Field(description="focus | habit | fixed | break")
    start_at: KstDatetime
    end_at: KstDatetime
    status: str = Field(description="DRAFT | ACTIVE")


class Conflict(CamelModel):
    """미리보기에서 감지된 충돌 한 건 (GET /plans/{planId})."""

    block_id: str
    reason: str = Field(description="CALENDAR_OVERLAP | POLICY_SLEEP | POLICY_LUNCH | OVERLOAD")
    detail: str


class WeekPlan(CamelModel):
    """horizon 안의 한 주 — generate 응답의 `weeks[]` 항목."""

    week_start: date
    workload_level: str = Field(description="low | medium | high")
    warnings: list[str]
    action_items: list[ActionItem]
    scheduled_blocks: list[ScheduledBlock]


class Plan(CamelModel):
    """계획 도메인 객체 — generate·get·approve 공통 응답.

    `status`: DRAFT(미승인, Draft Layer) → ACTIVE(사용자 승인 후 활성화).
    승인 전에는 어떤 변경도 캘린더/실행에 자동 적용되지 않는다 (HITL).
    """

    plan_id: str
    status: str = Field(description="DRAFT | ACTIVE")
    horizon_end: date = Field(description="focus goal 중 가장 먼 deadline")
    workload_level: str = Field(description="low | medium | high")
    generated_at: KstDatetime
    approved_at: KstDatetime | None
    conflicts: list[Conflict]
    warnings: list[str]
    weeks: list[WeekPlan]


# ──────────────────────────────────────────────────────────────────────────
# 주간 그리드 (S14)
# ──────────────────────────────────────────────────────────────────────────


class DayColumn(CamelModel):
    """주간 그리드의 하루 열 — 요일 + 그날의 블록."""

    date: date
    weekday: str = Field(description="월 | 화 | 수 | 목 | 금 | 토 | 일")
    blocks: list[ScheduledBlock]


class WeeklyGrid(CamelModel):
    """GET /plans/weekly 응답 — 7열 주간 그리드 데이터."""

    plan_id: str
    week_start: date
    week_end: date
    workload_level: str = Field(description="low | medium | high")
    days: list[DayColumn]


# ──────────────────────────────────────────────────────────────────────────
# AI 자연어 편집 (S16)
# ──────────────────────────────────────────────────────────────────────────


class BlockChange(CamelModel):
    """ai-edit diff 의 블록 변경 한 건.

    `before`/`after` 한쪽이 None 이면 add/remove, 둘 다 있으면 move/resize.
    """

    block_id: str
    change_type: str = Field(description="move | resize | add | remove")
    before: ScheduledBlock | None
    after: ScheduledBlock | None
    reason: str


class PlanDiff(CamelModel):
    """POST /plans/{planId}/ai-edit 응답 — diff 만 반환.

    실제 적용은 별도 endpoint(`/ai-edit/apply`)에서 사용자 승인 후 수행한다 (HITL).
    """

    plan_id: str
    instruction: str
    summary: str
    changes: list[BlockChange]
    requires_approval: bool = True


# ──────────────────────────────────────────────────────────────────────────
# 요청 스키마
# ──────────────────────────────────────────────────────────────────────────


class GeneratePlanRequest(CamelModel):
    """POST /plans/generate 요청 — 첫 계획 또는 재생성 (S06).

    모든 필드 선택값 — 빈 body(`{}`)로도 호출할 수 있다.
    """

    regenerate: bool = Field(default=False, description="true 면 기존 계획 폐기 후 재생성")
    horizon_end: date | None = Field(default=None, description="미지정 시 focus deadline 으로 산출")
    note: str | None = Field(default=None, max_length=500, description="재생성 시 사용자 의도")


class BlockEditRequest(CamelModel):
    """PATCH /plans/{planId}/blocks/{blockId} 요청 — 직접 편집 (S15).

    제공된 시각은 서버가 15분 격자에 snap 한다. start 만 옮기면 길이는 보존된다.
    """

    title: str | None = Field(default=None, min_length=1, max_length=120)
    start_at: datetime | None = None
    end_at: datetime | None = None


class AiEditRequest(CamelModel):
    """POST /plans/{planId}/ai-edit 요청 — 자연어 수정 (S16)."""

    instruction: str = Field(min_length=1, max_length=500, description="예: '수요일 일정을 줄여줘'")
