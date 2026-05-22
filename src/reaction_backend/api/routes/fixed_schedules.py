"""Fixed Schedules — 수동 고정 일정 (S05, api-contract §19).

#3-C 단계는 **mock 스텁**: 캘린더 미연결 사용자의 수업·알바 등 고정 일정 CRUD.
"""

from fastapi import APIRouter, status

from reaction_backend.api.mock.fixed_schedules import DEMO_FIXED_SCHEDULES, DemoFixedSchedule
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.fixed_schedules import (
    FixedSchedule,
    FixedScheduleCreateRequest,
    FixedScheduleUpdateRequest,
)

router = APIRouter(prefix="/fixed-schedules", tags=["fixed-schedules"])


def _to_schema(demo: DemoFixedSchedule) -> FixedSchedule:
    return FixedSchedule(
        schedule_id=demo.schedule_id,
        title=demo.title,
        days_of_week=list(demo.days_of_week),
        start_time=demo.start_time,
        end_time=demo.end_time,
    )


def _find(schedule_id: str) -> DemoFixedSchedule:
    """스텁은 DEMO_FIXED_SCHEDULES 의 id 만 유효 — 그 외는 404."""
    for schedule in DEMO_FIXED_SCHEDULES:
        if schedule.schedule_id == schedule_id:
            return schedule
    raise ApiError(
        ErrorCode.FIXED_SCHEDULE_NOT_FOUND,
        "해당 고정 일정을 찾을 수 없어요.",
        http_status=status.HTTP_404_NOT_FOUND,
    )


@router.get("")
async def list_schedules() -> list[FixedSchedule]:
    """[stub] 내 고정 일정 전체."""
    return [_to_schema(schedule) for schedule in DEMO_FIXED_SCHEDULES]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_schedule(body: FixedScheduleCreateRequest) -> FixedSchedule:
    """[stub] 신규 고정 일정 추가."""
    return FixedSchedule(
        schedule_id="fixed_new_stub",
        title=body.title,
        days_of_week=body.days_of_week,
        start_time=body.start_time,
        end_time=body.end_time,
    )


@router.patch("/{schedule_id}")
async def update_schedule(schedule_id: str, body: FixedScheduleUpdateRequest) -> FixedSchedule:
    """[stub] 고정 일정 부분 수정."""
    demo = _find(schedule_id)
    return FixedSchedule(
        schedule_id=demo.schedule_id,
        title=body.title if body.title is not None else demo.title,
        days_of_week=(
            body.days_of_week if body.days_of_week is not None else list(demo.days_of_week)
        ),
        start_time=body.start_time if body.start_time is not None else demo.start_time,
        end_time=body.end_time if body.end_time is not None else demo.end_time,
    )


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: str) -> None:
    """[stub] 고정 일정 soft delete (archived_at)."""
    _find(schedule_id)
    return None
