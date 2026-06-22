"""Planning — Weekly plan (S06, S14, S15, S16).

핵심 흐름 (Orchestrator 1 — Goal Structuring, ADR-0005 §2.5.1):
  VALIDATING → PLANNING → REVIEWING → HITL → SAVING

규칙:
- 입력: Deep Interview(#6) 의 경계 계약 `InterviewOutcome` 하나(인라인 또는 세션 로드).
- LLM(②③④)은 `first_plan.py` 노드 내부 `aiClient.run(...)` 만 (AGENTS §2). 스케줄링은 룰만.
- horizon = focus goals 의 가장 먼 deadline (outcome 파생).
- 출력: goal_nodes + action_items + scheduled_blocks 미리보기 (항상 Draft).
- 모든 변경은 사용자 [승인] 후 적용 (Draft Layer, AGENTS §1.4) — 본 endpoint 는 생성/미리보기만.

DB: action_items, scheduled_blocks, dependency_links, llm_runs (영속화는 approve 후속 PR).

예정 endpoint:
- POST  /plans/generate                 — 첫 계획 또는 재생성 (S06) ✅ 본 PR
- GET   /plans/{plan_id}                — 미리보기 (workload, conflicts 포함)
- POST  /plans/{plan_id}/approve        — 사용자 승인 → 활성화
- PATCH /plans/{plan_id}/blocks/{id}    — 직접 편집 (S15, 15분 snap)
- POST  /plans/{plan_id}/ai-edit        — 자연어 수정 (S16, P1)
- GET   /plans/weekly?week=...          — 주간 그리드 데이터 (S14)

구현 위치: orchestrator/first_plan.py (LangGraph) + orchestrator/goal_structuring.py (룰 스케줄러)
"""

from __future__ import annotations

from datetime import date
from http import HTTPStatus
from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator import first_plan, first_plan_adapter, interview_adapter
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.orchestrator.goal_structuring import PolicyViolationError
from reaction_backend.repositories.interview_repo import InterviewRepo, get_interview_repo
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.interview import InterviewEndReason, InterviewOutcome
from reaction_backend.schemas.planning import (
    FirstPlanApproveRequest,
    FirstPlanApproveResponse,
    FirstPlanGenerateRequest,
    FirstPlanResponse,
)

router = APIRouter(prefix="/plans", tags=["planning"])

# ADR-0005 §7.6 — Planning 동시성 lock 의 agent 식별자 (Interview/Recovery 와 공용 메커니즘).
_LOCK_AGENT = "planning"

