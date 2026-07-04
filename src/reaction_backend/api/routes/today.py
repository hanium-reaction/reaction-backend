"""Today / Execution — S10~S13 (api-contract §10).

Issue #19-A: 조회 — `GET /today/agenda` + `GET /today/actions/{id}`.
Issue #19-B: 실행 쓰기 — `POST /today/actions/{id}/start` + `POST /today/check-ins`.
  - scheduled_block 이 없으면 즉석(ad-hoc) 블록을 생성해 NOT NULL 의존을 해소
    (source='user_edit', §5.10). First Plan(#32) 블록이 있으면 그것을 사용.
  - 체크인 시 `action_item.status` 전이 — execution 레이어의 책임 (ActionItemRepo 합의).
  - pause/resume(interruption_events)은 #19-B-2 후속.

agenda 데이터 출처: daily_briefs(Morning Brief, #19-C cron 이 채움) + action_items(오늘 target_date)
+ habit_instances(이번 주) + fixed_schedules(오늘 요일). 모두 조회 — 쓰기 없음.
"""

from __future__ import annotations

from datetime import date
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.db.session import get_db
from reaction_backend.repositories.action_item_repo import ActionItemRepo, get_action_item_repo
from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo, get_daily_brief_repo
from reaction_backend.repositories.execution_repo import ExecutionRepo, get_execution_repo
from reaction_backend.repositories.fixed_schedule_repo import (
    FixedScheduleRepo,
    get_fixed_schedule_repo,
)
from reaction_backend.repositories.habit_instance_repo import (
    HabitInstanceRepo,
    get_habit_instance_repo,
)
from reaction_backend.repositories.habit_repo import current_week_start_kst
from reaction_backend.safety.encryption import encrypt_memo
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.today import (
    ActionDetail,
    AgendaCard,
    AgendaFixedSchedule,
    AgendaHabit,
    CheckInRequest,
    CheckInResponse,
    ExecutionEventResponse,
    ExecutionStartResponse,
    MorningBrief,
    TodayAgenda,
)

router = APIRouter(prefix="/today", tags=["today"])

_ACTION_PREFIX = "action_"
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _today_kst() -> date:
    return now_kst().date()


def _parse_action_id(action_id: str) -> UUID:
    if not action_id.startswith(_ACTION_PREFIX):
        raise _action_not_found()
    try:
        return UUID(action_id[len(_ACTION_PREFIX) :])
    except ValueError as e:
        raise _action_not_found() from e


