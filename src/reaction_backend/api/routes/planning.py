"""Planning — Weekly plan (S06, S14, S15, S16).

핵심 흐름 (Orchestrator 1 — Goal Structuring, architecture.md §2.1):
  VALIDATING → PLANNING → REVIEWING → HITL → SAVING → DONE

규칙 (AGENTS §1·§2):
- 입력: time_policies + goals + habits + behavioral_profiles + interview + calendar freebusy
- 정책 위반(수면/노터치/심야) 블록 생성 시 트랜잭션 롤백 (`PolicyViolationError`)
- 모든 변경은 사용자 [수락] 후 적용 (Draft Layer) — 본 라우터의 `isActive` 는 항상 false

DB: action_items, scheduled_blocks, dependency_links, llm_runs

엔드포인트:
- POST  /plans/generate                 — 첫 계획 / 재생성 (S06) — **이 PR 에서 구현**
- GET   /plans/{plan_id}                — 미리보기 (workload, conflicts 포함)
- POST  /plans/{plan_id}/approve        — 사용자 승인 → 활성화
- PATCH /plans/{plan_id}/blocks/{id}    — 직접 편집 (S15, 15분 snap)
- POST  /plans/{plan_id}/ai-edit        — 자연어 수정 (S16, P1)
- GET   /plans/weekly?week=...          — 주간 그리드 데이터 (S14)

구현 위치: agents/{validation,planning,scheduler,review}_agent.py
         + orchestrator/goal_structuring.py (#18 — 본 PR 에서 연결)
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from reaction_backend.api.mock.fixed_schedules import DEMO_FIXED_SCHEDULES
from reaction_backend.api.mock.habits import DEMO_HABITS
from reaction_backend.api.mock.time_policies import DEMO_POLICIES
from reaction_backend.orchestrator.goal_structuring import (
    DraftPlan,
    FixedScheduleLike,
    GoalStructuringInput,
    GoalStructuringOrchestrator,
    HabitLike,
    PolicyViolationError,
    TimePolicyLike,
)
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.planning import (
    BusyBlockSchema,
    DraftBlockSchema,
    GeneratePlanRequest,
    GeneratePlanResponse,
    TimeWindowSchema,
)

router = APIRouter(prefix="/plans", tags=["planning"])


# ─────────────────────────────────────────────────────────────────────────────
# 데모 데이터 → 오케스트레이터 입력 어댑터
#
# mock 의 정책 payload 는 API contract 표기인 camelCase("startTime") 로 저장돼
# 있는데, 오케스트레이터(=DB 저장 표기) 는 snake_case("start_time") 를 기대한다.
# 두 표기를 정규화하는 얇은 어댑터로 둘 사이를 잇는다. 실 DB 연결 단계에서는
# Repository 가 이 위치를 대체한다.
# ─────────────────────────────────────────────────────────────────────────────


_PAYLOAD_KEY_MAP: dict[str, str] = {
    "startTime": "start_time",
    "endTime": "end_time",
    "minMinutes": "min_minutes",
    "daysOfWeek": "days_of_week",
    "blockedCategories": "blocked_categories",
}


def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """API contract 의 camelCase payload 키를 orchestrator 가 보는 snake_case 로 정규화."""
    return {_PAYLOAD_KEY_MAP.get(k, k): v for k, v in payload.items()}


@dataclass(frozen=True, slots=True)
class _PolicyAdapter:
    """`DemoTimePolicy` → `TimePolicyLike` 어댑터."""

    policy_type: str
    payload: Mapping[str, Any]
    is_active: bool


@dataclass(frozen=True, slots=True)
class _FixedScheduleAdapter:
    """`DemoFixedSchedule`(start_time:str) → `FixedScheduleLike`(start_time:time) 어댑터."""

    title: str
    days_of_week: Sequence[str]
    start_time: time
    end_time: time


def _parse_hhmm(raw: str) -> time:
    hh, mm = raw.split(":")
    return time(int(hh), int(mm))


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI dependencies — `Annotated[T, Depends(...)]` (AGENTS §5)
#
# 각 provider 는 mock 데이터를 반환하지만, 실 DB 연결 시 Repository 호출로 교체
# 가능하다 (signature 보존). 테스트는 `app.dependency_overrides` 로 무엇이든
# 주입할 수 있다.
# ─────────────────────────────────────────────────────────────────────────────


def get_orchestrator() -> GoalStructuringOrchestrator:
    """오케스트레이터 인스턴스 — 요청당 새로 생성 (상태머신은 인스턴스별)."""
    return GoalStructuringOrchestrator()


def get_time_policies() -> tuple[TimePolicyLike, ...]:
    """현재 사용자의 활성 시간 정책. (mock: DEMO_POLICIES)."""
    return tuple(
        _PolicyAdapter(
            policy_type=p.policy_type,
            payload=_normalize_payload(p.payload),
            is_active=p.is_active,
        )
        for p in DEMO_POLICIES
    )


def get_fixed_schedules() -> tuple[FixedScheduleLike, ...]:
    """현재 사용자의 고정 일정. (mock: DEMO_FIXED_SCHEDULES)."""
    return tuple(
        _FixedScheduleAdapter(
            title=s.title,
            days_of_week=s.days_of_week,
            start_time=_parse_hhmm(s.start_time),
            end_time=_parse_hhmm(s.end_time),
        )
        for s in DEMO_FIXED_SCHEDULES
    )


def get_habits() -> tuple[HabitLike, ...]:
    """현재 사용자의 활성 습관. (mock: DEMO_HABITS)."""
    return tuple(DEMO_HABITS)


# Annotated alias — 라우터 시그니처에서 재사용
OrchestratorDep = Annotated[GoalStructuringOrchestrator, Depends(get_orchestrator)]
TimePoliciesDep = Annotated[tuple[TimePolicyLike, ...], Depends(get_time_policies)]
FixedSchedulesDep = Annotated[tuple[FixedScheduleLike, ...], Depends(get_fixed_schedules)]
HabitsDep = Annotated[tuple[HabitLike, ...], Depends(get_habits)]


# ─────────────────────────────────────────────────────────────────────────────
# 직렬화 — DraftPlan → GeneratePlanResponse
# ─────────────────────────────────────────────────────────────────────────────


def _draft_plan_to_response(plan: DraftPlan, state: str) -> GeneratePlanResponse:
    return GeneratePlanResponse(
        # 초안은 영속화 전이라 ephemeral ID — DB 연결 후엔 plans 테이블 PK.
        plan_id=f"draft_{uuid.uuid4().hex[:12]}",
        target_date=plan.target_date,
        orchestrator_state=state,
        generated_at=plan.generated_at,
        blocks=[
            DraftBlockSchema(
                origin=b.origin,
                origin_id=str(b.origin_id) if b.origin_id is not None else None,
                title=b.title,
                category=b.category,
                start_at=b.interval.start,
                end_at=b.interval.end,
                duration_minutes=b.interval.duration_minutes,
                block_status=b.block_status,
                source=b.source,
            )
            for b in plan.blocks
        ],
        free_blocks=[
            TimeWindowSchema(
                start_at=iv.start,
                end_at=iv.end,
                duration_minutes=iv.duration_minutes,
            )
            for iv in plan.free_blocks
        ],
        busy_blocks=[
            BusyBlockSchema(
                source=b.source,
                label=b.label,
                start_at=b.interval.start,
                end_at=b.interval.end,
            )
            for b in plan.busy_blocks
        ],
        warnings=list(plan.warnings),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/generate")
async def generate_plan(
    body: GeneratePlanRequest,
    orchestrator: OrchestratorDep,
    time_policies: TimePoliciesDep,
    fixed_schedules: FixedSchedulesDep,
    habits: HabitsDep,
) -> GeneratePlanResponse:
    """주간/horizon 계획 생성 — Goal Structuring orchestrator 실행 (#18).

    응답은 **항상 비활성 초안** (`isActive=false`). 활성화는 별도 승인 경로
    (`POST /plans/{plan_id}/approve`) — AGENTS §1: 자동 적용 금지.
    """
    target_date = body.target_date if body.target_date is not None else now_kst().date()

    payload = GoalStructuringInput(
        target_date=target_date,
        time_policies=time_policies,
        fixed_schedules=fixed_schedules,
        habits=habits,
    )

    try:
        plan = orchestrator.build_draft_plan(payload)
    except ValueError as exc:
        # 필수 입력 누락 (예: 활성 수면 정책 없음 — DevBaseline §1.4).
        missing = orchestrator.validate(payload)
        raise ApiError(
            ErrorCode.PLANNING_VALIDATION_ERROR,
            str(exc),
            http_status=HTTPStatus.BAD_REQUEST,
            field=missing[0] if missing else None,
        ) from exc
    except PolicyViolationError as exc:
        # 방어적: 오케스트레이터가 정책 위반 블록을 만들면 안 된다 (AGENTS §2).
        # 실제 발생하면 코드 버그 — 500 으로 surface.
        raise ApiError(
            ErrorCode.PLANNING_POLICY_VIOLATION,
            str(exc),
            http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
        ) from exc

    return _draft_plan_to_response(plan, orchestrator.state.value)


@router.get("/{plan_id}", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_plan(plan_id: str) -> None:
    """[stub] 초안 계획 미리보기 (workloadLevel, conflicts 등) — 후속 PR."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §8 — to be implemented in a follow-up.",
    )
