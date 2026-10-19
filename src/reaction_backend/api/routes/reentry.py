"""Explicit user-initiated first actions; no AI application, pushes, or completion writes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from reaction_backend.api.deps import CurrentUser
from reaction_backend.api.routes.today import (
    ActionRepoDep,
    ExecutionRepoDep,
    SessionDep,
    _action_not_found,
    _parse_action_id,
)
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.reentry_attempt import ReentryAttempt
from reaction_backend.repositories.execution_repo import ExecutionRepo, settle_pause
from reaction_backend.repositories.reentry_repo import ReentryRepo, get_reentry_repo
from reaction_backend.safety.encryption import decrypt_memo, encrypt_memo
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.reentry import (
    ReentryEvent,
    ReentryPrepare,
    ReentryStart,
    ReentryState,
)

router = APIRouter(prefix="/reentry", tags=["reentry"])
Repo = Annotated[ReentryRepo, Depends(get_reentry_repo)]


def conflict(message: str) -> ApiError:
    return ApiError(ErrorCode.COMMON_VALIDATION_ERROR, message, http_status=409)


async def blocked(action: ActionItem, executions: ExecutionRepo) -> str | None:
    if action.status in ("done", "over_done"):
        return "이미 완료한 작업이에요."
    if action.target_date > now_kst().date():
        return "미래에 예정된 작업이에요. 주간 계획에서 확인해 주세요."
    block = await executions.find_open_block(action.user_id, action.id)
    # Recovery-derived work needs the existing explicit replan approval first.
    if action.source in ("recovery_downscope", "recovery_carryover") and block is None:
        return "변경 확인 화면에서 먼저 일정을 승인해 주세요."
    if block is not None and block.start_at > now_kst():
        return "예정 시각이 아직 되지 않았어요."
    return None


async def state(a: ReentryAttempt, action: ActionItem, executions: ExecutionRepo) -> ReentryState:
    execution = await executions.get_by_id(a.user_id, a.execution_id) if a.execution_id else None
    pause = await executions.get_open_pause(execution.id) if execution else None
    elapsed = 0
    if execution:
        end = execution.actual_end_at or (pause.created_at if pause else now_kst())
        start = execution.actual_start_at or execution.plan_start_at
        elapsed = max(0, int((end - start).total_seconds()) - execution.pause_total_minutes * 60)
    reason = await blocked(action, executions)
    if a.ended_at or (execution and execution.completion_status != "in_progress"):
        reason = "이미 종료한 시도예요. 오늘 화면에서 새로 확인해 주세요."
    return ReentryState(
        attempt_id=a.id,
        action_id=f"action_{action.id}",
        title=action.title,
        action_status=action.status,
        entry_kind=a.entry_kind,
        first_step=decrypt_memo(a.first_step_encrypted) if a.first_step_encrypted else None,
        execution_id=f"exec_{execution.id}" if execution else None,
        execution_status=("paused" if pause else execution.completion_status)
        if execution
        else None,
        started_at=a.started_at,
        step_completed_at=a.step_completed_at,
        ended_at=a.ended_at,
        elapsed_seconds=elapsed,
        can_start=reason is None,
        blocked_reason=reason,
    )


@router.post("/actions/{action_id}/prepare")
async def prepare(
    action_id: str,
    body: ReentryPrepare,
    user: CurrentUser,
    actions: ActionRepoDep,
    executions: ExecutionRepoDep,
    repo: Repo,
    session: SessionDep,
) -> ReentryState:
    action = await actions.get_by_id_for_update(user.id, _parse_action_id(action_id))
    if action is None:
        raise _action_not_found()
    attempt = await repo.get(user.id, body.request_id)
    if attempt is not None and attempt.action_item_id != action.id:
        raise conflict("다른 작업에 사용한 요청이에요.")
    if attempt is None:
        attempt = await repo.active(user.id, action.id)
        if attempt and attempt.execution_id:
            execution = await executions.get_by_id(user.id, attempt.execution_id)
            if not execution or execution.completion_status != "in_progress":
                attempt.ended_at = now_kst()
                attempt = None
    if attempt is None:
        active = await executions.get_active_for_action(user.id, action.id)
        history = await executions.action_ids_with_history(user.id, [action.id])
        attempt = ReentryAttempt(
            id=body.request_id,
            user_id=user.id,
            action_item_id=action.id,
            execution_id=active.id if active else None,
            entry_kind="resume"
            if active
            else "restart"
            if history or action.source.startswith("recovery")
            else "initial",
            first_step_encrypted=encrypt_memo(action.first_step) if action.first_step else None,
            created_at=now_kst(),
        )
        await repo.add(attempt)
    result = await state(attempt, action, executions)
    await session.commit()
    return result


async def load(
    attempt_id: UUID, user_id: UUID, actions: ActionRepoDep, repo: ReentryRepo
) -> tuple[ReentryAttempt, ActionItem]:
    attempt = await repo.get(user_id, attempt_id)
    if attempt is None:
        raise _action_not_found()
    # All mutations serialize on the action row, matching existing start/approve.
    action = await actions.get_by_id_for_update(user_id, attempt.action_item_id)
    if action is None:
        raise _action_not_found()
    attempt = await repo.get(user_id, attempt_id)
    assert attempt is not None
    return attempt, action


@router.post("/attempts/{attempt_id}/start")
async def start(
    attempt_id: UUID,
    body: ReentryStart,
    user: CurrentUser,
    actions: ActionRepoDep,
    executions: ExecutionRepoDep,
    repo: Repo,
    session: SessionDep,
) -> ReentryState:
    attempt, action = await load(attempt_id, user.id, actions, repo)
    reason = await blocked(action, executions)
    if reason or attempt.ended_at:
        raise conflict(reason or "이미 종료한 시도예요.")
    text = body.first_step.strip()
    if not text:
        raise conflict("이번에 할 행동을 한 줄로 적어 주세요.")
    execution = (
        await executions.get_by_id(user.id, attempt.execution_id) if attempt.execution_id else None
    )
    if execution and execution.completion_status != "in_progress":
        raise conflict("이미 종료한 실행이에요. 오늘 화면에서 새로 확인해 주세요.")
    if execution is None:
        execution = await executions.get_active_for_action(user.id, action.id)
    now = now_kst()
    if execution is None:
        block = await executions.find_open_block(user.id, action.id)
        if block is None:
            block = await executions.create_adhoc_block(
                user_id=user.id, action_item=action, start_at=now
            )
        else:
            block.block_status = "started"
        execution = await executions.create_execution(
            user_id=user.id, action_item_id=action.id, block=block, started_at=now
        )
        action.status = "in_progress"
    pause = await executions.get_open_pause(execution.id)
    if pause:
        settle_pause(execution, pause, now=now, resumed=True)
    if attempt.started_at is None:
        attempt.first_step_encrypted = encrypt_memo(text)
        attempt.selected_at = attempt.selected_at or now
        attempt.started_at = now
    if not attempt.first_step_encrypted:
        attempt.first_step_encrypted = encrypt_memo(text)
    attempt.execution_id = execution.id
    result = await state(attempt, action, executions)
    await session.commit()
    return result


@router.post("/attempts/{attempt_id}/events")
async def record(
    attempt_id: UUID,
    body: ReentryEvent,
    user: CurrentUser,
    actions: ActionRepoDep,
    executions: ExecutionRepoDep,
    repo: Repo,
    session: SessionDep,
) -> ReentryState:
    attempt, action = await load(attempt_id, user.id, actions, repo)
    now = now_kst()
    if body.event == "viewed":
        if not attempt.ended_at and await blocked(action, executions) is None:
            attempt.viewed_at = attempt.viewed_at or now
    elif body.event in ("selected", "start_clicked", "start_failed"):
        field = {
            "selected": "selected_at",
            "start_clicked": "clicked_at",
            "start_failed": "start_failed_at",
        }[body.event]
        if not attempt.ended_at:
            setattr(attempt, field, getattr(attempt, field) or now)
    elif body.event == "first_step_completed":
        if attempt.step_completed_at is None:
            execution = (
                await executions.get_by_id(user.id, attempt.execution_id)
                if attempt.execution_id
                else None
            )
            if (
                not attempt.started_at
                or attempt.ended_at
                or not execution
                or execution.completion_status != "in_progress"
            ):
                raise conflict("진행 중인 시도에서만 첫 행동을 기록할 수 있어요.")
            attempt.step_completed_at = now
        # No check-in, action status, recovery completion, or reward mutation.
    else:
        attempt.left_at = attempt.left_at or now
        if attempt.execution_id:
            execution = await executions.get_by_id(user.id, attempt.execution_id)
            if (
                execution
                and execution.completion_status == "in_progress"
                and await executions.get_open_pause(execution.id) is None
            ):
                await executions.create_pause(user_id=user.id, execution_id=execution.id)
        # Leaving preserves the active attempt for a later resume.
    result = await state(attempt, action, executions)
    await session.commit()
    return result
