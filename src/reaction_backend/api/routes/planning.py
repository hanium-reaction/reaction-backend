"""Planning — Weekly plan (S06, S14, S15, S16).

핵심 흐름 (Orchestrator 1 — Goal Structuring):
  VALIDATING → PLANNING → REVIEWING → HITL → SAVING

규칙:
- 입력: time_policies + goals + habits + behavioral_profiles + interview + calendar freebusy
- horizon = focus goals의 가장 먼 deadline
- 출력: action_items + scheduled_blocks + dependency_links + habit_instances
- 정책 위반(수면/점심 슬롯) 블록 생성 시 트랜잭션 롤백
- 모든 변경은 사용자 [승인] 후 적용 (Draft Layer)

DB: action_items, scheduled_blocks, dependency_links, llm_runs

#3-B 단계는 **정적 mock 스텁**: `DEMO_PLAN_ID` 한 계획만 유효한 것으로 취급하고,
김민수 페르소나의 그럴듯한 horizon 계획을 반환한다. Goal Structuring orchestrator·
정책 위반 롤백·15분 snap 재배치·LLM 자연어 편집의 실구현은 #5/#6.

endpoint (api-contract §8):
- POST  /plans/generate                 — 첫 계획 또는 재생성 (S06)
- GET   /plans/{plan_id}                — 미리보기 (workload, conflicts 포함)
- POST  /plans/{plan_id}/approve        — 사용자 승인 → 활성화
- PATCH /plans/{plan_id}/blocks/{id}    — 직접 편집 (S15, 15분 snap)
- POST  /plans/{plan_id}/ai-edit        — 자연어 수정 (S16, P1)
- GET   /plans/weekly?week=...          — 주간 그리드 데이터 (S14)

구현 위치: agents/{validation,planning,scheduler,review}_agent.py + orchestrator/goal_structuring.py
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, status

from reaction_backend.api.mock.planning import (
    DEMO_PLAN_ID,
    build_plan,
    build_plan_diff,
    build_weekly_grid,
    find_demo_block,
)
from reaction_backend.schemas.common import KST
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.planning import (
    AiEditRequest,
    BlockEditRequest,
    GeneratePlanRequest,
    Plan,
    PlanDiff,
    ScheduledBlock,
    WeeklyGrid,
)

router = APIRouter(prefix="/plans", tags=["planning"])

# 15분 격자 — S15 직접 편집은 시작/종료를 이 단위로 snap 한다.
_SNAP_MINUTES = 15


def _ensure_demo_plan(plan_id: str) -> None:
    """스텁은 DEMO_PLAN_ID 만 유효한 계획으로 취급 — 그 외는 404."""
    if plan_id != DEMO_PLAN_ID:
        raise ApiError(
            ErrorCode.PLAN_NOT_FOUND,
            "해당 계획을 찾을 수 없어요.",
            http_status=status.HTTP_404_NOT_FOUND,
        )


def _snap_15min(dt: datetime) -> datetime:
    """datetime 을 가장 가까운 15분 격자로 정렬 (S15).

    naive 입력은 KST 로 간주한다 — 주간 그리드는 KST 기준 화면이다.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    dt = dt.replace(second=0, microsecond=0)
    remainder = dt.minute % _SNAP_MINUTES
    snapped = dt - timedelta(minutes=remainder)
    if remainder * 2 >= _SNAP_MINUTES:  # 절반 이상이면 다음 격자로 반올림
        snapped += timedelta(minutes=_SNAP_MINUTES)
    return snapped


@router.post("/generate", status_code=status.HTTP_201_CREATED)
async def generate_plan(body: GeneratePlanRequest | None = None) -> Plan:
    """[stub] 첫 계획 또는 재생성 (S06) — Goal Structuring orchestrator 실행.

    스텁은 입력(정책·goal·habit·interview·freebusy)을 읽지 않고 고정 데모 계획을
    DRAFT 상태로 반환한다. 승인 전이므로 캘린더/실행에 자동 적용되지 않는다.
    """
    return build_plan("DRAFT")


@router.get("/weekly")
async def get_weekly_grid(week_start: date | None = None) -> WeeklyGrid:
    """[stub] 주간 그리드 데이터 (S14) — 7열, 요일별 블록.

    `weekStart` 미지정 시 이번 주(월요일 기준)를 반환한다.
    """
    return build_weekly_grid(week_start)


@router.get("/{plan_id}")
async def get_plan(plan_id: str) -> Plan:
    """[stub] 계획 미리보기 — workloadLevel·conflicts·warnings 포함."""
    _ensure_demo_plan(plan_id)
    return build_plan("DRAFT")


@router.post("/{plan_id}/approve")
async def approve_plan(plan_id: str) -> Plan:
    """[stub] 사용자 승인 → 활성화 (HITL 게이트).

    Draft Layer 의 계획을 ACTIVE 로 전이한다. 실구현에서는 인터뷰 미완료 시
    401 `INTERVIEW_REQUIRED_FIRST` 가능 — 스텁은 인터뷰 완료를 전제로 한다.
    """
    _ensure_demo_plan(plan_id)
    return build_plan("ACTIVE")


@router.patch("/{plan_id}/blocks/{block_id}")
async def edit_block(plan_id: str, block_id: str, body: BlockEditRequest) -> ScheduledBlock:
    """[stub] 블록 직접 편집 (S15) — 시작/종료를 15분 격자로 snap.

    start 만 옮기면 블록 길이는 보존된다. 종료가 시작보다 앞서면 400.
    """
    _ensure_demo_plan(plan_id)
    block = find_demo_block(block_id)
    if block is None:
        raise ApiError(
            ErrorCode.PLAN_BLOCK_NOT_FOUND,
            "해당 블록을 찾을 수 없어요.",
            http_status=status.HTTP_404_NOT_FOUND,
        )

    new_start = block.start_at
    new_end = block.end_at
    if body.start_at is not None:
        new_start = _snap_15min(body.start_at)
        if body.end_at is None:  # start 만 이동 → 길이 보존
            new_end = new_start + (block.end_at - block.start_at)
    if body.end_at is not None:
        new_end = _snap_15min(body.end_at)
    if new_end <= new_start:
        raise ApiError(
            ErrorCode.PLAN_INVALID_BLOCK_TIME,
            "블록 종료 시각은 시작 시각보다 뒤여야 해요.",
            field="endAt",
        )

    return block.model_copy(
        update={
            "title": block.title if body.title is None else body.title,
            "start_at": new_start,
            "end_at": new_end,
        }
    )


@router.post("/{plan_id}/ai-edit")
async def ai_edit_plan(plan_id: str, body: AiEditRequest) -> PlanDiff:
    """[stub] 자연어 수정 (S16, P1) — diff 만 반환.

    실제 적용은 `/ai-edit/apply` 에서 사용자 승인 후 수행한다 (HITL). 스텁은
    LLM 호출 없이 고정 수정안(diff)을 반환한다 — 실구현은 llm/ Tool Executor 경유.
    """
    _ensure_demo_plan(plan_id)
    return build_plan_diff(body.instruction)
