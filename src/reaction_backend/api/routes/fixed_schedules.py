"""Fixed Schedules — 수동 고정 일정 (S05, api-contract §19).

Issue #17 실구현:
- 캘린더 미연결 사용자가 수업·알바 등 정기 일정을 직접 입력.
- 첫 POST 시 onboarding_state 전이: CALENDAR / MANUAL_SCHEDULE → POLICIES.
- soft delete (`archived_at`). hard delete X.
"""

from __future__ import annotations

from datetime import time
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.fixed_schedule import FixedSchedule as FixedScheduleModel
from reaction_backend.db.session import get_db
from reaction_backend.repositories.fixed_schedule_repo import (
    FixedScheduleRepo,
    get_fixed_schedule_repo,
)
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.fixed_schedules import (
    FixedSchedule,
    FixedScheduleCreateRequest,
    FixedScheduleUpdateRequest,
)

router = APIRouter(prefix="/fixed-schedules", tags=["fixed-schedules"])

_ID_PREFIX = "fixed_"
_VALID_DAYS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})


def _to_schema(schedule: FixedScheduleModel) -> FixedSchedule:
    return FixedSchedule(
        schedule_id=f"{_ID_PREFIX}{schedule.id}",
        title=schedule.title,
        days_of_week=list(schedule.days_of_week),
        start_time=schedule.start_time.strftime("%H:%M"),
        end_time=schedule.end_time.strftime("%H:%M"),
    )


def _parse_hhmm(value: str, *, field: str) -> time:
    try:
        h, m = value.split(":", 1)
        return time(int(h), int(m))
    except (ValueError, TypeError) as e:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            f"{field} 형식이 올바르지 않아요 (HH:MM).",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field=field,
        ) from e


def _validate_time_window(start: time, end: time, *, start_field: str = "startTime") -> None:
    if start >= end:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "시작 시각은 종료 시각보다 빨라야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field=start_field,
        )


def _validate_days(days: list[str]) -> list[str]:
    invalid = [d for d in days if d not in _VALID_DAYS]
    if invalid:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            f"요일 값이 올바르지 않아요: {invalid}. mon/tue/wed/thu/fri/sat/sun 중에서.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="daysOfWeek",
        )
    return days


def _parse_schedule_id(schedule_id: str) -> UUID:
    if not schedule_id.startswith(_ID_PREFIX):
        raise _not_found()
    try:
        return UUID(schedule_id[len(_ID_PREFIX) :])
    except ValueError as e:
        raise _not_found() from e


def _not_found() -> ApiError:
    return ApiError(
        ErrorCode.FIXED_SCHEDULE_NOT_FOUND,
        "해당 고정 일정을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


RepoDep = Annotated[FixedScheduleRepo, Depends(get_fixed_schedule_repo)]
UserRepoDep = Annotated[UserRepo, Depends(get_user_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("")
async def list_schedules(user: CurrentUser, repo: RepoDep) -> list[FixedSchedule]:
    """내 활성 고정 일정 전체 (시작 시각 오름차순)."""
    items = await repo.list_active(user.id)
    return [_to_schema(s) for s in items]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_schedule(
    body: FixedScheduleCreateRequest,
    user: CurrentUser,
    repo: RepoDep,
    user_repo: UserRepoDep,
    session: SessionDep,
) -> FixedSchedule:
    """신규 고정 일정.

    부수 효과: 사용자가 `ONBOARDING_CALENDAR` 또는 `ONBOARDING_MANUAL_SCHEDULE`
    단계에 있으면 `ONBOARDING_POLICIES` 로 전이 (멱등).
    """
    days = _validate_days(body.days_of_week)
    start = _parse_hhmm(body.start_time, field="startTime")
    end = _parse_hhmm(body.end_time, field="endTime")
    _validate_time_window(start, end)

    schedule = await repo.create(
        user_id=user.id,
        title=body.title,
        days_of_week=days,
        start_time=start,
        end_time=end,
    )
    await user_repo.advance_onboarding(
        user,
        expected_from=("ONBOARDING_CALENDAR", "ONBOARDING_MANUAL_SCHEDULE"),
        to="ONBOARDING_POLICIES",
    )
    await session.commit()
    await session.refresh(schedule)
    return _to_schema(schedule)


@router.patch("/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: FixedScheduleUpdateRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> FixedSchedule:
    """고정 일정 부분 수정 — 입력된 필드만 갱신."""
    schedule = await repo.get_by_id(user.id, _parse_schedule_id(schedule_id))
    if schedule is None:
        raise _not_found()

    days = _validate_days(body.days_of_week) if body.days_of_week is not None else None
    start = _parse_hhmm(body.start_time, field="startTime") if body.start_time else None
    end = _parse_hhmm(body.end_time, field="endTime") if body.end_time else None
    # 둘 중 하나만 바뀌어도 합성된 window 가 유효해야 함
    new_start = start if start is not None else schedule.start_time
    new_end = end if end is not None else schedule.end_time
    _validate_time_window(new_start, new_end)

    updated = await repo.update(
        schedule,
        title=body.title,
        days_of_week=days,
        start_time=start,
        end_time=end,
    )
    await session.commit()
    await session.refresh(updated)
    return _to_schema(updated)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: str,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> None:
    """고정 일정 soft delete (`archived_at`). hard delete 금지 (AGENTS.md §2)."""
    schedule = await repo.get_by_id(user.id, _parse_schedule_id(schedule_id))
    if schedule is None:
        raise _not_found()
    await repo.soft_delete(schedule)
    await session.commit()
    return None
