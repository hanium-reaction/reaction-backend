"""Review — Weekly Review (S21). Issue #21-A.

MVP 룰 기반 (LLM 한 줄 평 P2 — 이슈 #21). 일요일 03:00 cron 이 `period_summaries` 를
precompute 하고, 라우트는 그 행을 읽는다. 아직 없으면 GET 은 즉석 계산해 보여준다(쓰기 X) —
cron 미실행 환경(데모)에서도 빈 화면이 안 나오도록.

집계/영속화 로직은 `scheduler/weekly_review_precompute.py` 단일 소스를 재사용한다.

endpoint:
- GET  /reviews/weekly?weekStart=YYYY-MM-DD       — 주간 리뷰 (precomputed 우선, 없으면 즉석 계산)
- POST /reviews/weekly/generate                   — 수동 재생성 + 영속화 (디버그)
- GET  /reviews/habit-penalty                     — 3주 미달 빈도 재설계 후보 (S22, #21-C)
- POST /reviews/habit-penalty/{habitId}/accept    — 빈도 다운 수락 (Idempotency-Key, #21-C)
"""

from __future__ import annotations

from datetime import date, timedelta
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.habit import Habit
from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator.habit_penalty import PenaltyEval, evaluate_penalty
from reaction_backend.orchestrator.weekly_review import WeeklyKpi
from reaction_backend.repositories.habit_instance_repo import (
    HabitInstanceRepo,
    get_habit_instance_repo,
)
from reaction_backend.repositories.habit_repo import (
    HabitRepo,
    current_week_start_kst,
    get_habit_repo,
)
from reaction_backend.repositories.review_repo import ReviewRepo, get_review_repo
from reaction_backend.scheduler.weekly_review_precompute import (
    compute_weekly_review,
    run_weekly_review_for_user,
    week_start_of,
)
from reaction_backend.schemas.common import now_kst, to_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.reviews import (
    HabitPenaltyAcceptResponse,
    HabitPenaltyCandidate,
    HabitPenaltyListResponse,
    HabitWeekStat,
    WeeklyGenerateRequest,
    WeeklyReviewResponse,
)

router = APIRouter(prefix="/reviews", tags=["reviews"])

