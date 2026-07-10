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
from reaction_backend.db.models.action_item import ACTION_CATEGORY_VALUES
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.models.plan_draft import PlanDraft
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator import (
    first_plan,
    first_plan_adapter,
    interview_adapter,
    replan,
)
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.orchestrator.goal_structuring import (
    PolicyViolationError,
    fixed_schedules_to_busy,
    time_policies_to_busy,
)
from reaction_backend.orchestrator.plan_edit import find_policy_violation, snap_to_15min
from reaction_backend.repositories.action_item_repo import ActionItemRepo, get_action_item_repo
from reaction_backend.repositories.fixed_schedule_repo import (
    FixedScheduleRepo,
    get_fixed_schedule_repo,
)
from reaction_backend.repositories.interview_repo import InterviewRepo, get_interview_repo
from reaction_backend.repositories.plan_draft_repo import PlanDraftRepo, get_plan_draft_repo
from reaction_backend.repositories.review_repo import ReviewRepo, get_review_repo
from reaction_backend.repositories.scheduled_block_repo import (
    ScheduledBlockRepo,
    get_scheduled_block_repo,
)
from reaction_backend.repositories.time_policy_repo import TimePolicyRepo, get_time_policy_repo
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.scheduler.weekly_review_precompute import run_weekly_review_for_user
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
    ReplanApproveResponse,
    ReplanBlockPreview,
    ReplanResponse,
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


async def _project_session_outcome(row: InterviewSession, repo: InterviewRepo) -> InterviewOutcome:
    """종료된 인터뷰 세션의 slot_answers 를 outcome 으로 결정적 투영 (LLM 0회)."""
    slot_rows = await repo.list_slot_answers(row.id)
    slot_answers = {r.slot_key: r.value for r in slot_rows if r.value is not None}
    return interview_adapter.build_outcome(
        session_id=str(row.id),
        slot_answers=slot_answers,
        ambiguity_final=(float(row.ambiguity_final) if row.ambiguity_final is not None else 0.0),
        end_reason=cast(InterviewEndReason, row.end_reason or "completed"),
        # 인터뷰 정규화가 LLM 이었는지 룰 fallback 이었는지 (세션에 영속된 플래그).
        analysis_source="rule" if row.used_fallback else "llm",
    )


