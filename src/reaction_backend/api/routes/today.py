"""Today / Execution — S10~S13 (api-contract §10).

Issue #19-A: 조회 — `GET /today/agenda` + `GET /today/actions/{id}`.
Issue #19-B: 실행 쓰기 — `POST /today/actions/{id}/start` + `POST /today/check-ins`.
  - scheduled_block 이 없으면 즉석(ad-hoc) 블록을 생성해 NOT NULL 의존을 해소
    (source='user_edit', §5.10). First Plan(#32) 블록이 있으면 그것을 사용.
  - 체크인 시 `action_item.status` 전이 — execution 레이어의 책임 (ActionItemRepo 합의).
  - pause/resume(interruption_events)은 #19-B-2 후속.

agenda 데이터 출처: daily_briefs(Morning Brief, #19-C cron 이 채움) + action_items(오늘 target_date
+ 자정을 넘겨 아직 진행 중인 카드 `carriedOver`) + habit_instances(이번 주) + fixed_schedules(오늘
요일). 모두 조회 — 쓰기 없음.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.habit_instance import HabitInstance
from reaction_backend.db.session import get_db
from reaction_backend.domain import action_cancel, missed_check_in
from reaction_backend.integrations.google_calendar import freebusy
from reaction_backend.repositories.action_item_repo import ActionItemRepo, get_action_item_repo
from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo, get_daily_brief_repo
from reaction_backend.repositories.execution_repo import (
    ExecutionRepo,
    get_execution_repo,
    settle_pause,
)
from reaction_backend.repositories.fixed_schedule_repo import (
    FixedScheduleRepo,
    get_fixed_schedule_repo,
)
from reaction_backend.repositories.habit_instance_repo import (
    HabitInstanceRepo,
    get_habit_instance_repo,
)
from reaction_backend.repositories.habit_repo import current_week_start_kst
from reaction_backend.repositories.recovery_repo import RecoveryRepo, get_recovery_repo
from reaction_backend.safety.encryption import encrypt_memo
from reaction_backend.scheduler.expire_reflections import pending_reflection_since
from reaction_backend.schemas.calendar import CalendarCheck
from reaction_backend.schemas.common import KST, now_kst
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
_EXEC_PREFIX = "exec_"
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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


def _card_schema(
    a: ActionItem,
    *,
    has_execution_history: bool,
    missed: bool,
    execution_id: UUID | None,
    calendar_conflict: bool = False,
    carried_over: bool = False,
) -> AgendaCard:
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
        cancellable=action_cancel.is_cancellable(
            status=a.status,
            source=a.source,
            has_execution_history=has_execution_history,
        ),
        missed_check_in=missed,
        execution_id=f"{_EXEC_PREFIX}{execution_id}" if execution_id else None,
        calendar_conflict=calendar_conflict,
        carried_over=carried_over,
    )


def _habit_schema(i: HabitInstance) -> AgendaHabit:
    return AgendaHabit(
        instance_id=f"hinst_{i.id}",
        habit_id=f"habit_{i.habit_id}",
        title=i.habit.title,
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
RecoveryRepoDep = Annotated[RecoveryRepo, Depends(get_recovery_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]
BriefRepoDep = Annotated[DailyBriefRepo, Depends(get_daily_brief_repo)]
HabitInstRepoDep = Annotated[HabitInstanceRepo, Depends(get_habit_instance_repo)]
FixedRepoDep = Annotated[FixedScheduleRepo, Depends(get_fixed_schedule_repo)]


@router.get("/agenda")
async def today_agenda(
    user: CurrentUser,
    action_repo: ActionRepoDep,
    execution_repo: ExecutionRepoDep,
    brief_repo: BriefRepoDep,
    habit_inst_repo: HabitInstRepoDep,
    fixed_repo: FixedRepoDep,
    session: SessionDep,
) -> TodayAgenda:
    """오늘 어젠다 단일 조회 — daily_brief + cards + habits + fixed (모두 read).

    캘린더를 연결한 사용자는 오늘 구간의 Google 캘린더를 **열 때마다** 확인해, 아직 시작 안 한
    블록이 그 뒤 생긴 약속과 겹치면 카드에 `calendarConflict` 를 단다(5분 캐시·2초 상한 —
    못 읽어도 어젠다는 뜬다, `calendar.status=failed`).
    """
    # '오늘' 과 '지금' 을 한 시각에서 뽑는다 — 따로 읽으면 자정 경계에서 서로 다른 날을 본다.
    now = now_kst()
    today = now.date()
    weekday = _WEEKDAY_KEYS[today.weekday()]
    day_start = datetime.combine(today, time(0, 0), tzinfo=KST)

    brief = await brief_repo.get_by_date(user.id, today)
    cards = await action_repo.list_by_date(user.id, today)
    # 자정을 넘겨도 아직 진행 중인 어제 카드는 오늘 카드 뒤에 이어 붙인다 (today-3).
    # 창 경계는 `/reflection/pending` 과 같은 단일 소스 — 두 화면이 같은 카드를 본다.
    today_ids = {c.id for c in cards}
    carried = [
        a
        for a in await execution_repo.list_carried_over_actions(
            user.id,
            today=today,
            day_start=day_start,
            since=pending_reflection_since(today),
            now=now,
        )
        if a.id not in today_ids
    ]
    carried_ids = {a.id for a in carried}
    cards = [*cards, *carried]
    action_ids = [c.id for c in cards]
    # 카드마다 묻지 않는다 — 한 번에 받아 `cancellable` 판정에 쓴다 (#214).
    with_history = await execution_repo.action_ids_with_history(user.id, action_ids)
    latest_executions = await execution_repo.latest_execution_ids(user.id, action_ids)
    # T1 미체크 배지(근거 대장 §6.2) — 판정은 domain.missed_check_in, 재료만 여기서 배치 조회.
    active_blocks = await execution_repo.list_active_blocks_for_actions(user.id, action_ids)
    missed_ids = {
        action_item_id
        for action_item_id, block_status, start_at, end_at in active_blocks
        if missed_check_in.is_missed_check_in(
            block_status=block_status,
            start_at=start_at,
            now=now,
            # 유예는 블록 길이에 비례한다 — 15분짜리 블록에 20분 유예를 주면 배지가 뜰 때는
            # 이미 블록이 끝나 있다(ADR-0009 D5).
            block_minutes=int((end_at - start_at).total_seconds() // 60),
        )
    }
    habit_instances = await habit_inst_repo.list_for_user_week(user.id, current_week_start_kst())
    fixed = await fixed_repo.list_active(user.id)
    todays_fixed = [s for s in fixed if weekday in (s.days_of_week or [])]

    calendar = await freebusy.screen_conflicts(
        session,
        user_id=user.id,
        start=day_start,
        end=day_start + timedelta(days=1),
        blocks=list(active_blocks),
        now=now,
    )
    # 조회가 토큰을 갱신·회수했으면 확정한다 — freebusy 는 commit 하지 않는다(호출자 몫).
    await session.commit()

    return TodayAgenda(
        date=today.isoformat(),
        brief=_brief_schema(brief),
        cards=[
            _card_schema(
                a,
                has_execution_history=a.id in with_history,
                missed=a.id in missed_ids,
                execution_id=latest_executions.get(a.id),
                calendar_conflict=a.id in calendar.keys,
                carried_over=a.id in carried_ids,
            )
            for a in cards
        ],
        habits=[_habit_schema(i) for i in habit_instances],
        fixed_schedules=[_fixed_schema(s) for s in todays_fixed],
        calendar=CalendarCheck(status=calendar.status, checked_at=calendar.checked_at),
    )


@router.post("/actions/{action_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_action(
    action_id: str,
    user: CurrentUser,
    action_repo: ActionRepoDep,
    execution_repo: ExecutionRepoDep,
    session: SessionDep,
) -> None:
    """카드 취소 = soft delete (#214).

    "이건 처음부터 없던 일" 이라는 의사표시다. 그래서 `archived_at` 만 세팅하고
    **`status` 는 건드리지 않으며**, 지표에서는 분모째로 빠진다(조회가 archived 를
    거른다). 남은 미종결 블록은 repo 가 함께 cancel 한다 — 안 그러면 주간 그리드에
    유령 블록으로 남아 그 시간대를 계속 막는다(data-2). 취소 가능한 카드는 실행 이력이 없으므로 주간 KPI 는 애초에 이 카드를
    join 한 적이 없다 — 지워도 과거 통계가 흔들리지 않는다.

    **보관된 카드를 다시 취소해도 204** 다. FE 는 5초 스낵바 뒤에 호출하므로 재시도가
    실패로 보이면 안 된다(#214 FE 코멘트). 되돌리기(restore)는 만들지 않는다 — 자료의
    걸음에서 다시 담으면 몇 초면 복구된다.
    """
    action = await action_repo.get_by_id_any(user.id, _parse_action_id(action_id))
    if action is None:
        raise _action_not_found()
    if action.archived_at is not None:
        return None  # 이미 취소됨 — 멱등

    has_history = bool(await execution_repo.action_ids_with_history(user.id, [action.id]))
    reason = action_cancel.rejection_reason(
        status=action.status,
        source=action.source,
        has_execution_history=has_history,
    )
    if reason is not None:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            reason,
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="actionId",
        )

    await action_repo.cancel(action)
    await session.commit()
    return None


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
    response: Response,
) -> ExecutionStartResponse:
    """[▶ 시작] → execution_events 생성 (#19-B).

    카드의 미종결 scheduled_block 이 있으면 사용, 없으면 즉석 블록 생성
    (source='user_edit').

    **같은 카드가 이미 진행 중이면 그 실행을 200 으로 돌려준다**(새로 만들지 않는다).
    예전엔 409 였는데, FE 는 실행 id 를 sessionStorage 에만 들고 있어서 앱이 백그라운드에서
    죽거나 탭을 닫으면 그게 비고, [이어서 하기] 가 start 를 다시 부른다 — 그러면 409 가
    끝없이 반복되고 [완료] 가 영영 막혔다. 끝낸 일을 '일부만/잘 안됐어요' 로만 남길 수
    있었다(today-1). 응답 모양은 같다 — `actualStartAt` 은 **처음 시작한 시각**이라 FE 가
    타이머를 그 시각부터 이어 붙일 수 있다.

    카드는 **행 잠금으로 읽는다**(#368). 계획 교체·목표 완료가 같은 카드를 보관하는
    중이면 여기서 기다렸다가 `archived_at IS NULL` 재평가에 걸려 404 로 끝난다 —
    execution_events·scheduled_block 을 만들기 **전에** 걸러야 한다. 상태만 조건부로
    막으면 실행 행이 남고, `list_pending_reflection` 은 `action_items` 에 join 하지
    않으므로 그 실행이 회고 화면까지 새어 나간다. 같은 잠금 덕에 [시작] 연타도 직렬화돼
    두 번째 요청은 첫 요청이 만든 실행을 돌려받는다(실행이 두 개 생기지 않는다).
    """
    action = await action_repo.get_by_id_for_update(user.id, _parse_action_id(action_id))
    if action is None:
        raise _action_not_found()

    active = await execution_repo.get_active_for_action(user.id, action.id)
    if active is not None:
        # 멱등 — 카드 상태도 블록도 건드리지 않는다(이미 시작 때 전이됐다).
        await session.commit()  # 행 잠금을 바로 놓는다
        response.status_code = HTTPStatus.OK
        return ExecutionStartResponse(
            execution_id=f"{_EXEC_PREFIX}{active.id}",
            action_id=f"{_ACTION_PREFIX}{action.id}",
            completion_status=active.completion_status,
            actual_start_at=active.actual_start_at or active.plan_start_at,
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
    recovery_repo: RecoveryRepoDep,
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
    # execution 종결 + 블록 finished — `/reflection/batch` 와 같은 전이 한 벌 (today-13).
    await execution_repo.close_execution(
        execution, status=body.completion_status, ended_at=ended_at
    )
    if body.user_rating is not None:
        execution.user_rating = body.user_rating
    if body.user_feedback:
        execution.user_feedback_encrypted = encrypt_memo(body.user_feedback)

    # 카드 상태 전이 — 4칩 값은 ACTION_STATUS_VALUES 와 1:1 (done/partial_done/failed/over_done)
    action = await action_repo.get_by_id(user.id, execution.action_item_id)
    if action is not None:
        action.status = body.completion_status

    # 이 카드가 회복 카드(resulting_action_item)면 그 RecoveryAttempt 에 완료 스탬프
    # (average_recovery_minutes 생산자, #20). 회복이 아니면 no-op.
    await recovery_repo.complete_for_action(
        user.id,
        execution.action_item_id,
        completed_at=ended_at,
        completion_status=body.completion_status,
    )

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

    execution 은 in_progress 유지. 재개 시 누적 시간이 반영된다.

    **이미 정지 중이면 새 구간을 열지 않고 200 `paused`** 다(예전 409 `TODAY_ALREADY_PAUSED`).
    정지는 서버에 들어갔는데 응답만 잃은 FE 가 다시 보내면 409 가 끝없이 반복돼 '저장되지
    않았어요' 배너가 사라지지 않았다(today-5). 결과 상태가 같으니 멱등이 맞다.
    """
    execution = _require_in_progress(
        await execution_repo.get_by_id(user.id, _parse_execution_id(execution_id))
    )
    if await execution_repo.get_open_pause(execution.id) is None:
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

    정지 시작(created_at)부터 지금까지를 지연분으로 기록한다. 6h cron 이 '6시간 안에 안
    돌아옴' 으로 표시한 정지도 아직 열린 정지다 — 아침에 멈추고 저녁에 돌아와 [계속] 을
    눌러도 재개되고, 그 시간이 정지 시간에 들어간다(sched-14).

    **정지 중이 아니면 아무것도 바꾸지 않고 200 `in_progress`** 다(예전 409
    `TODAY_NOT_PAUSED`). 재개 응답을 잃은 FE 의 재시도가 영영 실패하지 않게(today-5).
    """
    execution = _require_in_progress(
        await execution_repo.get_by_id(user.id, _parse_execution_id(execution_id))
    )
    pause = await execution_repo.get_open_pause(execution.id)
    if pause is not None:
        settle_pause(execution, pause, now=now_kst(), resumed=True)
        await session.commit()
    return _execution_event(execution, status="in_progress")