ReviewRepoDep = Annotated[ReviewRepo, Depends(get_review_repo)]
HabitRepoDep = Annotated[HabitRepo, Depends(get_habit_repo)]
HabitInstRepoDep = Annotated[HabitInstanceRepo, Depends(get_habit_instance_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]

_HABIT_PREFIX = "habit_"


def _parse_week_start(raw: str | None) -> date:
    """weekStart 파싱 → 해당 주 월요일. None 이면 이번 주. 형식 오류 422."""
    if raw is None:
        return week_start_of(now_kst().date())
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as e:
        raise ApiError(
            ErrorCode.REVIEW_INVALID_WEEK,
            "weekStart 는 YYYY-MM-DD 형식이어야 해요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="weekStart",
        ) from e
    return week_start_of(parsed)


def _from_summary(summary: PeriodSummary) -> WeeklyReviewResponse:
    """precomputed PeriodSummary → 응답 (Numeric→float)."""
    return WeeklyReviewResponse(
        week_start=summary.start_date,
        week_end=summary.end_date,
        adherence_rate=_f(summary.adherence_rate),
        consistency_days=summary.consistency_days,
        resilience_rate=_f(summary.resilience_rate),
        avg_delay_minutes=_f(summary.avg_delay_minutes),
        restart_success_rate=_f(summary.restart_success_rate),
        repeated_failure_count=summary.repeated_failure_count,
        average_recovery_minutes=_f(summary.average_recovery_minutes),
        category_success_rate={k: float(v) for k, v in summary.category_success_rate.items()},
        peak_window=summary.peak_point_window,
        drain_window=summary.drain_point_window,
        one_liner=summary.llm_one_liner,
        policy_update_candidates=summary.policy_update_candidates,
        generated_at=summary.generated_at,
    )


def _from_kpi(week_start: date, kpi: WeeklyKpi) -> WeeklyReviewResponse:
    """즉석 계산한 KPI → 응답 (영속화 전, generated_at=now)."""
    return WeeklyReviewResponse(
        week_start=week_start,
        week_end=week_start + timedelta(days=6),
        adherence_rate=kpi.adherence_rate,
        consistency_days=kpi.consistency_days,
        resilience_rate=kpi.resilience_rate,
        avg_delay_minutes=kpi.avg_delay_minutes,
        restart_success_rate=kpi.restart_success_rate,
        repeated_failure_count=kpi.repeated_failure_count,
        average_recovery_minutes=kpi.average_recovery_minutes,
        category_success_rate=kpi.category_success_rate,
        peak_window=kpi.peak_point_window,
        drain_window=kpi.drain_point_window,
        one_liner=kpi.one_liner,
        policy_update_candidates=kpi.policy_update_candidates,
        generated_at=now_kst(),
    )


def _f(value: object | None) -> float | None:
    """Numeric(Decimal) → float. None 보존."""
    return None if value is None else float(value)  # type: ignore[arg-type]


@router.get("/weekly")
async def get_weekly_review(
    user: CurrentUser,
    repo: ReviewRepoDep,
    week_start: Annotated[str | None, Query(alias="weekStart")] = None,
) -> WeeklyReviewResponse:
    """이번 주(또는 지정 주차) 리뷰. precomputed 우선, 없으면 즉석 계산(쓰기 없음)."""
    monday = _parse_week_start(week_start)
    existing = await repo.get_weekly(user.id, monday)
    if existing is not None:
        return _from_summary(existing)
    kpi = await compute_weekly_review(user.id, monday, repo=repo)
    return _from_kpi(monday, kpi)


@router.post("/weekly/generate")
async def generate_weekly_review(
    body: WeeklyGenerateRequest,
    user: CurrentUser,
    repo: ReviewRepoDep,
    session: SessionDep,
) -> WeeklyReviewResponse:
    """주간 리뷰 강제 재생성 + 영속화 (디버그/관리자). 같은 주 덮어쓰기."""
    monday = _parse_week_start(body.week_start)
    summary = await run_weekly_review_for_user(user.id, monday, now_kst(), repo=repo, force=True)
    await session.commit()
    return _from_summary(summary)


# ───────────────────────── S22 Habit Penalty (#21-C) ─────────────────────────


def _parse_habit_id(raw: str) -> UUID:
    if not raw.startswith(_HABIT_PREFIX):
        raise _habit_not_found()
    try:
        return UUID(raw[len(_HABIT_PREFIX) :])
    except ValueError as e:
        raise _habit_not_found() from e


def _habit_not_found() -> ApiError:
    return ApiError(
        ErrorCode.HABIT_NOT_FOUND,
        "해당 습관을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _last_completed_monday() -> date:
    """직전에 끝난 주의 월요일 — 진행 중인 이번 주는 제외하고 감지."""
    return current_week_start_kst() - timedelta(days=7)


def _already_decided(habit: Habit, reference_week: date) -> bool:
    """이번 사이클(직전 완료 주)에 이미 페널티를 평가했으면 재제안하지 않는다."""
    decided_at = habit.last_penalty_evaluated_at
    return decided_at is not None and to_kst(decided_at).date() >= reference_week


def _candidate_message(target: int, avg_done: float, suggested: int) -> str:
    """비난 없는 재설계 톤 (베이스라인 §1.4)."""
    return (
        f"지난 3주 동안 주 {target}회 목표 중 평균 {avg_done:g}회를 했어요. "
        f"무리하지 않게 주 {suggested}회로 맞춰볼까요?"
    )


def _to_candidate(habit: Habit, ev: PenaltyEval) -> HabitPenaltyCandidate:
    target = ev.recent[-1][1] if ev.recent else habit.target_count
    return HabitPenaltyCandidate(
        habit_id=f"{_HABIT_PREFIX}{habit.id}",
        title=habit.title,
        current_frequency=habit.frequency_per_week,
        suggested_frequency=ev.suggested_frequency,
        recent_weeks=[HabitWeekStat(done_count=d, target_count=t) for d, t in ev.recent],
        message=_candidate_message(target, ev.avg_done, ev.suggested_frequency),
    )


@router.get("/habit-penalty")
async def list_habit_penalty(
    user: CurrentUser,
    habit_repo: HabitRepoDep,
    habit_inst_repo: HabitInstRepoDep,
) -> HabitPenaltyListResponse:
    """3주 연속 미달(50% 미만) habit 의 빈도 재설계 제안 후보 (S22)."""
    reference = _last_completed_monday()
    candidates: list[HabitPenaltyCandidate] = []
    for habit in await habit_repo.list_active(user.id):
        if _already_decided(habit, reference):
            continue
        instances = await habit_inst_repo.list_recent_for_habit(habit.id, reference, 3)
        ev = evaluate_penalty(instances, habit.frequency_per_week)
        if ev is not None:
            candidates.append(_to_candidate(habit, ev))
    return HabitPenaltyListResponse(candidates=candidates)


@router.post("/habit-penalty/{habit_id}/accept")
async def accept_habit_penalty(
    habit_id: str,
    user: CurrentUser,
    habit_repo: HabitRepoDep,
    habit_inst_repo: HabitInstRepoDep,
    session: SessionDep,
) -> HabitPenaltyAcceptResponse:
    """빈도 재설계 수락 → frequency 다운 (Idempotency-Key 필수 — §1.7 미들웨어)."""
    habit = await habit_repo.get_by_id(user.id, _parse_habit_id(habit_id))
    if habit is None:
        raise _habit_not_found()

    reference = _last_completed_monday()
    instances = await habit_inst_repo.list_recent_for_habit(habit.id, reference, 3)
    ev = evaluate_penalty(instances, habit.frequency_per_week)
    if ev is None or _already_decided(habit, reference):
        raise ApiError(
            ErrorCode.HABIT_PENALTY_NOT_ELIGIBLE,
            "지금은 빈도 재설계를 제안할 조건이 아니에요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    previous = habit.frequency_per_week
    await habit_repo.apply_penalty(
        habit, new_frequency=ev.suggested_frequency, decided_at=now_kst()
    )
    await session.commit()

    return HabitPenaltyAcceptResponse(
        habit_id=f"{_HABIT_PREFIX}{habit.id}",
        previous_frequency=previous,
        new_frequency=ev.suggested_frequency,
        message=f"주 {previous}회에서 {ev.suggested_frequency}회로 조정했어요. 이 리듬으로 가봐요.",
    )