async def _resolve_outcome(
    body: FirstPlanGenerateRequest, user_id: UUID, repo: InterviewRepo
) -> InterviewOutcome:
    """요청에서 First Plan 시드 `InterviewOutcome` 을 확정한다.

    우선순위: ① 인라인 `outcome` → ② `interviewSessionId` 로 종료 세션 투영 →
    ③ **빈 본문이면 최근 '정상 종료' 인터뷰 세션으로 자동 복구** — FE 가 새로고침 등으로
    sessionId(메모리 보관)를 잃어도 계획 생성이 가능하도록 (abandoned 제외).
    셋 다 불가하면 422.
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
        return await _project_session_outcome(row, repo)
    latest = await repo.get_latest_finished(user_id)
    if latest is not None:
        return await _project_session_outcome(latest, repo)
    raise ApiError(
        ErrorCode.COMMON_VALIDATION_ERROR,
        "완료된 인터뷰가 없어요. 인터뷰를 먼저 진행하거나 outcome/interviewSessionId 를 보내주세요.",
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
        state = first_plan.initial_state(
            user_id=user.id, outcome=outcome, target_date=target_date, scope=body.scope
        )
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


def _block_view(
    block: ScheduledBlock, title: str, category: str, goal_id: UUID | None
) -> WeeklyBlock:
    return WeeklyBlock(
        block_id=f"{_BLOCK_PREFIX}{block.id}",
        action_id=f"{_ACTION_PREFIX}{block.action_item_id}",
        title=title,
        category=category,
        # 블록 → 목표 연결 (action_item.goal_id 경유) — FE 가 목표 분류/색을 붙일 수 있게.
        goal_id=f"goal_{goal_id}" if goal_id is not None else None,
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
    for block, title, category, goal_id in rows:
        bucket = by_date.get(to_kst(block.start_at).date())
        if bucket is not None:
            bucket.blocks.append(_block_view(block, title, category, goal_id))

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
    """블록 15분 snap 이동 + 목표(category)/제목 수정 (S15).

    충돌 422 `PLAN_BLOCK_CONFLICT` / 정책 422 `POLICY_VIOLATION`. `category`/`title` 을 주면
    블록이 매달린 action_item 을 갱신한다(같은 액션의 모든 세션 블록 공유). 정책 검사는
    **변경된 category** 로 수행하고, 변경 반영은 성공 commit 시에만 영속된다(422 면 롤백).
    """
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
    # 목표(category)/제목 변경을 action_item 에 반영 — 미지정 필드는 유지. 정책 검사·응답이
    # 새 값을 쓰도록 커밋 전에 적용(422 면 커밋 안 돼 롤백). category 미지원값은 'other'.
    if action is not None:
        if body.category is not None:
            action.category = body.category if body.category in ACTION_CATEGORY_VALUES else "other"
        if body.title is not None and body.title.strip():
            action.title = body.title.strip()
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
        # GET /plans/weekly 와 동일하게 목표 연결을 에코 — 이동 후에도 FE 분류/색 유지.
        goal_id=(
            f"goal_{action.goal_id}" if action is not None and action.goal_id is not None else None
        ),
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

    승인 = 교체: 같은 target_date 의 이전 AI 계획 산출물 중 사용자가 손대지 않은
    카드(source=goal·status=planned, user_edit 블록 없는 것)와 그 블록을 soft 정리
    (archived/cancelled)하고, heaviest goal 의 기존 분해 트리도 보관한 뒤 새 계획을
    영속화한다 — 재생성→재승인 반복으로 같은 날짜에 카드/블록/노드가 겹겹이 누적되던
    문제 방지 (`first_plan_adapter.supersede_previous_plan`).

    동시성(더블클릭·다중 디바이스 동시 승인): advisory lock 은 **트랜잭션 스코프**
    (`pg_advisory_xact_lock`) 라 commit/rollback 마다 풀린다. 그래서 시도(attempt)마다
    lock 을 새로 잡고, Draft 로드·만료·멱등 검사 → 영속화 → Draft 승인 마킹·온보딩
    전이(`on_success` — 가드 트랜잭션 내부)까지를 **한 트랜잭션 단일 commit** 으로 묶는다.
    lock 이 풀리는 순간엔 항상 status=approved 가 이미 커밋돼 있어, 대기하던 요청은
    멱등 응답으로 빠진다. 재시도(ADR-0005 §2.5.1, 3회)는 이 라우터 루프가 담당한다
    (adapter 내부 재시도는 rollback 으로 lock 을 잃은 채 돌게 되므로 `max_retries=1`).

    부수 효과: 첫 계획 승인 = 온보딩 완료 → onboarding_state 를 `ACTIVE` 로 전이(멱등,
    어느 온보딩 단계에서든). 원설계(FIRST_PLAN → NOTIFICATIONS)는 실제 FE 흐름에서 상태가
    WELCOME 에 고정돼 새로고침 시 재-온보딩되던 문제가 있어 승인에서 ACTIVE 로 마감
    (api-contract §3).
    응답은 명시 승인이므로 `is_draft=false` (ADR-0005 §7.2).
    """
    last_exc: Exception | None = None
    for _attempt in range(first_plan_adapter.MAX_SAVE_RETRIES):
        async with user_agent_lock(session, user.id, _LOCK_AGENT):
            # 검사→영속화→승인 마킹이 lock 을 쥔 한 트랜잭션 — 이중 영속화 방지.
            draft = await _load_draft(draft_repo, user.id, plan_id)
            if draft.status == "expired" or draft.expires_at < now_kst():
                raise ApiError(
                    ErrorCode.PLAN_DRAFT_EXPIRED,
                    "오래 두신 계획 초안이 만료됐어요. 다시 만들어 볼까요?",
                    http_status=HTTPStatus.GONE,
                )

            payload = draft.payload
            # 재계획 Draft(kind=replan)를 이 First Plan 승인에 넣으면 payload["outcome"] 가 없어
            # KeyError→500 이 난다. 전용 endpoint(/plans/replan/{id}/approve)로 안내(#117).
            if payload.get("kind") == "replan":
                raise ApiError(
                    ErrorCode.PLAN_DRAFT_NOT_FOUND,
                    "이 초안은 재계획 초안이에요. 재계획 승인으로 진행해 주세요.",
                    http_status=HTTPStatus.NOT_FOUND,
                )
            if draft.status == "approved":  # 멱등 — 이미 영속화됨, 재저장하지 않음
                return _approved_response(plan_id, payload)

            outcome = InterviewOutcome.model_validate(payload["outcome"])
            goal_nodes = [GoalNodeDraft.model_validate(n) for n in payload["goal_nodes"]]
            action_items = [ActionItemDraft.model_validate(a) for a in payload["action_items"]]
            blocks = [ScheduledBlockPreview.model_validate(b) for b in payload["blocks"]]
            policies = first_plan_adapter.time_policies_from_outcome(outcome)

            async def _finalize(draft: PlanDraft = draft) -> None:
                """영속화와 같은 가드 트랜잭션(단일 commit) 안에서 실행되는 부수 기록.

                첫 계획 승인 = 온보딩 완료 신호 → onboarding_state 를 ACTIVE 로 마감(멱등).
                원설계는 FIRST_PLAN→NOTIFICATIONS(그 뒤 알림 설정에서 ACTIVE)였으나, 실제
                FE 흐름은 (a) 알림 설정이 계획 승인보다 먼저 끝나고 (b) 인터뷰~캘린더 단계
                전이가 항상 트리거되지 않아 onboarding_state 가 WELCOME 에 고정 → 새로고침
                시 재-온보딩·계획 중복 누적 문제가 있었다. 승인 시점에 어느 온보딩 단계에
                있든 ACTIVE 로 올려 이를 없앤다. 이미 ACTIVE 면 no-op.
                """
                await draft_repo.mark_approved(draft, approved_at=now_kst())
                await user_repo.advance_onboarding(
                    user,
                    expected_from=(
                        "WELCOME",
                        "ONBOARDING_INTERVIEW",
                        "ONBOARDING_CONFIRM",
                        "ONBOARDING_CALENDAR",
                        "ONBOARDING_MANUAL_SCHEDULE",
                        "ONBOARDING_POLICIES",
                        "ONBOARDING_FIRST_PLAN",
                        "ONBOARDING_NOTIFICATIONS",
                    ),
                    to="ACTIVE",
                )

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
                    max_retries=1,  # 재시도는 이 라우터 루프가 lock 재획득과 함께 수행
                    on_success=_finalize,
                )
            except PolicyViolationError as exc:
                raise ApiError(
                    ErrorCode.PLAN_POLICY_VIOLATION,
                    "계획에 수면·노터치 같은 보호 시간과 겹치는 블록이 있어요. 시간을 옮겨 다시 시도해 주세요.",
                    http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                ) from exc
            except (
                Exception
            ) as exc:  # 이 시도 실패 — 이미 롤백됨(가드 트랜잭션), lock 재획득 후 재시도
                last_exc = exc
                continue

            return FirstPlanApproveResponse(
                plan_id=plan_id,
                activated_goals=result.goals,
                activated_goal_nodes=result.goal_nodes,
                activated_action_items=result.action_items,
                activated_blocks=result.scheduled_blocks,
                activated_at=now_kst(),
            )

    # MAX_SAVE_RETRIES 회 모두 실패 (ADR-0005 §2.5.1)
    raise ApiError(
        ErrorCode.PLAN_SAVE_FAILED,
        "계획 저장에 잠시 문제가 있어요. 잠시 후 다시 시도해 주세요.",
        http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
    ) from last_exc


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


