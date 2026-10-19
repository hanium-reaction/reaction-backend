from datetime import date, datetime, time, timedelta
from uuid import uuid5

from fastapi import APIRouter

from reaction_backend.api.deps import CurrentUser
from reaction_backend.api.routes.today import (
    ActionRepoDep,
    ExecutionRepoDep,
    SessionDep,
    _action_not_found,
    _execution_not_found,
    _parse_action_id,
    _parse_execution_id,
    get_action_detail,
)
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.work_context import ActionResource, ExecutionNote
from reaction_backend.repositories.work_context_repo import WorkContextRepo
from reaction_backend.safety.encryption import decrypt_memo, encrypt_memo
from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.today import ActionDetail
from reaction_backend.schemas.work_context import (
    NoteInput,
    NoteView,
    PreferenceInput,
    QuickAction,
    ReentryEvidence,
    ResourceInput,
    ResourceView,
)

router = APIRouter(prefix="/work-context", tags=["work-context"])


@router.post("/quick-action")
async def quick_action(
    body: QuickAction, user: CurrentUser, actions: ActionRepoDep, session: SessionDep
) -> ActionDetail:
    repo = WorkContextRepo(session)
    await repo.lock_user(user.id)
    action_id = uuid5(user.id, str(body.request_id))
    action = await repo.existing_action(action_id)
    if action:
        if action.archived_at:
            raise _action_not_found()
        if (action.title, action.first_step, action.estimated_minutes, action.goal_id) != (
            body.title,
            body.first_step,
            body.attempt_minutes,
            body.goal_id,
        ):
            raise ApiError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                "같은 요청의 내용이 달라졌어요. 새 작업으로 다시 확인해 주세요.",
                http_status=409,
            )
    else:
        if body.goal_id and not await repo.goal(user.id, body.goal_id):
            raise _action_not_found()
        action = ActionItem(
            id=action_id,
            user_id=user.id,
            title=body.title,
            first_step=body.first_step,
            estimated_minutes=body.attempt_minutes,
            target_date=now_kst().date(),
            source="manual",
            status="planned",
            priority=3,
            category="other",
            goal_id=body.goal_id,
        )
        await repo.add_action(action)
    result = await get_action_detail(f"action_{action.id}", user, actions)
    await session.commit()
    return result


@router.get("/actions/{action_id}/resource")
async def get_resource(
    action_id: str, user: CurrentUser, actions: ActionRepoDep, session: SessionDep
) -> ResourceView:
    action = await actions.get_by_id(user.id, _parse_action_id(action_id))
    if not action:
        raise _action_not_found()
    row = await WorkContextRepo(session).resource(action.id)
    return ResourceView(
        resource=ResourceInput.model_validate_json(decrypt_memo(row.content_encrypted))
        if row and row.content_encrypted
        else None
    )


@router.put("/actions/{action_id}/resource")
async def put_resource(
    action_id: str,
    body: ResourceInput,
    user: CurrentUser,
    actions: ActionRepoDep,
    session: SessionDep,
) -> ResourceView:
    action = await actions.get_by_id_for_update(user.id, _parse_action_id(action_id))
    if not action:
        raise _action_not_found()
    row = await WorkContextRepo(session).resource(action.id)
    if row is None:
        row = ActionResource(action_id=action.id, user_id=user.id)
        session.add(row)
    row.content_encrypted = encrypt_memo(body.model_dump_json())
    await session.commit()
    return ResourceView(resource=body)


@router.post("/actions/{action_id}/resource/unlink")
async def unlink_resource(
    action_id: str, user: CurrentUser, actions: ActionRepoDep, session: SessionDep
) -> ResourceView:
    action = await actions.get_by_id_for_update(user.id, _parse_action_id(action_id))
    if not action:
        raise _action_not_found()
    row = await WorkContextRepo(session).resource(action.id)
    if row:
        row.content_encrypted = None
    await session.commit()
    return ResourceView(resource=None)


@router.get("/executions/{execution_id}/note")
async def get_note(
    execution_id: str, user: CurrentUser, executions: ExecutionRepoDep, session: SessionDep
) -> NoteView:
    execution = await executions.get_by_id(user.id, _parse_execution_id(execution_id))
    if not execution:
        raise _execution_not_found()
    row = await WorkContextRepo(session).note(execution.id)
    return NoteView(memo=decrypt_memo(row.memo_encrypted) if row and row.memo_encrypted else None)


@router.put("/executions/{execution_id}/note")
async def put_note(
    execution_id: str,
    body: NoteInput,
    user: CurrentUser,
    actions: ActionRepoDep,
    executions: ExecutionRepoDep,
    session: SessionDep,
) -> NoteView:
    execution = await executions.get_by_id(user.id, _parse_execution_id(execution_id))
    if not execution:
        raise _execution_not_found()
    if not await actions.get_by_id_for_update(user.id, execution.action_item_id):
        raise _action_not_found()
    row = await WorkContextRepo(session).note(execution.id)
    if row is None:
        row = ExecutionNote(execution_id=execution.id, user_id=user.id)
        session.add(row)
    row.memo_encrypted = encrypt_memo(body.memo) if body.memo else None
    await session.commit()
    return NoteView(memo=body.memo or None)


@router.put("/preference")
async def put_preference(
    body: PreferenceInput, user: CurrentUser, session: SessionDep
) -> PreferenceInput:
    locked = await WorkContextRepo(session).lock_user(user.id)
    prefs = dict(locked.focus_mode_preferences or {})
    if body.preference is None:
        prefs.pop("reentry_preference", None)
    else:
        prefs["reentry_preference"] = body.preference
    locked.focus_mode_preferences = prefs
    await session.commit()
    return body


@router.get("/evidence")
async def evidence(week_start: date, user: CurrentUser, session: SessionDep) -> ReentryEvidence:
    start = datetime.combine(week_start, time.min, tzinfo=KST)
    counts = await WorkContextRepo(session).evidence(user.id, start, start + timedelta(days=7))
    raw = (user.focus_mode_preferences or {}).get("reentry_preference")
    preference = PreferenceInput(preference=raw).preference
    return ReentryEvidence(
        week_start=week_start,
        week_end=week_start + timedelta(days=6),
        attempts=counts[0],
        started=counts[1],
        first_steps_reported=counts[2],
        tasks_completed=counts[3],
        preference=preference,
    )


@router.get("/preference")
async def get_preference(user: CurrentUser) -> PreferenceInput:
    return PreferenceInput(preference=(user.focus_mode_preferences or {}).get("reentry_preference"))


@router.get("/actions/{action_id}/last-note")
async def last_note(
    action_id: str, user: CurrentUser, actions: ActionRepoDep, session: SessionDep
) -> NoteView:
    action = await actions.get_by_id(user.id, _parse_action_id(action_id))
    if not action:
        raise _action_not_found()
    row = await WorkContextRepo(session).latest_note(user.id, action.id)
    return NoteView(memo=decrypt_memo(row.memo_encrypted) if row and row.memo_encrypted else None)
