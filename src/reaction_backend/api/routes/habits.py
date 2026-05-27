"""Habits — 반복 행동 + 주별 인스턴스 (S27, api-contract §7).

Issue #22 실구현:
- `/habits` CRUD 실 DB. frequency_per_week 1~7 (Pydantic + DB CheckConstraint 둘 다).
- `POST /habits` 시 이번 주 `habit_instances` **자동 생성** (cron 도입 전 임시; ADR-0005 §4 단계 5 cron 후속).
- `/habit-instances` GET (week 필터) + `POST /{id}/check` (done_count++).
- soft delete (`archived_at`).

두 라우터를 export — `main.py` 가 둘 다 include (#16 인증 router-level Depends 자동 적용).
"""

from __future__ import annotations

from datetime import date
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.habit import Habit as HabitModel
from reaction_backend.db.models.habit_instance import HabitInstance as HabitInstanceModel
from reaction_backend.db.session import get_db
from reaction_backend.repositories.habit_instance_repo import (
    HabitInstanceRepo,
    get_habit_instance_repo,
)
from reaction_backend.repositories.habit_repo import (
    HabitRepo,
    current_week_start_kst,
    get_habit_repo,
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

_HABIT_PREFIX = "habit_"
_INSTANCE_PREFIX = "hinst_"


def _to_habit(habit: HabitModel) -> Habit:
    return Habit(
        habit_id=f"{_HABIT_PREFIX}{habit.id}",
        title=habit.title,
        category=habit.category,
        frequency_per_week=habit.frequency_per_week,
        minutes_per_session=habit.minutes_per_session,
        time_preference=habit.time_preference,
        priority_level=habit.priority_level,
    )


def _to_instance(instance: HabitInstanceModel) -> HabitInstance:
    return HabitInstance(
        instance_id=f"{_INSTANCE_PREFIX}{instance.id}",
        habit_id=f"{_HABIT_PREFIX}{instance.habit_id}",
        week_start=instance.week_start.isoformat(),
        target_count=instance.target_count,
        done_count=instance.done_count,
    )


def _parse_habit_id(habit_id: str) -> UUID:
    if not habit_id.startswith(_HABIT_PREFIX):
        raise _habit_not_found()
    try:
        return UUID(habit_id[len(_HABIT_PREFIX) :])
    except ValueError as e:
        raise _habit_not_found() from e


def _parse_instance_id(instance_id: str) -> UUID:
    if not instance_id.startswith(_INSTANCE_PREFIX):
        raise _instance_not_found()
    try:
        return UUID(instance_id[len(_INSTANCE_PREFIX) :])
    except ValueError as e:
        raise _instance_not_found() from e


def _habit_not_found() -> ApiError:
    return ApiError(
        ErrorCode.HABIT_NOT_FOUND,
        "해당 습관을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _instance_not_found() -> ApiError:
    return ApiError(
        ErrorCode.HABIT_NOT_FOUND,
        "해당 습관 인스턴스를 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _parse_week_start(value: str | None) -> date:
    if value is None:
        return current_week_start_kst()
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "weekStart 형식이 올바르지 않아요 (YYYY-MM-DD).",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="weekStart",
        ) from e


HabitRepoDep = Annotated[HabitRepo, Depends(get_habit_repo)]
InstanceRepoDep = Annotated[HabitInstanceRepo, Depends(get_habit_instance_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


# ── /habits ──────────────────────────────────────────────────────────────────


@router.get("")
async def list_habits(user: CurrentUser, repo: HabitRepoDep) -> list[Habit]:
    """내 활성 습관 전체."""
    items = await repo.list_active(user.id)
    return [_to_habit(h) for h in items]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_habit(
    body: HabitCreateRequest,
    user: CurrentUser,
    repo: HabitRepoDep,
    instance_repo: InstanceRepoDep,
    session: SessionDep,
) -> Habit:
    """신규 습관 + 이번 주 instance 자동 생성 (cron 도입 전 임시)."""
    habit = await repo.create(
        user_id=user.id,
        title=body.title,
        category=body.category,
        frequency_per_week=body.frequency_per_week,
        minutes_per_session=body.minutes_per_session,
        time_preference=body.time_preference,
        priority_level=body.priority_level,
    )
    await instance_repo.create_or_get_for_week(
        habit_id=habit.id,
        week_start=current_week_start_kst(),
        target_count=body.frequency_per_week,
    )
    await session.commit()
    await session.refresh(habit)
    return _to_habit(habit)


@router.patch("/{habit_id}")
async def update_habit(
    habit_id: str,
    body: HabitUpdateRequest,
    user: CurrentUser,
    repo: HabitRepoDep,
    session: SessionDep,
) -> Habit:
    """습관 부분 수정 — 제목 · 빈도. 빈도 변경 시 `target_count` 도 동기화."""
    habit = await repo.get_by_id(user.id, _parse_habit_id(habit_id))
    if habit is None:
        raise _habit_not_found()
    updated = await repo.update(
        habit,
        title=body.title,
        frequency_per_week=body.frequency_per_week,
    )
    await session.commit()
    await session.refresh(updated)
    return _to_habit(updated)


@router.delete("/{habit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_habit(
    habit_id: str, user: CurrentUser, repo: HabitRepoDep, session: SessionDep
) -> None:
    """습관 soft delete (`archived_at`)."""
    habit = await repo.get_by_id(user.id, _parse_habit_id(habit_id))
    if habit is None:
        raise _habit_not_found()
    await repo.soft_delete(habit)
    await session.commit()
    return None


# ── /habit-instances ─────────────────────────────────────────────────────────


@router_instances.get("")
async def list_instances(
    user: CurrentUser,
    instance_repo: InstanceRepoDep,
    week_start: Annotated[str | None, Query(alias="weekStart")] = None,
) -> list[HabitInstance]:
    """그 주의 모든 활성 habit 인스턴스. weekStart 누락 시 이번 주(KST 월요일)."""
    ws = _parse_week_start(week_start)
    items = await instance_repo.list_for_user_week(user.id, ws)
    return [_to_instance(i) for i in items]


@router_instances.post("/{instance_id}/check")
async def check_instance(
    instance_id: str,
    user: CurrentUser,
    instance_repo: InstanceRepoDep,
    session: SessionDep,
) -> HabitInstance:
    """1회 달성 카운트 증가. user_id scope 는 habit 조인으로 자동 검증."""
    instance = await instance_repo.get_for_user(user.id, _parse_instance_id(instance_id))
    if instance is None:
        raise _instance_not_found()
    updated = await instance_repo.increment_done(instance)
    await session.commit()
    await session.refresh(updated)
    return _to_instance(updated)