# ─────────────────────────────────────────────────────────────────────────────
# 주간 forward 재계획 (S21 후속) — 남은 작업을 이후 구간에 다시 배치.
# 주간 리포트를 먼저 작성하고, 다음 주부터 마감까지 재배치한다. 기존 goal/node/action 재사용,
# 미래 미착수 블록만 교체(중복 0). 승인은 blanket-cancel 없이 **block-id 재조정**(#117).
# ─────────────────────────────────────────────────────────────────────────────

ReviewRepoDep = Annotated[ReviewRepo, Depends(get_review_repo)]
FixedRepoDep = Annotated[FixedScheduleRepo, Depends(get_fixed_schedule_repo)]

# 재계획 튜닝 기본값 — 피크/세션/휴식 개인화(behavioral_profile 반영)는 후속.
_REPLAN_TUNING = replan.ReplanTuning(
    peak_windows=(),
    focus_chunk_min=60,
    break_min=10,
    daily_focus_cap_min=first_plan_adapter.DEFAULT_DAILY_FOCUS_CAP_MIN,
)


class _RulePolicy:
    """`TimePolicyLike` 최소 구현 — 정책 미설정 유저의 기본 수면창용."""

    def __init__(self, policy_type: str, payload: dict[str, Any]) -> None:
        self.policy_type = policy_type
        self.payload = payload
        self.is_active = True


