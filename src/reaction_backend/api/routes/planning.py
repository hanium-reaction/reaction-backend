"""Planning — Weekly plan (S06, S14, S15, S16).

핵심 흐름 (Orchestrator 1 — Goal Structuring, ADR-0005 §2.5.1):
  VALIDATING → PLANNING → REVIEWING → HITL → SAVING

규칙:
- 입력: Deep Interview(#6) 의 경계 계약 `InterviewOutcome` 하나(인라인 또는 세션 로드).
- LLM(②③④)은 `first_plan.py` 노드 내부 `aiClient.run(...)` 만 (AGENTS §2). 스케줄링은 룰만.
- horizon = focus goals 의 가장 먼 deadline (outcome 파생).
- 출력: goal_nodes + action_items + scheduled_blocks 미리보기 (항상 Draft).
- 모든 변경은 사용자 [승인] 후 적용 (Draft Layer, AGENTS §1.4).

흐름 (#62):
- generate 가 Draft 를 `plan_drafts` 에 저장하고 실제 `planId` 반환.
- `GET /plans/{planId}` 가 Draft 미리보기 재구성(LLM 0회).
- `POST /plans/{planId}/approve` 가 Draft 를 로드해 goal 트리(goals/goal_nodes/action_items/
  scheduled_blocks)로 단일 트랜잭션 영속화(+3회 재시도) → 활성화.

DB: plan_drafts, goals, goal_nodes, action_items, scheduled_blocks, llm_runs.

예정 endpoint:
- POST  /plans/generate                 — 첫 계획 생성 (S06) ✅
- GET   /plans/{plan_id}                — Draft 미리보기 ✅
- POST  /plans/{plan_id}/approve        — 사용자 승인 → 활성화 ✅
- PATCH /plans/{plan_id}/blocks/{id}    — 직접 편집 (S15, 15분 snap)
- POST  /plans/{plan_id}/ai-edit        — 자연어 수정 (S16, P1)
- GET   /plans/weekly?week=...          — 주간 그리드 데이터 (S14)

구현 위치: orchestrator/first_plan.py (LangGraph) + orchestrator/goal_structuring.py (룰 스케줄러)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator import first_plan, first_plan_adapter, interview_adapter
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.orchestrator.goal_structuring import PolicyViolationError
from reaction_backend.orchestrator.plan_edit import find_policy_violation, snap_to_15min
from reaction_backend.repositories.action_item_repo import ActionItemRepo, get_action_item_repo
from reaction_backend.repositories.interview_repo import InterviewRepo, get_interview_repo
from reaction_backend.repositories.plan_draft_repo import PlanDraftRepo, get_plan_draft_repo
from reaction_backend.repositories.scheduled_block_repo import (
    ScheduledBlockRepo,
    get_scheduled_block_repo,
)
from reaction_backend.repositories.time_policy_repo import TimePolicyRepo, get_time_policy_repo
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.schemas.common import KST, now_kst, to_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.interview import InterviewEndReason, InterviewOutcome
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    BlockEditRequest,
    BlockEditResponse,
    FirstPlanApproveResponse,
    FirstPlanGenerateRequest,
    FirstPlanResponse,
    GoalNodeDraft,
    PolicyViolation,
    ScheduledBlockPreview,
    WeeklyBlock,
    WeeklyPlanDay,
    WeeklyPlanResponse,
)

router = APIRouter(prefix="/plans", tags=["planning"])

# ADR-0005 §7.6 — Planning 동시성 lock 의 agent 식별자 (Interview/Recovery 와 공용 메커니즘).
_LOCK_AGENT = "planning"

# ADR-0005 §7.8 — Planning Draft 72h 미응답 만료.
_DRAFT_TTL = timedelta(hours=72)

RepoDep = Annotated[InterviewRepo, Depends(get_interview_repo)]
UserRepoDep = Annotated[UserRepo, Depends(get_user_repo)]
DraftRepoDep = Annotated[PlanDraftRepo, Depends(get_plan_draft_repo)]
BlockRepoDep = Annotated[ScheduledBlockRepo, Depends(get_scheduled_block_repo)]
ActionRepoDep = Annotated[ActionItemRepo, Depends(get_action_item_repo)]
PolicyRepoDep = Annotated[TimePolicyRepo, Depends(get_time_policy_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]

# S14/S15 (#21-B) — 주간 그리드/블록 편집. planId 는 주 논리 식별자(Plan 테이블 없음), 편집 권한은 blockId.
_BLOCK_PREFIX = "block_"
_ACTION_PREFIX = "action_"
_WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _config(session: AsyncSession, tone_mode: str | None = None) -> RunnableConfig:
    """노드가 예산 가드·llm_runs 기록에 쓰는 세션 채널 (ADR-0005 §7.1) + 톤(#23-D)."""
    return {"configurable": {"session": session, "tone_mode": tone_mode}}


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
            # 인터뷰 정규화가 LLM 이었는지 룰 fallback 이었는지 (세션에 영속된 플래그).
            analysis_source="rule" if row.used_fallback else "llm",
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


# ─────────────────────────────────────────────────────────────────────────────
# Draft payload ↔ schema 변환 (#62) — 저장 스냅샷은 snake key, 재구성은 model_validate.
# ─────────────────────────────────────────────────────────────────────────────


def _build_payload(
    *,
    outcome: InterviewOutcome,
    goal_nodes: list[GoalNodeDraft],
    action_items: list[ActionItemDraft],
    blocks: list[ScheduledBlockPreview],
    warnings: list[str],
    policy_violations: list[PolicyViolation],
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "outcome": outcome.model_dump(mode="json"),
        "goal_nodes": [n.model_dump(mode="json") for n in goal_nodes],
        "action_items": [a.model_dump(mode="json") for a in action_items],
        "blocks": [b.model_dump(mode="json") for b in blocks],
        "warnings": list(warnings),
        "policy_violations": [v.model_dump(mode="json") for v in policy_violations],
        "generated_at": generated_at.isoformat(),
    }


def _draft_to_response(draft: PlanDraft) -> FirstPlanResponse:
    """저장된 Draft → 미리보기 응답 재구성 (LLM 0회)."""
    p = draft.payload
    return FirstPlanResponse(
        is_draft=True,
        ai_source=cast(Literal["llm", "rule"], draft.ai_source),
        plan_id=str(draft.id),
        target_date=draft.target_date.isoformat(),
        horizon=draft.horizon,
        goal_nodes=[GoalNodeDraft.model_validate(n) for n in p["goal_nodes"]],
        action_items=[ActionItemDraft.model_validate(a) for a in p["action_items"]],
        blocks=[ScheduledBlockPreview.model_validate(b) for b in p["blocks"]],
        warnings=list(p.get("warnings", [])),
        policy_violations=[
            PolicyViolation.model_validate(v) for v in p.get("policy_violations", [])
        ],
        generated_at=datetime.fromisoformat(p["generated_at"]),
    )


def _draft_not_found() -> ApiError:
    return ApiError(
        ErrorCode.PLAN_DRAFT_NOT_FOUND,
        "해당 계획 초안을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


async def _load_draft(repo: PlanDraftRepo, user_id: UUID, plan_id: str) -> PlanDraft:
    try:
        draft_id = UUID(plan_id)
    except ValueError as exc:
        raise _draft_not_found() from exc
    draft = await repo.get_by_id(user_id, draft_id)
    if draft is None:
        raise _draft_not_found()
    return draft


# ─────────────────────────────────────────────────────────────────────────────
# endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/generate")
async def generate_plan(
    body: FirstPlanGenerateRequest,
    user: CurrentUser,
    repo: RepoDep,
    draft_repo: DraftRepoDep,
    session: SessionDep,
) -> FirstPlanResponse:
    """첫 주간/horizon 계획 생성 — First Plan orchestrator(LangGraph) 실행 → Draft 저장.

    흐름: VALIDATING(tier 게이트) → decompose(LLM) → schedule(룰) → review(LLM) → Draft 저장.
    Focus≤3 / Maintain≤5 초과 시 LLM 분해 전에 422 `GOAL_TIER_LIMIT_EXCEEDED`.
    Draft 를 `plan_drafts`(72h 만료)에 저장하고 실제 `planId` 를 반환. 항상 `is_draft=true`.

    동시성 lock(ADR-0005 §7.6): 다중 디바이스 동시 생성으로 인한 state race 방지.
    """
    outcome = await _resolve_outcome(body, user.id, repo)
    target_date = _resolve_target_date(body.target_date)

    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        config = _config(session, user.tone_mode)
        state = first_plan.initial_state(user_id=user.id, outcome=outcome, target_date=target_date)
        # Validation Agent — LLM 분해 전에 Focus≤3 / Maintain≤5 게이트 (LLM 0회, 룰만).
        gate = await first_plan.validate_inputs(state, config)
        if gate["tier_violation"] is not None:
            raise _tier_limit_exceeded()

        graph = first_plan.build_first_plan_graph()
        final = await graph.ainvoke(state, config=config)

        gp = final["goal_plan"]
        ai_source: Literal["llm", "rule"] = "rule" if final["used_fallback"] else "llm"
        payload = _build_payload(
            outcome=outcome,
            goal_nodes=gp.goal_nodes if gp is not None else [],
            action_items=gp.action_items if gp is not None else [],
            blocks=final["scheduled_blocks"],
            warnings=final["schedule_warnings"],
            policy_violations=gp.policy_violations if gp is not None else [],
            generated_at=now_kst(),
        )
        draft = await draft_repo.create(
            user.id,
            target_date=date.fromisoformat(target_date),
            horizon=final["horizon"],
            ai_source=ai_source,
            payload=payload,
            expires_at=now_kst() + _DRAFT_TTL,
        )
        await session.commit()

    return _draft_to_response(draft)


# ─────────────────────────────────────────────────────────────────────────────
# S14 Weekly Plan View + S15 직접 편집 (#21-B). `/weekly` 는 `/{plan_id}` 보다 먼저 선언.
# ─────────────────────────────────────────────────────────────────────────────


def _monday_of(day: date) -> date:
    """그 날이 속한 주의 월요일 (월=0)."""
    return day - timedelta(days=day.weekday())


def _week_bounds(monday: date) -> tuple[datetime, datetime]:
    start_dt = datetime.combine(monday, datetime.min.time(), tzinfo=KST)
    return start_dt, start_dt + timedelta(days=7)


def _parse_week_start(raw: str | None) -> date:
    if raw is None:
        return _monday_of(now_kst().date())
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as e:
        raise ApiError(
            ErrorCode.PLAN_INVALID_TIME,
            "weekStart 는 YYYY-MM-DD 형식이어야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="weekStart",
        ) from e
    return _monday_of(parsed)


def _block_not_found() -> ApiError:
    return ApiError(
        ErrorCode.PLAN_BLOCK_NOT_FOUND,
        "해당 일정 블록을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _parse_block_id(raw: str) -> UUID:
    if not raw.startswith(_BLOCK_PREFIX):
        raise _block_not_found()
    try:
        return UUID(raw[len(_BLOCK_PREFIX) :])
    except ValueError as e:
        raise _block_not_found() from e


def _parse_block_dt(raw: str, field: str) -> datetime:
    """ISO 8601 → KST aware. naive 면 KST 로 간주. 형식 오류 422."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ApiError(
            ErrorCode.PLAN_INVALID_TIME,
            "시각은 ISO 8601 형식이어야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field=field,
        ) from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _block_view(block: ScheduledBlock, title: str, category: str) -> WeeklyBlock:
    return WeeklyBlock(
        block_id=f"{_BLOCK_PREFIX}{block.id}",
        action_id=f"{_ACTION_PREFIX}{block.action_item_id}",
        title=title,
        category=category,
        start_at=block.start_at,
        end_at=block.end_at,
        block_status=block.block_status,
        source=block.source,
    )


@router.get("/weekly")
async def get_weekly_plan(
    user: CurrentUser,
    repo: BlockRepoDep,
    week_start: Annotated[str | None, Query(alias="weekStart")] = None,
) -> WeeklyPlanResponse:
    """주간 블록 그리드 (S14). weekStart 생략 시 이번 주 월요일 기준."""
    monday = _parse_week_start(week_start)
    start_dt, end_dt = _week_bounds(monday)
    rows = await repo.list_week(user.id, start_dt, end_dt)

    days = [
        WeeklyPlanDay(date=monday + timedelta(days=offset), weekday=_WEEKDAY_NAMES[offset])
        for offset in range(7)
    ]
    by_date = {d.date: d for d in days}
    for block, title, category in rows:
        bucket = by_date.get(to_kst(block.start_at).date())
        if bucket is not None:
            bucket.blocks.append(_block_view(block, title, category))

    return WeeklyPlanResponse(
        plan_id=f"plan_{monday.isoformat()}",
        week_start=monday,
        week_end=monday + timedelta(days=6),
        days=days,
    )


@router.patch("/{plan_id}/blocks/{block_id}")
async def edit_block(
    plan_id: str,  # noqa: ARG001 — 논리 식별자(주). 편집 권한은 blockId.
    block_id: str,
    body: BlockEditRequest,
    user: CurrentUser,
    repo: BlockRepoDep,
    action_repo: ActionRepoDep,
    policy_repo: PolicyRepoDep,
    session: SessionDep,
) -> BlockEditResponse:
    """블록 15분 snap 이동 (S15). 충돌 422 `PLAN_BLOCK_CONFLICT` / 정책 422 `POLICY_VIOLATION`."""
    block = await repo.get_block(user.id, _parse_block_id(block_id))
    if block is None:
        raise _block_not_found()

    new_start = snap_to_15min(_parse_block_dt(body.start_at, "startAt"))
    if body.end_at is not None:
        new_end = snap_to_15min(_parse_block_dt(body.end_at, "endAt"))
    else:
        new_end = new_start + (block.end_at - block.start_at)  # 길이 보존

    if new_end <= new_start:
        raise ApiError(
            ErrorCode.PLAN_INVALID_TIME,
            "종료 시각이 시작 시각보다 늦어야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="endAt",
        )

    conflicts = await repo.list_overlapping(user.id, new_start, new_end, exclude_block_id=block.id)
    if conflicts:
        raise ApiError(
            ErrorCode.PLAN_BLOCK_CONFLICT,
            "그 시간에 이미 다른 일정이 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="startAt",
        )

    action = await action_repo.get_by_id(user.id, block.action_item_id)
    category = action.category if action is not None else "other"
    policies = await policy_repo.list_active(user.id)
    violated = find_policy_violation(to_kst(new_start), to_kst(new_end), category, policies)
    if violated is not None:
        raise ApiError(
            ErrorCode.POLICY_VIOLATION,
            f"이 시간대는 '{violated}' 정책과 겹쳐요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="startAt",
        )

    block.start_at = new_start
    block.end_at = new_end
    block.source = "user_edit"
    await session.commit()

    return BlockEditResponse(
        block_id=f"{_BLOCK_PREFIX}{block.id}",
        action_id=f"{_ACTION_PREFIX}{block.action_item_id}",
        title=action.title if action is not None else "",
        category=category,
        start_at=block.start_at,
        end_at=block.end_at,
        block_status=block.block_status,
        source=block.source,
    )


@router.get("/{plan_id}")
async def get_plan(plan_id: str, user: CurrentUser, draft_repo: DraftRepoDep) -> FirstPlanResponse:
    """저장된 First Plan Draft 미리보기 — LLM 재호출 없이 스냅샷 재구성."""
    draft = await _load_draft(draft_repo, user.id, plan_id)
    return _draft_to_response(draft)


@router.post("/{plan_id}/approve")
async def approve_plan(
    plan_id: str,
    user: CurrentUser,
    user_repo: UserRepoDep,
    draft_repo: DraftRepoDep,
    session: SessionDep,
) -> FirstPlanApproveResponse:
    """First Plan Draft 승인 → SAVING (goal 트리 단일 가드 트랜잭션 영속화, ADR-0005 §2.5.1).

    `plan_id` 로 저장된 Draft 를 로드해 goals/goal_nodes/action_items/scheduled_blocks 를
    단일 트랜잭션으로 영속화(+최대 3회 재시도). `policy_guarded_transaction`(PR #30 재사용)이
    절대 시간 정책 위반 시 롤백 → 422 `PLAN_POLICY_VIOLATION`, 그 외 실패는 롤백 후 500
    `PLAN_SAVE_FAILED`. 만료된 Draft 는 410 `PLAN_DRAFT_EXPIRED`. 이미 승인된 Draft 는 멱등.

    부수 효과: onboarding `ONBOARDING_FIRST_PLAN → ONBOARDING_NOTIFICATIONS` 전이(멱등) —
    Issue #17 이 "#9(First Plan) 다음에" 로 First Plan 에 위임 (api-contract §3).
    응답은 명시 승인이므로 `is_draft=false` (ADR-0005 §7.2).
    """
    draft = await _load_draft(draft_repo, user.id, plan_id)
    if draft.status == "expired" or draft.expires_at < now_kst():
        raise ApiError(
            ErrorCode.PLAN_DRAFT_EXPIRED,
            "오래 두신 계획 초안이 만료됐어요. 다시 만들어 볼까요?",
            http_status=HTTPStatus.GONE,
        )

    payload = draft.payload
    if draft.status == "approved":  # 멱등 — 이미 영속화됨, 재저장하지 않음
        return _approved_response(plan_id, payload)

    outcome = InterviewOutcome.model_validate(payload["outcome"])
    goal_nodes = [GoalNodeDraft.model_validate(n) for n in payload["goal_nodes"]]
    action_items = [ActionItemDraft.model_validate(a) for a in payload["action_items"]]
    blocks = [ScheduledBlockPreview.model_validate(b) for b in payload["blocks"]]
    policies = first_plan_adapter.time_policies_from_outcome(outcome)

    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        try:
            result = await first_plan_adapter.db_apply_first_plan(
                session,
                user_id=user.id,
                target_date=draft.target_date,
                outcome=outcome,
                goal_nodes=goal_nodes,
                action_items=action_items,
                blocks=blocks,
                time_policies=policies,
            )
        except PolicyViolationError as exc:
            raise ApiError(
                ErrorCode.PLAN_POLICY_VIOLATION,
                "계획에 수면·노터치 같은 보호 시간과 겹치는 블록이 있어요. 시간을 옮겨 다시 시도해 주세요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            ) from exc
        except Exception as exc:  # 영속화 실패(3회 재시도 후) — 이미 롤백됨(가드 트랜잭션)
            raise ApiError(
                ErrorCode.PLAN_SAVE_FAILED,
                "계획 저장에 잠시 문제가 있어요. 잠시 후 다시 시도해 주세요.",
                http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc

        await draft_repo.mark_approved(draft, approved_at=now_kst())
        # First Plan 단계 완료 → 다음 온보딩 단계(알림 설정)로 전이. 멱등(이미 진행/ACTIVE 면 no-op).
        await user_repo.advance_onboarding(
            user,
            expected_from="ONBOARDING_FIRST_PLAN",
            to="ONBOARDING_NOTIFICATIONS",
        )
        await session.commit()

    return FirstPlanApproveResponse(
        plan_id=plan_id,
        activated_goals=result.goals,
        activated_goal_nodes=result.goal_nodes,
        activated_action_items=result.action_items,
        activated_blocks=result.scheduled_blocks,
        activated_at=now_kst(),
    )


def _approved_response(plan_id: str, payload: dict[str, Any]) -> FirstPlanApproveResponse:
    """이미 승인된 Draft 재승인 — 저장 스냅샷 길이로 멱등 응답(재영속화 없음)."""
    core_goals = payload.get("outcome", {}).get("core_goals", [])
    return FirstPlanApproveResponse(
        plan_id=plan_id,
        activated_goals=len(core_goals),
        activated_goal_nodes=len(payload.get("goal_nodes", [])),
        activated_action_items=len(payload.get("action_items", [])),
        activated_blocks=len(payload.get("blocks", [])),
        activated_at=now_kst(),
    )