RepoDep = Annotated[InterviewRepo, Depends(get_interview_repo)]
UserRepoDep = Annotated[UserRepo, Depends(get_user_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


def _config(session: AsyncSession) -> RunnableConfig:
    """노드가 예산 가드·llm_runs 기록에 쓰는 세션 채널 (ADR-0005 §7.1)."""
    return {"configurable": {"session": session}}


def _resolve_target_date(raw: str | None) -> str:
    """target_date 정규화 — 미지정 시 오늘(KST). 형식 오류는 422."""
    if raw is None:
        return now_kst().date().isoformat()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "targetDate 는 YYYY-MM-DD 형식이어야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="targetDate",
        ) from exc


async def _resolve_outcome(
    body: FirstPlanGenerateRequest, user_id: UUID, repo: InterviewRepo
) -> InterviewOutcome:
    """요청에서 First Plan 시드 `InterviewOutcome` 을 확정한다.

    온보딩 인라인 전달(`outcome`)을 우선하고, 없으면 `interviewSessionId` 로 종료된 세션의
    slot_answers 를 결정적으로 투영한다(LLM 0회, `interview_adapter.build_outcome`).
    """
    if body.outcome is not None:
        return body.outcome
    if body.interview_session_id:
        try:
            session_uuid = UUID(body.interview_session_id)
        except ValueError as exc:
            raise _interview_not_found() from exc
        row = await repo.get_active(user_id, session_uuid)
        if row is None:
            raise _interview_not_found()
        slot_rows = await repo.list_slot_answers(row.id)
        slot_answers = {r.slot_key: r.value for r in slot_rows if r.value is not None}
        return interview_adapter.build_outcome(
            session_id=str(row.id),
            slot_answers=slot_answers,
            ambiguity_final=(
                float(row.ambiguity_final) if row.ambiguity_final is not None else 0.0
            ),
            end_reason=cast(InterviewEndReason, row.end_reason or "completed"),
            analysis_source="rule",  # slot 결정적 투영 — 정규화 LLM 미개입
        )
    raise ApiError(
        ErrorCode.COMMON_VALIDATION_ERROR,
        "outcome 또는 interviewSessionId 중 하나가 필요해요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


def _interview_not_found() -> ApiError:
    return ApiError(
        ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
        "해당 인터뷰 세션을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _tier_limit_exceeded() -> ApiError:
    return ApiError(
        ErrorCode.GOAL_TIER_LIMIT_EXCEEDED,
        "집중 목표는 최대 3개, 유지 목표는 최대 5개까지예요. 기존 목표를 보관(park)하고 다시 시도해 주세요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


@router.post("/generate")
async def generate_plan(
    body: FirstPlanGenerateRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> FirstPlanResponse:
    """첫 주간/horizon 계획 생성 — First Plan orchestrator(LangGraph) 실행.

    흐름: VALIDATING(tier 게이트) → decompose(LLM) → schedule(룰) → review(LLM) → Draft.
    Focus 한도(≤3) 초과 시 LLM 분해 전에 422 `GOAL_TIER_LIMIT_EXCEEDED`(ADR-0005 §2.5.1).
    응답은 항상 `is_draft=True`(AGENTS §1.4) — 영속화는 `/plans/{id}/approve` 후속.

    동시성 lock(ADR-0005 §7.6): 다중 디바이스 동시 생성으로 인한 state race 방지.
    """
    outcome = await _resolve_outcome(body, user.id, repo)
    target_date = _resolve_target_date(body.target_date)

    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        config = _config(session)
        state = first_plan.initial_state(user_id=user.id, outcome=outcome, target_date=target_date)
        # Validation Agent — LLM 분해 전에 Focus≤3 게이트 (LLM 0회, 룰만).
        gate = await first_plan.validate_inputs(state, config)
        if gate["tier_violation"] is not None:
            raise _tier_limit_exceeded()

        graph = first_plan.build_first_plan_graph()
        final = await graph.ainvoke(state, config=config)

    gp = final["goal_plan"]
    return FirstPlanResponse(
        is_draft=True,  # AGENTS §1.4 — 사용자 승인 전까지 항상 Draft.
        ai_source="rule" if final["used_fallback"] else "llm",
        plan_id=f"plan_{uuid4().hex}",  # ephemeral draft id (영속화는 approve 후속 PR)
        target_date=target_date,
        horizon=final["horizon"],
        goal_nodes=gp.goal_nodes if gp is not None else [],
        action_items=gp.action_items if gp is not None else [],
        blocks=final["scheduled_blocks"],
        warnings=final["schedule_warnings"],
        policy_violations=gp.policy_violations if gp is not None else [],
        generated_at=now_kst(),
    )


@router.post("/{plan_id}/approve")
async def approve_plan(
    plan_id: str,
    body: FirstPlanApproveRequest,
    user: CurrentUser,
    user_repo: UserRepoDep,
    session: SessionDep,
) -> FirstPlanApproveResponse:
    """First Plan Draft 승인 → SAVING (단일 가드 트랜잭션 영속화, ADR-0005 §2.5.1).

    HITL [수락] 이후에만 호출되는 영속화 경로. `policy_guarded_transaction`(PR #30 재사용)이
    절대 시간 정책 위반 시 즉시 롤백 → 422 `PLAN_POLICY_VIOLATION`. 그 외 영속화 실패는 롤백 후
    500 `PLAN_SAVE_FAILED`. 응답은 명시 승인이므로 `is_draft=false` (ADR-0005 §7.2).

    부수 효과: First Plan 단계 완료 → onboarding `ONBOARDING_FIRST_PLAN → ONBOARDING_NOTIFICATIONS`
    전이 (멱등). Issue #17 "각 도메인 라우터가 자기 단계 완료 시 전이" 규약 + 본 전이는 #17 이
    "#9(First Plan) 다음에" 로 명시적으로 First Plan 에 위임 → api-contract §3.

    동시성 lock(ADR-0005 §7.6): 다중 디바이스 동시 승인 race 방지.
    ⚠️ 본 슬라이스는 action_items + scheduled_blocks 만 영속화 — goal/goal_node 트리 +
    dependency_links 영속화는 후속 SAVING 작업. `plan_id` 는 echo(ephemeral).
    """
    target_date = _resolve_target_date(body.target_date)
    policies = first_plan_adapter.time_policies_from_outcome(body.outcome)

    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        try:
            n_actions, n_blocks = await first_plan_adapter.db_apply_first_plan(
                session,
                user_id=user.id,
                target_date=date.fromisoformat(target_date),
                action_items=body.action_items,
                blocks=body.blocks,
                time_policies=policies,
            )
        except PolicyViolationError as exc:
            raise ApiError(
                ErrorCode.PLAN_POLICY_VIOLATION,
                "계획에 수면·노터치 같은 보호 시간과 겹치는 블록이 있어요. 시간을 옮겨 다시 시도해 주세요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            ) from exc
        except Exception as exc:  # 영속화 부분 실패 — 이미 롤백됨(가드 트랜잭션)
            raise ApiError(
                ErrorCode.PLAN_SAVE_FAILED,
                "계획 저장에 잠시 문제가 있어요. 잠시 후 다시 시도해 주세요.",
                http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc

        # First Plan 단계 완료 → 다음 온보딩 단계(알림 설정)로 전이. 멱등(이미 진행/ACTIVE 면 no-op).
        await user_repo.advance_onboarding(
            user,
            expected_from="ONBOARDING_FIRST_PLAN",
            to="ONBOARDING_NOTIFICATIONS",
        )
        await session.commit()

    return FirstPlanApproveResponse(
        plan_id=plan_id,
        activated_action_items=n_actions,
        activated_blocks=n_blocks,
        activated_at=now_kst(),
    )