def _active_or_default_policies(rows: list[Any]) -> list[Any]:
    """활성 time_policies. 하나도 없으면 기본 수면창(23:00~08:00)만 적용해 주간 시간대 배치."""
    if rows:
        return list(rows)
    return [_RulePolicy("sleep", {"start_time": "23:00", "end_time": "08:00"})]


def _replan_response(draft: PlanDraft) -> ReplanResponse:
    """저장된 재계획 Draft → 응답(재조회·생성 공용)."""
    payload = draft.payload
    blocks = [
        ReplanBlockPreview(
            action_id=str(b["actionId"]),
            title=str(b["title"]),
            category=str(b["category"]),
            start=datetime.fromisoformat(str(b["start"])),
            end=datetime.fromisoformat(str(b["end"])),
            replaces_block_id=b.get("replacesBlockId"),
        )
        for b in payload.get("blocks", [])
    ]
    return ReplanResponse(
        plan_id=str(draft.id),
        ai_source="rule",
        window_start=str(payload.get("window_start", "")),
        horizon=payload.get("horizon"),
        blocks=blocks,
        warnings=list(payload.get("warnings", [])),
        generated_at=now_kst(),
    )


@router.post("/replan", status_code=201)
async def generate_replan(
    user: CurrentUser,
    block_repo: BlockRepoDep,
    action_repo: ActionRepoDep,
    policy_repo: PolicyRepoDep,
    fixed_repo: FixedRepoDep,
    draft_repo: DraftRepoDep,
    review_repo: ReviewRepoDep,
    session: SessionDep,
) -> ReplanResponse:
    """주간 리포트를 작성하고, 남은 작업 + 수락한 회복을 **다음 주부터 마감까지** 다시 배치.

    - 대상: 다음 주 이후 미착수 블록의 액션 + 활성 블록 없는 planned 백로그(수락한 회복 포함).
      과거·시작/완료·user_edit 블록은 불변. 실패 원본은 미래 블록이 없어 자동 제외.
    - busy = 확정(시작/완료·user_edit) 블록 + DB 시간정책 + **고정일정(#112 정합)**.
    - 각 새 블록에 '교체할 옛 블록 id'(replacesBlockId)를 실어, 승인이 blanket-cancel 없이
      그 블록만 현재 상태로 재조정 취소하게 한다(#117). 산출물은 Draft — 자동 적용 금지.
    """
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        today = now_kst().date()
        this_monday = today - timedelta(days=today.weekday())
        # 직전 완료 주의 주간 리포트 작성(그 데이터가 회복 수락→백로그로 상류 반영됨).
        await run_weekly_review_for_user(
            user.id, this_monday - timedelta(days=7), now_kst(), repo=review_repo, force=True
        )

        window_start = replan.next_week_start(today)
        scan_start, scan_end = replan.day_bounds_kst(
            window_start, window_start + timedelta(days=365)
        )
        scheduled_pairs = await block_repo.list_scheduled_between(user.id, scan_start, scan_end)
        backlog = await action_repo.list_planned_without_block(user.id)
        committed_blocks = await block_repo.list_committed_between(user.id, scan_start, scan_end)

        # 후보(action_id dedup) + 각 후보가 교체할 옛 미래 블록 **전부**.
        # #115 스케줄러가 긴 액션을 여러 세션 블록으로 쪼개므로 한 액션에 옛 블록이 여러 개일
        # 수 있다. 1개만 잡으면 승인 때 나머지가 유령으로 남거나 새 세션이 드롭된다(리뷰 지적).
        cand: dict[UUID, replan.ReplanCandidate] = {}
        old_blocks_by_action: dict[UUID, list[UUID]] = {}
        for block, action in scheduled_pairs:
            cand[action.id] = replan.ReplanCandidate(
                action_id=action.id,
                title=action.title,
                category=action.category,
                estimated_minutes=action.estimated_minutes or 30,
            )
            old_blocks_by_action.setdefault(action.id, []).append(block.id)
        for action in backlog:
            cand.setdefault(
                action.id,
                replan.ReplanCandidate(
                    action_id=action.id,
                    title=action.title,
                    category=action.category,
                    estimated_minutes=action.estimated_minutes or 30,
                ),
            )
        candidates = list(cand.values())

        deadline = window_start
        for block, _action in scheduled_pairs:
            deadline = max(deadline, to_kst(block.start_at).date())
        for action in backlog:
            if action.target_date is not None:
                deadline = max(deadline, action.target_date)
        # 미래 블록이 없고 backlog target_date 가 전부 과거/None 이면 deadline 이 window_start
        # 에 머물러 창이 '하루'로 붕괴 → 남은 일이 next Monday 하루에 몰린다(cramming). 마감
        # 신호가 없을 때는 최소 한 주(다음 주 월~일)에 걸쳐 분산하도록 지평을 넓힌다(#117).
        if deadline <= window_start:
            deadline = window_start + timedelta(days=6)
        # 먼 미래 backlog target_date 로 지평이 몇 년까지 벌어져 busy 루프·분산이 폭주하지
        # 않도록 스캔 창(1년)으로 상한. 그보다 먼 카드는 다음 재계획이 다시 당겨온다.
        deadline = min(deadline, window_start + timedelta(days=365))

        policies = _active_or_default_policies(await policy_repo.list_active(user.id))
        fixed: list[Any] = list(await fixed_repo.list_active(user.id))
        committed = replan.committed_busy_from_blocks(
            [(b.start_at, b.end_at) for b in committed_blocks]
        )
        day = window_start
        while day <= deadline:
            committed.extend(time_policies_to_busy(day, policies))
            committed.extend(fixed_schedules_to_busy(day, fixed))
            day += timedelta(days=1)

        blocks, warnings = replan.build_forward_replan(
            window_start=window_start,
            horizon_day=deadline,
            candidates=candidates,
            committed_busy=committed,
            tuning=_REPLAN_TUNING,
        )

        payload: dict[str, Any] = {
            "kind": "replan",
            "window_start": window_start.isoformat(),
            "horizon": deadline.isoformat(),
            "blocks": [
                {
                    "actionId": f"{_ACTION_PREFIX}{b.action_id}",
                    "title": b.title,
                    "category": b.category,
                    "start": b.start.isoformat(),
                    "end": b.end.isoformat(),
                    "replacesBlockId": (  # 미리보기용(대표 1개) — 재조정 권위는 아래 oldBlocks.
                        f"{_BLOCK_PREFIX}{old_blocks_by_action[b.action_id][0]}"
                        if b.action_id in old_blocks_by_action
                        else None
                    ),
                }
                for b in blocks
            ],
            # 재조정 권위 소스: 액션당 교체할 옛 블록 **전부**. 승인이 액션 단위로 옛 블록 집합을
            # 통째 취소하고 새 세션 블록을 전부 생성한다(#117 다중 세션 손실·유령 봉합, 리뷰 대응).
            "oldBlocks": {
                f"{_ACTION_PREFIX}{aid}": [f"{_BLOCK_PREFIX}{bid}" for bid in bids]
                for aid, bids in old_blocks_by_action.items()
            },
            "warnings": warnings,
        }
        draft = await draft_repo.create(
            user.id,
            target_date=window_start,
            horizon=deadline.isoformat(),
            ai_source="rule",
            payload=payload,
            expires_at=now_kst() + _DRAFT_TTL,
        )
        await session.commit()

    return _replan_response(draft)


