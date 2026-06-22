"""Recovery — S19, S20. if-then 코핑 플랜 (Issue #20-A 수직 슬라이스).

흐름 (Orchestrator 2 — Recovery):
  DETECTING → DIAGNOSING → COACHING → HITL → UPDATING → SAVING

핵심 결정 (AGENTS.md §1):
- UX 4 그룹 (DOWNSCOPE / RESCHEDULE / CARRY_OVER / PARK) — 같은 그룹 동시 노출 1카드.
- 내부 9 전략은 `recovery_strategy_catalog` 기준, 통계/감사용 보존.
- 8초 안에 LLM 응답 못 받으면 heuristic fallback (PRD §9) — 룰 선택 + 카탈로그 템플릿.
- 원본 `action_item.status` (FAILED 등)는 절대 변경 X — Resilience 지표 전제.
- AI 출력 = Draft Layer (`is_draft=True`) → `/recovery/decisions` 에서만 확정.

#20-A 구현 범위:
- POST /recovery/proposals/generate — 룰 선택(orchestrator.recovery) + LLM personalize
  (`recovery/if_then_proposal`, fallback 시 템플릿) → recovery_attempts(pending) INSERT
- POST /recovery/decisions — 수락/스킵 저장 (Idempotency 미들웨어 §1.7).
  수락 그룹이 DOWNSCOPE/CARRY_OVER 면 새 ActionItem(source=recovery_*) 생성.

후속 (#20-B): GET /replan/{executionId} + POST /replan/{executionId}/approve (S20 diff).
"""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
from reaction_backend.db.session import get_db
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator.recovery import (
    first_matching_tag,
    render_template,
    select_strategies,
)
from reaction_backend.repositories.action_item_repo import (
    ActionItemRepo,
    get_action_item_repo,
)
from reaction_backend.repositories.recovery_repo import RecoveryRepo, get_recovery_repo
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.recovery import (
    RecoveryCard,
    RecoveryDecisionRequest,
    RecoveryDecisionResponse,
    RecoveryGenerateRequest,
    RecoveryProposalLLM,
    RecoveryProposalsResponse,
)

router = APIRouter(tags=["recovery"])

_EXEC_PREFIX = "exec_"
_ATTEMPT_PREFIX = "rec_"
_ACTION_PREFIX = "action_"

# 회복 대상 completion_status — 실패/부분완료만 (DevBaseline: 21시 회고 흐름에서 호출)
_ELIGIBLE_STATUSES = ("failed", "partial_done")

# 수락 시 새 ActionItem 을 만드는 그룹 (없는 그룹: RESCHEDULE / PARK — §5.16)
_GROUP_TO_SOURCE = {
    "DOWNSCOPE": "recovery_downscope",
    "CARRY_OVER": "recovery_carryover",
}

