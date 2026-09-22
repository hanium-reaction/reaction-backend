"""Fixed Schedules — 수동 고정 일정 (S05, api-contract §19).

Issue #17 실구현:
- 캘린더 미연결 사용자가 수업·알바 등 정기 일정을 직접 입력.
- 첫 POST 시 onboarding_state 전이: CALENDAR / MANUAL_SCHEDULE → POLICIES.
- soft delete (`archived_at`). hard delete X.
- 같은 요일에 시간이 겹치면 409 `FIXED_SCHEDULE_OVERLAP` (api-contract §19). 맞닿는 건 겹침이 아니다.

자정을 넘는 일정(22:00–02:00)은 **받지 않는다** — 둘로 나눠 넣게 안내한다. 저장 모델은
`start > end` 를 담을 수 있지만, 그걸 읽는 쪽(`goal_structuring.fixed_schedules_to_busy`)은 같은
요일 안에서 [00:00, end)·[start, 24:00) 으로 접어 버린다 — 금 22:00–02:00 이 **금요일 새벽**을
막고 정작 토요일 새벽은 비워 둔다. 다음 요일로 넘기는 전개·겹침 판정·오늘 화면 표시까지 같이
바뀌어야 하는 일이라 계약 변경으로 따로 다룬다.
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
from reaction_backend.orchestrator._common import user_agent_lock
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
from reaction_backend.schemas.goals import strip_invisible

router = APIRouter(prefix="/fixed-schedules", tags=["fixed-schedules"])

_ID_PREFIX = "fixed_"
_VALID_DAYS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})
#: `fixed_schedules.title` 은 String(200) — 넘기면 DB 가 거절해 예전엔 원인 모를 500 이었다.
_TITLE_MAX = 200
#: 오류 문구에 필드 이름(startTime) 대신 쓸 말 — 사용자에게 보이는 문장이다.
_FIELD_LABELS = {"startTime": "시작 시각", "endTime": "종료 시각"}
#: 사용자별 advisory lock 이름 — 겹침 검사와 저장 사이에 다른 요청이 끼지 못하게.
_LOCK_NAME = "fixed_schedules"


def _to_schema(schedule: FixedScheduleModel) -> FixedSchedule:
    return FixedSchedule(
        schedule_id=f"{_ID_PREFIX}{schedule.id}",
        title=schedule.title,
        days_of_week=list(schedule.days_of_week),
        start_time=schedule.start_time.strftime("%H:%M"),
        end_time=schedule.end_time.strftime("%H:%M"),
    )


def _parse_hhmm(value: str, *, field: str) -> time:
    """`HH:MM` → time. `"24:00"`(밤 12시까지)은 그날의 마지막 순간(`time.max`)이다.

    스케줄러(`goal_structuring._parse_hhmm`)·시간 정책(`time_policies._is_hhmm`)과 같은 규칙이다.
    예전엔 여기서만 24:00 을 거절해, 자정까지 하는 알바를 넣을 방법이 없었다. 응답은
    `strftime` 이라 `23:59` 로 보인다(다른 화면의 고정 일정 표시와 같다).
    """
    try:
        h_s, m_s = value.split(":", 1)
        h, m = int(h_s), int(m_s)
        if h == 24 and m == 0:
            return time.max
        return time(h, m)
    except (ValueError, TypeError) as e:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            f"{_FIELD_LABELS.get(field, '시각')} 형식이 올바르지 않아요 (예: 09:00).",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field=field,
        ) from e


def _validate_time_window(start: time, end: time, *, start_field: str = "startTime") -> None:
    if start > end:
        # 자정을 넘는 일정 — 모듈 독스트링. 왜 안 되는지와 어떻게 넣으면 되는지를 같이 말한다.
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "자정을 넘기는 일정은 둘로 나눠 넣어 주세요. 예: 금 22:00–24:00, 토 00:00–02:00",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field=start_field,
        )
    if start == end:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "시작 시각은 종료 시각보다 빨라야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field=start_field,
        )


def _normalize_title(raw: str) -> str:
    """앞뒤 공백을 걷어낸 제목. 비었거나 200자를 넘으면 422 — 예전엔 공백뿐인 제목이 저장되고
    201자는 DB 가 거절해 500 이었다."""
    title = strip_invisible(raw)
    if not title:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "일정 이름을 적어 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="title",
        )
    if len(title) > _TITLE_MAX:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            f"일정 이름은 {_TITLE_MAX}자까지 적을 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="title",
        )
    return title


def _validate_days(days: list[str]) -> list[str]:
    """요일 검증 + 중복 제거(순서 유지). 빈 목록은 422 — 어떤 요일에도 안 걸리는 일정은
    아무것도 막지 않으면서 목록에만 남는다."""
    if not days:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "요일을 하나 이상 골라 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="daysOfWeek",
        )
    invalid = [d for d in days if d not in _VALID_DAYS]
    if invalid:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            f"요일 값이 올바르지 않아요: {invalid}. mon/tue/wed/thu/fri/sat/sun 중에서.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="daysOfWeek",
        )
    return list(dict.fromkeys(days))


def _first_overlap(
    schedules: list[FixedScheduleModel],
    *,
    days: list[str],
    start: time,
    end: time,
    exclude_id: UUID | None = None,
) -> FixedScheduleModel | None:
    """같은 요일에 [start, end) 가 겹치는 첫 활성 일정. 맞닿는 건(10:00 끝·10:00 시작) 겹침이 아니다."""
    wanted = set(days)
    for other in schedules:
        if other.id == exclude_id or wanted.isdisjoint(other.days_of_week or []):
            continue
        if start < other.end_time and other.start_time < end:
            return other
    return None


def _overlap_error(other: FixedScheduleModel) -> ApiError:
    return ApiError(
        ErrorCode.FIXED_SCHEDULE_OVERLAP,
        f"이미 넣어 둔 '{other.title}' 일정과 시간이 겹쳐요.",
        http_status=HTTPStatus.CONFLICT,
    )


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

    같은 요일 시간이 겹치면 409 `FIXED_SCHEDULE_OVERLAP`. 검사와 저장은 사용자별 advisory
    lock 안에서 한다 — 느린 모바일에서 [추가] 를 두 번 누르면 예전엔 두 요청이 서로를 못 보고
    같은 수업이 두 줄 생겼다(오늘 화면에도 두 번). 이제 두 번째 요청은 첫 번째의 commit 을
    기다렸다가 그 행을 보고 409 가 된다.
    """
    title = _normalize_title(body.title)
    days = _validate_days(body.days_of_week)
    start = _parse_hhmm(body.start_time, field="startTime")
    end = _parse_hhmm(body.end_time, field="endTime")
    _validate_time_window(start, end)

    async with user_agent_lock(session, user.id, _LOCK_NAME):
        other = _first_overlap(await repo.list_active(user.id), days=days, start=start, end=end)
        if other is not None:
            raise _overlap_error(other)
        schedule = await repo.create(
            user_id=user.id,
            title=title,
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
    """고정 일정 부분 수정 — 입력된 필드만 갱신.

    요일·시각을 바꿀 때만 겹침을 본다(자기 자신 제외). 제목만 고치는 요청까지 막으면, 겹침
    검사가 생기기 전에 이미 겹쳐 저장된 일정은 이름조차 못 고친다.
    """
    schedule = await repo.get_by_id(user.id, _parse_schedule_id(schedule_id))
    if schedule is None:
        raise _not_found()

    title = _normalize_title(body.title) if body.title is not None else None
    days = _validate_days(body.days_of_week) if body.days_of_week is not None else None
    start = _parse_hhmm(body.start_time, field="startTime") if body.start_time else None
    end = _parse_hhmm(body.end_time, field="endTime") if body.end_time else None
    # 둘 중 하나만 바뀌어도 합성된 window 가 유효해야 함
    new_start = start if start is not None else schedule.start_time
    new_end = end if end is not None else schedule.end_time
    _validate_time_window(new_start, new_end)

    async with user_agent_lock(session, user.id, _LOCK_NAME):
        if days is not None or start is not None or end is not None:
            other = _first_overlap(
                await repo.list_active(user.id),
                days=days if days is not None else list(schedule.days_of_week),
                start=new_start,
                end=new_end,
                exclude_id=schedule.id,
            )
            if other is not None:
                raise _overlap_error(other)
        updated = await repo.update(
            schedule,
            title=title,
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