@router.post("/replan/{plan_id}/approve")
async def approve_replan(
    plan_id: str,
    user: CurrentUser,
    block_repo: BlockRepoDep,
    action_repo: ActionRepoDep,
    draft_repo: DraftRepoDep,
    session: SessionDep,
) -> ReplanApproveResponse:
    """재계획 Draft 승인 → **action 단위 재조정**으로 미래 블록 교체(#117 재작업).

    #115 스케줄러가 긴 액션을 **여러 세션 블록**으로 쪼개므로, 재조정은 개별 블록이 아니라
    **액션당 '옛 블록 집합'(payload `oldBlocks`)** 을 통째 다룬다 — 옛 블록 1개만 취소하면
    나머지가 유령으로 남거나 새 세션이 드롭되던 문제 방지(리뷰 대응). 액션마다 현재 DB 상태로:
    - 옛 블록 집합 중 하나라도 `started/finished`(사용자 착수) → 이 액션 **전체 보존**(skip).
    - 활성(`scheduled`) 옛 블록이 하나도 없음(그새 전부 취소·삭제) → 중복 방지 skip.
    - 그 외 → 활성 옛 블록 **전부 취소** + 새 세션 블록 **전부 생성**.
    - 백로그(옛 블록 없음): 그새 그 action 이 활성 블록을 얻었으면 생성 skip.
    - action 이 그새 아카이브/삭제됐으면(예: #113 First Plan 교체) 전체 skip(좀비 블록 방지).

    Draft 로드·검사~쓰기를 `user_agent_lock`(xact-scoped) 안에서 단일 commit 으로 원자화한다
    (동시 더블 승인 봉합, #113 패턴). 과거·시작/완료·user_edit 블록은 불변. 항상 Draft→HITL.
    """
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        draft = await _load_draft(draft_repo, user.id, plan_id)
        payload = draft.payload
        if payload.get("kind") != "replan":
            raise ApiError(
                ErrorCode.PLAN_DRAFT_NOT_FOUND,
                "재계획 초안을 찾을 수 없어요.",
                http_status=HTTPStatus.NOT_FOUND,
            )
        if draft.status == "expired" or draft.expires_at < now_kst():
            raise ApiError(
                ErrorCode.PLAN_DRAFT_EXPIRED,
                "오래 두신 재계획 초안이 만료됐어요. 다시 만들어 볼까요?",
                http_status=HTTPStatus.GONE,
            )
        if draft.status == "approved":  # 멱등 — lock 안 확인이라 동시 승인이 직렬화됨
            return ReplanApproveResponse(
                plan_id=plan_id,
                cancelled_blocks=0,
                created_blocks=len(payload.get("blocks", [])),
                skipped_blocks=0,
                activated_at=now_kst(),
            )

        # payload 새 블록을 액션 단위로 묶는다 — 한 액션이 여러 세션 블록으로 나뉠 수 있으므로
        # (분할). 재조정은 '액션당 옛 블록 집합'을 통째 다루어 손실·유령을 막는다.
        new_by_action: dict[UUID, list[dict[str, Any]]] = {}
        for b in payload.get("blocks", []):
            aid = UUID(str(b["actionId"]).removeprefix(_ACTION_PREFIX))
            new_by_action.setdefault(aid, []).append(b)
        old_map: dict[str, list[str]] = payload.get("oldBlocks", {})

        cancelled = created = skipped = 0
        for action_id, new_blocks in new_by_action.items():
            n = len(new_blocks)
            # generate~approve 사이 action 이 아카이브/삭제됐으면(#113 supersede) 전체 skip.
            if await action_repo.get_by_id(user.id, action_id) is None:
                skipped += n
                continue
            old_ids = [
                UUID(str(x).removeprefix(_BLOCK_PREFIX))
                for x in old_map.get(f"{_ACTION_PREFIX}{action_id}", [])
            ]
            if old_ids:  # 교체 경로 — 옛 블록 집합을 재조정
                # 취소 전에 옛 블록을 **모두** 먼저 로드(autoflush 로 앞 취소가 뒤 조회에 새는
                # 것 방지). 하나라도 시작/완료면 액션 전체 보존.
                olds = [await block_repo.get_block(user.id, bid) for bid in old_ids]
                present = [o for o in olds if o is not None]
                if any(o.block_status in ("started", "finished") for o in present):
                    skipped += n
                    continue
                active = [o for o in present if o.block_status == "scheduled"]
                # 활성 옛 블록이 하나도 없으면(그새 전부 취소·삭제) 중복 방지로 skip.
                if not active:
                    skipped += n
                    continue
                for o in active:
                    o.block_status = "cancelled"
                    cancelled += 1
            elif await block_repo.list_by_action_item(user.id, action_id):
                # 백로그인데 그새 활성 블록이 생겼으면 중복 방지로 skip.
                skipped += n
                continue
            # 이 액션의 새 세션 블록을 전부 생성.
            for nb in new_blocks:
                await block_repo.create_block(
                    user_id=user.id,
                    action_item_id=action_id,
                    start_at=datetime.fromisoformat(str(nb["start"])),
                    end_at=datetime.fromisoformat(str(nb["end"])),
                    source="ai_plan",
                )
                created += 1

        await draft_repo.mark_approved(draft, approved_at=now_kst())
        await session.commit()

        return ReplanApproveResponse(
            plan_id=plan_id,
            cancelled_blocks=cancelled,
            created_blocks=created,
            skipped_blocks=skipped,
            activated_at=now_kst(),
        )