def _action_not_found() -> ApiError:
    return ApiError(
        ErrorCode.COMMON_NOT_FOUND,
        "해당 카드를 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _brief_schema(brief: DailyBrief | None) -> MorningBrief | None:
    if brief is None:
        return None
    hints = [
        str(h.get("text", h)) if isinstance(h, dict) else str(h) for h in brief.adjustment_hints
    ]
    return MorningBrief(
        headline=brief.headline_text,
        big_rock_action_id=(
            f"{_ACTION_PREFIX}{brief.big_rock_action_item_id}"
            if brief.big_rock_action_item_id is not None
            else None
        ),
        adjustment_hints=hints,
        fallback_used=brief.fallback_used,
    )


def _card_schema(a: ActionItem) -> AgendaCard:
    return AgendaCard(
        action_id=f"{_ACTION_PREFIX}{a.id}",
        title=a.title,
        category=a.category,
        status=a.status,
        priority=a.priority,
        estimated_minutes=a.estimated_minutes,
        source=a.source,
        why_now=a.why_now,
        first_step=a.first_step,
    )


def _habit_schema(i: HabitInstance) -> AgendaHabit:
    return AgendaHabit(
        instance_id=f"hinst_{i.id}",
        habit_id=f"habit_{i.habit_id}",
        title="",  # 제목은 habit 본체 — #19-A 는 진행 카운트만. FE 가 /habits 와 join (또는 후속 확장)
        target_count=i.target_count,
        done_count=i.done_count,
    )


def _fixed_schema(s: FixedSchedule) -> AgendaFixedSchedule:
    return AgendaFixedSchedule(
        schedule_id=f"fixed_{s.id}",
        title=s.title,
        start_time=s.start_time.strftime("%H:%M"),
        end_time=s.end_time.strftime("%H:%M"),
    )


ActionRepoDep = Annotated[ActionItemRepo, Depends(get_action_item_repo)]
ExecutionRepoDep = Annotated[ExecutionRepo, Depends(get_execution_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]
BriefRepoDep = Annotated[DailyBriefRepo, Depends(get_daily_brief_repo)]
HabitInstRepoDep = Annotated[HabitInstanceRepo, Depends(get_habit_instance_repo)]
FixedRepoDep = Annotated[FixedScheduleRepo, Depends(get_fixed_schedule_repo)]


@router.get("/agenda")
async def today_agenda(
    user: CurrentUser,
    action_repo: ActionRepoDep,
    brief_repo: BriefRepoDep,
    habit_inst_repo: HabitInstRepoDep,
    fixed_repo: FixedRepoDep,
) -> TodayAgenda:
    """오늘 어젠다 단일 조회 — daily_brief + cards + habits + fixed (모두 read)."""
    today = _today_kst()
    weekday = _WEEKDAY_KEYS[today.weekday()]

    brief = await brief_repo.get_by_date(user.id, today)
    cards = await action_repo.list_by_date(user.id, today)
    habit_instances = await habit_inst_repo.list_for_user_week(user.id, current_week_start_kst())
    fixed = await fixed_repo.list_active(user.id)
    todays_fixed = [s for s in fixed if weekday in (s.days_of_week or [])]

    return TodayAgenda(
        date=today.isoformat(),
        brief=_brief_schema(brief),
        cards=[_card_schema(a) for a in cards],
        habits=[_habit_schema(i) for i in habit_instances],
        fixed_schedules=[_fixed_schema(s) for s in todays_fixed],
    )


@router.get("/actions/{action_id}")
async def get_action_detail(
    action_id: str, user: CurrentUser, action_repo: ActionRepoDep
) -> ActionDetail:
    """S11 카드 상세."""
    action = await action_repo.get_by_id(user.id, _parse_action_id(action_id))
    if action is None:
        raise _action_not_found()
    return ActionDetail(
        action_id=f"{_ACTION_PREFIX}{action.id}",
        title=action.title,
        category=action.category,
        status=action.status,
        priority=action.priority,
        estimated_minutes=action.estimated_minutes,
        target_date=action.target_date.isoformat(),
        source=action.source,
        why_now=action.why_now,
        first_step=action.first_step,
        goal_id=f"goal_{action.goal_id}" if action.goal_id is not None else None,
    )


_EXEC_PREFIX = "exec_"


def _parse_execution_id(execution_id: str) -> UUID:
    if not execution_id.startswith(_EXEC_PREFIX):
        raise _execution_not_found()
    try:
        return UUID(execution_id[len(_EXEC_PREFIX) :])
    except ValueError as e:
        raise _execution_not_found() from e


def _execution_not_found() -> ApiError:
    return ApiError(
        ErrorCode.TODAY_EXECUTION_NOT_FOUND,
        "해당 실행 기록을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


@router.post("/actions/{action_id}/start", status_code=201)
async def start_action(
    action_id: str,
    user: CurrentUser,
    action_repo: ActionRepoDep,
    execution_repo: ExecutionRepoDep,
    session: SessionDep,
) -> ExecutionStartResponse:
    """[▶ 시작] → execution_events 생성 (#19-B).

    카드의 미종결 scheduled_block 이 있으면 사용, 없으면 즉석 블록 생성
    (source='user_edit'). 같은 카드의 in_progress 실행이 있으면 409.
    """
    action = await action_repo.get_by_id(user.id, _parse_action_id(action_id))
    if action is None:
        raise _action_not_found()

    active = await execution_repo.get_active_for_action(user.id, action.id)
    if active is not None:
        raise ApiError(
            ErrorCode.TODAY_EXECUTION_ALREADY_ACTIVE,
            "이미 진행 중인 실행이 있어요. 먼저 체크인으로 마무리해 주세요.",
            http_status=HTTPStatus.CONFLICT,
        )

    started_at = now_kst()
    block = await execution_repo.find_open_block(user.id, action.id)
    if block is None:
        block = await execution_repo.create_adhoc_block(
            user_id=user.id, action_item=action, start_at=started_at
        )
    else:
        block.block_status = "started"

    execution = await execution_repo.create_execution(
        user_id=user.id,
        action_item_id=action.id,
        block=block,
        started_at=started_at,
    )
    # 실행 시작 → 카드 상태 전이 (execution 레이어 책임)
    action.status = "in_progress"
    await session.commit()

    return ExecutionStartResponse(
        execution_id=f"{_EXEC_PREFIX}{execution.id}",
        action_id=f"{_ACTION_PREFIX}{action.id}",
        completion_status=execution.completion_status,
        actual_start_at=started_at,
    )


@router.post("/check-ins")
async def quick_check_in(
    body: CheckInRequest,
    user: CurrentUser,
    action_repo: ActionRepoDep,
    execution_repo: ExecutionRepoDep,
    session: SessionDep,
) -> CheckInResponse:
    """Quick Check-in 4칩 (S13/S17) — 완료/조금함/못함/더함 (#19-B).

    execution 종결 + 블록 finished + `action_item.status` 전이.
    `needs_failure_tags=True`(failed/partial_done) 면 FE 는 S18 실패 사유로 이동
    → `POST /reflection/failure-tags/{executionId}` → Recovery(§12) 로 이어진다.
    """
    execution = await execution_repo.get_by_id(user.id, _parse_execution_id(body.execution_id))
    if execution is None:
        raise _execution_not_found()
    if execution.completion_status != "in_progress":
        raise ApiError(
            ErrorCode.TODAY_ALREADY_CHECKED_IN,
            "이미 체크인이 끝난 실행이에요.",
            http_status=HTTPStatus.CONFLICT,
        )

    ended_at = now_kst()
    execution.completion_status = body.completion_status
    execution.actual_end_at = ended_at
    if execution.actual_start_at is not None:
        delta = ended_at - execution.actual_start_at
        execution.actual_duration_minutes = max(int(delta.total_seconds() // 60), 0)
    if body.user_rating is not None:
        execution.user_rating = body.user_rating
    if body.user_feedback:
        execution.user_feedback_encrypted = encrypt_memo(body.user_feedback)

    block = await execution_repo.get_block(execution.scheduled_block_id)
    if block is not None:
        block.block_status = "finished"

    # 카드 상태 전이 — 4칩 값은 ACTION_STATUS_VALUES 와 1:1 (done/partial_done/failed/over_done)
    action = await action_repo.get_by_id(user.id, execution.action_item_id)
    if action is not None:
        action.status = body.completion_status

    await session.commit()

    return CheckInResponse(
        execution_id=body.execution_id,
        action_id=f"{_ACTION_PREFIX}{execution.action_item_id}",
        completion_status=execution.completion_status,
        actual_duration_minutes=execution.actual_duration_minutes,
        needs_failure_tags=body.completion_status in ("failed", "partial_done"),
    )


def _execution_event(execution: ExecutionEvent, *, status: str) -> ExecutionEventResponse:
    return ExecutionEventResponse(
        execution_id=f"{_EXEC_PREFIX}{execution.id}",
        action_item_id=f"{_ACTION_PREFIX}{execution.action_item_id}",
        started_at=execution.actual_start_at or execution.plan_start_at,
        ended_at=execution.actual_end_at,
        status=status,
        pause_total_minutes=execution.pause_total_minutes,
    )


def _require_in_progress(execution: ExecutionEvent | None) -> ExecutionEvent:
    if execution is None:
        raise _execution_not_found()
    if execution.completion_status != "in_progress":
        raise ApiError(
            ErrorCode.TODAY_ALREADY_CHECKED_IN,
            "이미 체크인이 끝난 실행이에요.",
            http_status=HTTPStatus.CONFLICT,
        )
    return execution


@router.post("/focus/{execution_id}/pause")
async def pause_focus(
    execution_id: str,
    user: CurrentUser,
    execution_repo: ExecutionRepoDep,
    session: SessionDep,
) -> ExecutionEventResponse:
    """[⏸] 집중 세션 일시정지 (#83) — user_pause interruption 을 연다.

    execution 은 in_progress 유지. 이미 정지 중이면 409. 재개 시 누적 시간이 반영된다.
    """
    execution = _require_in_progress(
        await execution_repo.get_by_id(user.id, _parse_execution_id(execution_id))
    )
    if await execution_repo.get_open_pause(execution.id) is not None:
        raise ApiError(
            ErrorCode.TODAY_ALREADY_PAUSED,
            "이미 일시정지 중이에요.",
            http_status=HTTPStatus.CONFLICT,
        )
    await execution_repo.create_pause(user_id=user.id, execution_id=execution.id)
    await session.commit()
    return _execution_event(execution, status="paused")


@router.post("/focus/{execution_id}/resume")
async def resume_focus(
    execution_id: str,
    user: CurrentUser,
    execution_repo: ExecutionRepoDep,
    session: SessionDep,
) -> ExecutionEventResponse:
    """[▶ 계속] 집중 세션 재개 (#83) — 열린 정지 구간을 닫고 pause_total_minutes 누적.

    정지 중이 아니면 409. 정지 시작(created_at)부터 지금까지를 지연분으로 기록한다.
    """
    execution = _require_in_progress(
        await execution_repo.get_by_id(user.id, _parse_execution_id(execution_id))
    )
    pause = await execution_repo.get_open_pause(execution.id)
    if pause is None:
        raise ApiError(
            ErrorCode.TODAY_NOT_PAUSED,
            "일시정지 상태가 아니에요.",
            http_status=HTTPStatus.CONFLICT,
        )
    paused_minutes = max(int((now_kst() - pause.created_at).total_seconds() // 60), 0)
    pause.resume_delay_minutes = paused_minutes
    pause.resumed_after_interrupt = True
    execution.pause_total_minutes += paused_minutes
    await session.commit()
    return _execution_event(execution, status="in_progress")