RecoveryRepoDep = Annotated[RecoveryRepo, Depends(get_recovery_repo)]
ActionRepoDep = Annotated[ActionItemRepo, Depends(get_action_item_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


def _parse_id(raw: str, prefix: str, error: ApiError) -> UUID:
    if not raw.startswith(prefix):
        raise error
    try:
        return UUID(raw[len(prefix) :])
    except ValueError as e:
        raise error from e


def _execution_not_found() -> ApiError:
    return ApiError(
        ErrorCode.RECOVERY_EXECUTION_NOT_FOUND,
        "해당 실행 기록을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _attempt_not_found() -> ApiError:
    return ApiError(
        ErrorCode.RECOVERY_ATTEMPT_NOT_FOUND,
        "해당 회복 카드를 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _to_card(attempt: RecoveryAttempt, strategy: RecoveryStrategyCatalog | None) -> RecoveryCard:
    return RecoveryCard(
        attempt_id=f"{_ATTEMPT_PREFIX}{attempt.id}",
        option_group=attempt.recovery_option_group,  # type: ignore[arg-type]
        strategy_type=attempt.recovery_strategy_type,
        label_ko=strategy.label_ko if strategy is not None else attempt.recovery_strategy_type,
        suggested_action_text=attempt.suggested_action_text or "",
        min_recovery_unit_minutes=(
            strategy.min_recovery_unit_minutes if strategy is not None else 5
        ),
        allow_rest_mode=strategy.allow_rest_mode if strategy is not None else False,
        trigger_tag=attempt.trigger_tag,
    )


async def _get_execution_or_404(
    user_id: UUID, raw_execution_id: str, repo: RecoveryRepo
) -> ExecutionEvent:
    execution_id = _parse_id(raw_execution_id, _EXEC_PREFIX, _execution_not_found())
    execution = await repo.get_execution(user_id, execution_id)
    if execution is None:
        raise _execution_not_found()
    return execution


@router.post("/recovery/proposals/generate", status_code=status.HTTP_201_CREATED)
async def generate_recovery_proposals(
    body: RecoveryGenerateRequest,
    user: CurrentUser,
    repo: RecoveryRepoDep,
    action_repo: ActionRepoDep,
    session: SessionDep,
) -> RecoveryProposalsResponse:
    """실패 컨텍스트 기반 회복 옵션 2~4개 생성 (LLM ≤ 8s + heuristic fallback).

    이미 pending 카드가 있으면 재생성하지 않고 그대로 반환한다 (중복 INSERT 방지).
    """
    execution = await _get_execution_or_404(user.id, body.execution_id, repo)
    if execution.completion_status not in _ELIGIBLE_STATUSES:
        raise ApiError(
            ErrorCode.RECOVERY_NOT_ELIGIBLE,
            "완료되지 않은(실패/부분완료) 실행에만 회복 카드를 만들 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="executionId",
        )

    strategies = await repo.list_active_strategies()
    catalog = {s.strategy_type: s for s in strategies}

    # 멱등 — pending 카드가 이미 있으면 그대로 반환 (재호출/새로고침 안전)
    existing = await repo.list_attempts(user.id, execution.id)
    pending = [a for a in existing if a.user_decision == "pending"]
    if pending:
        return RecoveryProposalsResponse(
            execution_id=body.execution_id,
            cards=[_to_card(a, catalog.get(a.recovery_strategy_type)) for a in pending],
            ai_source="rule" if all(a.llm_fallback_used for a in pending) else "llm",
        )

    failure_tags = await repo.list_failure_tag_codes(execution.id)
    selected = select_strategies(failure_tags, strategies)
    if not selected:
        raise ApiError(
            ErrorCode.RECOVERY_NO_PROPOSAL,
            "지금 제안할 수 있는 회복 카드가 없어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    # 템플릿 변수 — 원본 카드 제목 기반 (없어도 동작)
    action = await action_repo.get_by_id(user.id, execution.action_item_id)
    action_title = action.title if action is not None else ""
    variables = {
        "first_step": (action.first_step or action_title) if action is not None else "",
        "suspended_step": action_title,
    }
    texts = {s.strategy_type: render_template(s.if_then_template, variables) for s in selected}

    # LLM personalize — 선두 카드의 if-then 문구만. 실패 시 카탈로그 템플릿 그대로 (PRD §9).
    top = selected[0]
    result = await aiClient.run(
        module="recovery",
        schema=RecoveryProposalLLM,
        prompt_id="recovery/if_then_proposal",
        fallback=lambda: RecoveryProposalLLM(
            strategy_code=top.strategy_type,
            if_clause="",
            then_clause=texts[top.strategy_type],
            rationale="",
        ),
        timeout=8.0,
        variables={
            "failure_type": ", ".join(failure_tags) if failure_tags else "UNKNOWN",
            "confidence": "n/a",
            "interruption_summary": "없음",
            "context_summary": f"실행 카드: {action_title} / 결과: {execution.completion_status}",
        },
        user_id=user.id,
        session=session,
    )
    if not result.fell_back and result.value.strategy_code in texts:
        proposal = result.value
        personalized = " ".join(
            part for part in (proposal.if_clause, proposal.then_clause) if part
        ).strip()
        if personalized:
            texts[proposal.strategy_code] = personalized

    attempts = [
        await repo.create_attempt(
            user_id=user.id,
            execution_id=execution.id,
            option_group=s.option_group,
            strategy_type=s.strategy_type,
            suggested_action_text=texts[s.strategy_type],
            trigger_tag=first_matching_tag(failure_tags, s),
            llm_fallback_used=result.fell_back,
        )
        for s in selected
    ]
    await session.commit()

    return RecoveryProposalsResponse(
        execution_id=body.execution_id,
        cards=[_to_card(a, catalog.get(a.recovery_strategy_type)) for a in attempts],
        ai_source="rule" if result.fell_back else "llm",
    )


@router.post("/recovery/decisions")
async def decide_recovery(
    body: RecoveryDecisionRequest,
    user: CurrentUser,
    repo: RecoveryRepoDep,
    action_repo: ActionRepoDep,
    session: SessionDep,
) -> RecoveryDecisionResponse:
    """사용자 선택 저장 (Idempotency-Key 필수 — §1.7 미들웨어 enforce).

    - `accepted` → 선택 카드 accepted, 나머지 pending 은 rejected.
      그룹이 DOWNSCOPE/CARRY_OVER 면 새 ActionItem(source=recovery_*) 생성 — 원본
      카드 status 는 변경하지 않는다 (혈통: parent_action_item_id).
    - `skipped` → 모든 pending 카드 skipped ("오늘은 쉬기").
    """
    execution = await _get_execution_or_404(user.id, body.execution_id, repo)
    attempts = await repo.list_attempts(user.id, execution.id)
    pending = [a for a in attempts if a.user_decision == "pending"]
    if not pending:
        raise ApiError(
            ErrorCode.RECOVERY_ALREADY_DECIDED,
            "이 실행의 회복 카드는 이미 결정됐어요.",
            http_status=HTTPStatus.CONFLICT,
        )

    decided_at = now_kst()
    accepted_id: str | None = None
    rejected_ids: list[str] = []
    skipped_ids: list[str] = []
    resulting_action_id: str | None = None

    if body.decision == "accepted":
        if body.accepted_attempt_id is None:
            raise ApiError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                "수락할 카드(acceptedAttemptId)를 알려주세요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="acceptedAttemptId",
            )
        attempt_id = _parse_id(body.accepted_attempt_id, _ATTEMPT_PREFIX, _attempt_not_found())
        target = next((a for a in pending if a.id == attempt_id), None)
        if target is None:
            raise _attempt_not_found()

        target.user_decision = "accepted"
        target.recovery_decided_at = decided_at
        target.recovery_started_at = decided_at
        target.decision_reason = body.decision_reason
        accepted_id = f"{_ATTEMPT_PREFIX}{target.id}"

        for sibling in pending:
            if sibling.id == target.id:
                continue
            sibling.user_decision = "rejected"
            sibling.recovery_decided_at = decided_at
            rejected_ids.append(f"{_ATTEMPT_PREFIX}{sibling.id}")

        source = _GROUP_TO_SOURCE.get(target.recovery_option_group)
        if source is not None:
            original = await action_repo.get_by_id(user.id, execution.action_item_id)
            strategy = next(
                (
                    s
                    for s in await repo.list_active_strategies()
                    if s.strategy_type == target.recovery_strategy_type
                ),
                None,
            )
            target_date = decided_at.date()
            if target.recovery_option_group == "CARRY_OVER":
                target_date = target_date + timedelta(days=1)
            new_action = await action_repo.create_from_recovery(
                user_id=user.id,
                parent_action_item_id=execution.action_item_id,
                title=(target.suggested_action_text or "회복 액션")[:300],
                category=original.category if original is not None else "other",
                source=source,
                target_date=target_date,
                estimated_minutes=(
                    max(strategy.min_recovery_unit_minutes, 5) if strategy is not None else 5
                ),
            )
            target.resulting_action_item_id = new_action.id
            resulting_action_id = f"{_ACTION_PREFIX}{new_action.id}"
    else:  # skipped
        for sibling in pending:
            sibling.user_decision = "skipped"
            sibling.recovery_decided_at = decided_at
            sibling.decision_reason = body.decision_reason
            skipped_ids.append(f"{_ATTEMPT_PREFIX}{sibling.id}")

    await session.commit()

    return RecoveryDecisionResponse(
        execution_id=body.execution_id,
        accepted_attempt_id=accepted_id,
        rejected_attempt_ids=rejected_ids,
        skipped_attempt_ids=skipped_ids,
        resulting_action_item_id=resulting_action_id,
    )


@router.get("/replan/{execution_id}", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_replan_diff(execution_id: str) -> None:
    """S20 before/after diff — #20-B 후속."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §12 — to be implemented in #20-B.",
    )


@router.post("/replan/{execution_id}/approve", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def approve_replan(execution_id: str) -> None:
    """S20 최종 적용 (Idempotency) — #20-B 후속."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §12 — to be implemented in #20-B.",
    )
