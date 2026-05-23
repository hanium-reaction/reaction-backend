"""Habits — 반복 행동 + 주별 인스턴스 (S27, api-contract §7).

#3-D 단계는 **mock 스텁**. 실제 frequency 한도 enforcement·3주 미달 페널티 평가는 후속(#22).

본 모듈은 **두 라우터**를 export:
- `router` (prefix `/habits`) — 습관 CRUD
- `router_instances` (prefix `/habit-instances`) — 주별 인스턴스 조회·달성 체크
`main.py` 가 둘 다 include 한다.
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from reaction_backend.api.mock.habits import (
    DEMO_HABIT_INSTANCES,
    DEMO_HABITS,
    DemoHabit,
    DemoHabitInstance,
)
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.habits import (
    Habit,
    HabitCreateRequest,
    HabitInstance,
    HabitUpdateRequest,
)

router = APIRouter(prefix="/habits", tags=["habits"])
router_instances = APIRouter(prefix="/habit-instances", tags=["habits"])


def _to_habit(demo: DemoHabit) -> Habit:
    return Habit(
        habit_id=demo.habit_id,
        title=demo.title,
        category=demo.category,
        frequency_per_week=demo.frequency_per_week,
        minutes_per_session=demo.minutes_per_session,
        time_preference=demo.time_preference,
        priority_level=demo.priority_level,
    )


def _to_instance(demo: DemoHabitInstance) -> HabitInstance:
    return HabitInstance(
        instance_id=demo.instance_id,
        habit_id=demo.habit_id,
        week_start=demo.week_start,
        target_count=demo.target_count,
        done_count=demo.done_count,
    )


def _find_habit(habit_id: str) -> DemoHabit:
    """스텁은 DEMO_HABITS 의 id 만 유효."""
    for habit in DEMO_HABITS:
        if habit.habit_id == habit_id:
            return habit
    raise ApiError(
        ErrorCode.HABIT_NOT_FOUND,
        "해당 습관을 찾을 수 없어요.",
        http_status=status.HTTP_404_NOT_FOUND,
    )


def _find_instance(instance_id: str) -> DemoHabitInstance:
    """스텁은 DEMO_HABIT_INSTANCES 의 id 만 유효."""
    for instance in DEMO_HABIT_INSTANCES:
        if instance.instance_id == instance_id:
            return instance
    raise ApiError(
        ErrorCode.HABIT_NOT_FOUND,
        "해당 습관 인스턴스를 찾을 수 없어요.",
        http_status=status.HTTP_404_NOT_FOUND,
    )


# ── /habits ──────────────────────────────────────────────────────────────────


@router.get("")
async def list_habits() -> list[Habit]:
    """[stub] 내 습관 전체."""
    return [_to_habit(habit) for habit in DEMO_HABITS]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_habit(body: HabitCreateRequest) -> Habit:
    """[stub] 신규 습관 추가."""
    return Habit(
        habit_id="habit_new_stub",
        title=body.title,
        category=body.category,
        frequency_per_week=body.frequency_per_week,
        minutes_per_session=body.minutes_per_session,
        time_preference=body.time_preference,
        priority_level=body.priority_level,
    )


@router.patch("/{habit_id}")
async def update_habit(habit_id: str, body: HabitUpdateRequest) -> Habit:
    """[stub] 습관 부분 수정 — 제목·빈도."""
    demo = _find_habit(habit_id)
    return Habit(
        habit_id=demo.habit_id,
        title=body.title if body.title is not None else demo.title,
        category=demo.category,
        frequency_per_week=(
            body.frequency_per_week
            if body.frequency_per_week is not None
            else demo.frequency_per_week
        ),
        minutes_per_session=demo.minutes_per_session,
        time_preference=demo.time_preference,
        priority_level=demo.priority_level,
    )


@router.delete("/{habit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_habit(habit_id: str) -> None:
    """[stub] 습관 soft delete (archived_at)."""
    _find_habit(habit_id)
    return None


# ── /habit-instances ─────────────────────────────────────────────────────────


@router_instances.get("")
async def list_instances(
    week_start: Annotated[str | None, Query(alias="weekStart")] = None,
) -> list[HabitInstance]:
    """[stub] 이번 주 Habit 인스턴스. `?weekStart=` 필터 (스텁은 미적용)."""
    return [_to_instance(instance) for instance in DEMO_HABIT_INSTANCES]


@router_instances.post("/{instance_id}/check")
async def check_instance(instance_id: str) -> HabitInstance:
    """[stub] 1회 달성 카운트 증가."""
    demo = _find_instance(instance_id)
    return HabitInstance(
        instance_id=demo.instance_id,
        habit_id=demo.habit_id,
        week_start=demo.week_start,
        target_count=demo.target_count,
        done_count=demo.done_count + 1,
    )
