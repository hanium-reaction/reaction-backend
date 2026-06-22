"""Planning — Weekly plan (S06, S14, S15, S16).

Issue #21-B: 주간 그리드 조회(S14) + 블록 직접 편집(S15).
- Plan 테이블은 없음 — `planId` 는 주(週) 논리 식별자(`plan_<weekStart>`). 편집 권한은 `blockId`
  (영속 `scheduled_blocks`)가 가진다. 15분 snap·시간 충돌(422)·정책 위반(422)은
  순수 로직 `orchestrator/plan_edit.py` 로 판정.

POST /plans/generate (S06, Goal Structuring) 는 #18/#32 범위 — 501 유지.

endpoint:
- GET   /plans/weekly?weekStart=YYYY-MM-DD       — 7일 블록 그리드 (S14)
- PATCH /plans/{planId}/blocks/{blockId}         — 15분 snap 이동 (S15)
- POST  /plans/generate                          — 🚧 501 (#18/#32)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator.plan_edit import find_policy_violation, snap_to_15min
from reaction_backend.repositories.action_item_repo import ActionItemRepo, get_action_item_repo
from reaction_backend.repositories.scheduled_block_repo import (
    ScheduledBlockRepo,
    get_scheduled_block_repo,
)
from reaction_backend.repositories.time_policy_repo import TimePolicyRepo, get_time_policy_repo
from reaction_backend.schemas.common import KST, now_kst, to_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.planning import (
    BlockEditRequest,
    BlockEditResponse,
    WeeklyBlock,
    WeeklyPlanDay,
    WeeklyPlanResponse,
)

router = APIRouter(prefix="/plans", tags=["planning"])

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

BlockRepoDep = Annotated[ScheduledBlockRepo, Depends(get_scheduled_block_repo)]
ActionRepoDep = Annotated[ActionItemRepo, Depends(get_action_item_repo)]
PolicyRepoDep = Annotated[TimePolicyRepo, Depends(get_time_policy_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


def _monday_of(day: date) -> date:
    """그 날이 속한 주의 월요일 (월=0)."""
    return day - timedelta(days=day.weekday())


def _week_bounds(monday: date) -> tuple[datetime, datetime]:
    start_dt = datetime.combine(monday, time.min, tzinfo=KST)
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


def _parse_block_id(raw: str) -> UUID:
    if not raw.startswith(_BLOCK_PREFIX):
        raise _block_not_found()
    try:
        return UUID(raw[len(_BLOCK_PREFIX) :])
    except ValueError as e:
        raise _block_not_found() from e


def _block_not_found() -> ApiError:
    return ApiError(
        ErrorCode.PLAN_BLOCK_NOT_FOUND,
        "해당 일정 블록을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _parse_dt(raw: str, field: str) -> datetime:
    """ISO 8601 파싱 → KST aware. naive 면 KST 로 간주. 형식 오류 422."""
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
        local_date = to_kst(block.start_at).date()
        bucket = by_date.get(local_date)
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

    new_start = snap_to_15min(_parse_dt(body.start_at, "startAt"))
    if body.end_at is not None:
        new_end = snap_to_15min(_parse_dt(body.end_at, "endAt"))
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


@router.post("/generate", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def generate_plan() -> None:
    """주간/horizon 계획 생성 — Goal Structuring orchestrator 실행 (#18/#32)."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §8 — to be implemented in #18/#32.",
    )
