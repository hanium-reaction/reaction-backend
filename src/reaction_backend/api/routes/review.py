"""Review — Weekly Review (S21). Issue #21-A.

MVP 룰 기반 (LLM 한 줄 평 P2 — 이슈 #21). 일요일 03:00 cron 이 `period_summaries` 를
precompute 하고, 라우트는 그 행을 읽는다. 아직 없으면 GET 은 즉석 계산해 보여준다(쓰기 X) —
cron 미실행 환경(데모)에서도 빈 화면이 안 나오도록.

집계/영속화 로직은 `scheduler/weekly_review_precompute.py` 단일 소스를 재사용한다.

endpoint:
- GET  /reviews/weekly?weekStart=YYYY-MM-DD  — 주간 리뷰 (precomputed 우선, 없으면 즉석 계산)
- POST /reviews/weekly/generate              — 수동 재생성 + 영속화 (디버그)

S22 habit-penalty · S14/S15 weekly plan 은 #21-B/#21-C 후속.
"""

from __future__ import annotations

from datetime import date, timedelta
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.period_summary import PeriodSummary
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator.weekly_review import WeeklyKpi
from reaction_backend.repositories.review_repo import ReviewRepo, get_review_repo
from reaction_backend.scheduler.weekly_review_precompute import (
    compute_weekly_review,
    run_weekly_review_for_user,
    week_start_of,
)
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.reviews import WeeklyGenerateRequest, WeeklyReviewResponse

router = APIRouter(prefix="/reviews", tags=["reviews"])

ReviewRepoDep = Annotated[ReviewRepo, Depends(get_review_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


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
