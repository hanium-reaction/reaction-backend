"""Today / Execution — S10~S13 (api-contract §10).

Issue #19-A: **조회만** — `GET /today/agenda` + `GET /today/actions/{id}`.
Focus 실행 로깅(start/pause/resume/check-ins)은 #19-B (`execution_events.scheduled_block_id`
NOT NULL → First Plan #18/#32 의 scheduled_blocks 의존).

agenda 데이터 출처: daily_briefs(Morning Brief, #19-C cron 이 채움) + action_items(오늘 target_date)
+ habit_instances(이번 주) + fixed_schedules(오늘 요일). 모두 조회 — 쓰기 없음.
"""

from __future__ import annotations

from datetime import date
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.repositories.action_item_repo import ActionItemRepo, get_action_item_repo
from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo, get_daily_brief_repo
from reaction_backend.repositories.fixed_schedule_repo import (
    FixedScheduleRepo,
    get_fixed_schedule_repo,
)
from reaction_backend.repositories.habit_instance_repo import (
    HabitInstanceRepo,
    get_habit_instance_repo,
)
from reaction_backend.repositories.habit_repo import current_week_start_kst
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.today import (
    ActionDetail,
    AgendaCard,
    AgendaFixedSchedule,
    AgendaHabit,
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
