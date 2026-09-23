"""Habits — 반복 행동 + 주별 인스턴스 (S27, api-contract §7).

Issue #22 실구현:
- `/habits` CRUD 실 DB. frequency_per_week 1~7 (Pydantic + DB CheckConstraint 둘 다).
- `POST /habits` 시 이번 주 `habit_instances` **자동 생성** (cron 도입 전 임시; ADR-0005 §4 단계 5 cron 후속).
- `/habit-instances` GET (week 필터) + `POST /{id}/check` (done_count++) +
  `POST /{id}/uncheck` (잘못 누른 체크 되돌리기, done_count--).
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
    first_week_target,
    get_habit_repo,
    week_target,
)
from reaction_backend.schemas.common import KST
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.habits import (
    Habit,
    HabitCreateRequest,
    HabitInstance,
    HabitUpdateRequest,
)

router = APIRouter(prefix="/habits", tags=["habits"])
router_instances = APIRouter(prefix="/habit-instances", tags=["habits"])

# goals.py(만다라 칸 ↔ 습관)도 같은 접두를 쓴다 — 한 곳에서만 정의한다.
HABIT_ID_PREFIX = "habit_"
_HABIT_PREFIX = HABIT_ID_PREFIX
_INSTANCE_PREFIX = "hinst_"


def to_habit_schema(habit: HabitModel, *, current_instance_id: UUID | None = None) -> Habit:
    """`Habit` 응답 — `/habits` 와 만다라 반복형 전환(`goals.py`)이 같은 변환을 쓴다."""
    return Habit(
        habit_id=f"{_HABIT_PREFIX}{habit.id}",
        title=habit.title,
        category=habit.category,
        frequency_per_week=habit.frequency_per_week,
        minutes_per_session=habit.minutes_per_session,
        time_preference=habit.time_preference,
        priority_level=habit.priority_level,
        # 만다라 반복형 칸에서 만들어진 습관이면 그 칸 id — `node_` 접두는 goals.py 의
        # _NODE_PREFIX 와 같은 값(ADR-0008 §1).
        goal_node_id=f"node_{habit.goal_node_id}" if habit.goal_node_id is not None else None,
        current_instance_id=(
            f"{_INSTANCE_PREFIX}{current_instance_id}" if current_instance_id is not None else None
        ),
    )


def habit_created_on(habit: HabitModel) -> date | None:
    """습관을 등록한 날(KST) — 등록 주 목표치(`week_target`) 계산용. 저장 전이면 None."""
    created_at = getattr(habit, "created_at", None)
    return created_at.astimezone(KST).date() if created_at is not None else None


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
    return [to_habit_schema(h) for h in items]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_habit(
    body: HabitCreateRequest,
    user: CurrentUser,
    repo: HabitRepoDep,
    instance_repo: InstanceRepoDep,
    session: SessionDep,
) -> Habit:
    """신규 습관 + 이번 주 instance 자동 생성.

    주별 생성은 `scheduler/habit_instances.py` cron 이 맡지만, 여기서도 만든다 — 주 중간에
    등록한 습관이 다음 월요일까지 오늘 화면에 안 보이면 안 된다. 같은 get-or-create 라
    cron 과 겹쳐도 1행. 이번 주 목표는 남은 날만큼으로 줄인다(`week_target`). 응답에 그
    인스턴스 id(`currentInstanceId`)를 실어 화면이 곧바로 체크할 수 있게 한다.
    """
    habit = await repo.create(
        user_id=user.id,
        title=body.title,
        category=body.category,
        frequency_per_week=body.frequency_per_week,
        minutes_per_session=body.minutes_per_session,
        time_preference=body.time_preference,
        priority_level=body.priority_level,
    )
    week_start = current_week_start_kst()
    instance = await instance_repo.create_or_get_for_week(
        habit_id=habit.id,
        week_start=week_start,
        target_count=first_week_target(body.frequency_per_week),
    )
    await session.commit()
    await session.refresh(habit)
    return to_habit_schema(habit, current_instance_id=instance.id)


@router.patch("/{habit_id}")
async def update_habit(
    habit_id: str,
    body: HabitUpdateRequest,
    user: CurrentUser,
    repo: HabitRepoDep,
    instance_repo: InstanceRepoDep,
    session: SessionDep,
) -> Habit:
    """습관 부분 수정 — 제목 · 빈도. 빈도 변경 시 `target_count` 도 동기화.

    이번 주 인스턴스의 목표치도 함께 바꾼다 — 예전엔 `habits` 만 고쳐서 주 5회 → 2회로 줄여도
    이번 주 카드가 0/5 로 남았다. 지난 주 기록은 그대로 둔다.
    """
    habit = await repo.get_by_id(user.id, _parse_habit_id(habit_id))
    if habit is None:
        raise _habit_not_found()
    updated = await repo.update(
        habit,
        title=body.title,
        frequency_per_week=body.frequency_per_week,
    )
    if body.frequency_per_week is not None:
        week_start = current_week_start_kst()
        await instance_repo.sync_week_target(
            updated.id,
            week_start,
            week_target(
                body.frequency_per_week,
                created_on=habit_created_on(updated),
                week_start=week_start,
            ),
        )
    await session.commit()
    await session.refresh(updated)
    return to_habit_schema(updated)


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
    session: SessionDep,
    week_start: Annotated[str | None, Query(alias="weekStart")] = None,
) -> list[HabitInstance]:
    """그 주의 모든 활성 habit 인스턴스. weekStart 누락 시 이번 주(KST 월요일).

    **이번 주**를 읽을 때는 없는 인스턴스를 먼저 채운다(`ensure_for_week`, 멱등) — 새 주
    인스턴스는 월요일 00:05 cron 이 만드는데, 그 전에 화면을 열면 습관에 체크할 대상이 없어
    누른 체크가 서버에 안 올라갔다. 지난 주·다음 주는 읽기만 한다.
    """
    ws = _parse_week_start(week_start)
    if ws == current_week_start_kst():
        await instance_repo.ensure_for_week(user.id, ws)
        await session.commit()
    items = await instance_repo.list_for_user_week(user.id, ws)
    return [_to_instance(i) for i in items]


async def _this_week_instance(
    instance: HabitInstanceModel,
    *,
    user_id: UUID,
    habit_repo: HabitRepo,
    instance_repo: HabitInstanceRepo,
) -> HabitInstanceModel:
    """지난 주 인스턴스로 온 체크를 **이번 주** 인스턴스로 옮긴다.

    일요일 밤에 열어 둔 오늘 화면에서 월요일에 체크하면 화면이 들고 있던 지난 주 id 가
    그대로 온다. 예전엔 지난 주 칸이 +1 되고(이미 목표치면 그냥 버려지고) 이번 주는 0 으로
    남았다. 지난 주 기록은 건드리지 않고, 이번 주 인스턴스(없으면 cron 과 같은 값으로 만든다)를
    대신 올린다.
    """
    week_start = current_week_start_kst()
    if instance.week_start >= week_start:
        return instance
    habit = await habit_repo.get_by_id(user_id, instance.habit_id)
    if habit is None:
        raise _instance_not_found()
    return await instance_repo.create_or_get_for_week(
        habit_id=habit.id, week_start=week_start, target_count=habit.target_count
    )


@router_instances.post("/{instance_id}/check")
async def check_instance(
    instance_id: str,
    user: CurrentUser,
    habit_repo: HabitRepoDep,
    instance_repo: InstanceRepoDep,
    session: SessionDep,
) -> HabitInstance:
    """1회 달성 카운트 증가. user_id scope 는 habit 조인으로 자동 검증.

    지난 주 인스턴스로 오면 이번 주 인스턴스를 올리고 **그 인스턴스**를 돌려준다(응답의
    `instanceId`·`weekStart` 로 화면이 새 주를 알 수 있다).
    """
    instance = await instance_repo.get_for_user(user.id, _parse_instance_id(instance_id))
    if instance is None:
        raise _instance_not_found()
    target = await _this_week_instance(
        instance, user_id=user.id, habit_repo=habit_repo, instance_repo=instance_repo
    )
    updated = await instance_repo.increment_done(target)
    await session.commit()
    await session.refresh(updated)
    return _to_instance(updated)


@router_instances.post("/{instance_id}/uncheck")
async def uncheck_instance(
    instance_id: str,
    user: CurrentUser,
    instance_repo: InstanceRepoDep,
    session: SessionDep,
) -> HabitInstance:
    """잘못 누른 체크 1회 되돌리기 — 0 아래로는 안 내려간다(다시 눌러도 안전).

    예전엔 체크를 되돌릴 방법이 없어, 잘못 누르거나 두 번 눌린 체크가 그 주 기록에 영영
    남았다. 다른 사용자의 인스턴스는 check 와 같은 404.
    """
    instance = await instance_repo.get_for_user(user.id, _parse_instance_id(instance_id))
    if instance is None:
        raise _instance_not_found()
    updated = await instance_repo.decrement_done(instance)
    await session.commit()
    await session.refresh(updated)
    return _to_instance(updated)
