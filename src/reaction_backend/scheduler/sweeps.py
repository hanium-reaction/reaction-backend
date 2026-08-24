"""Cron sweep — 전체 활성 사용자 순회 wrapper (Issue #24).

per-user job(`morning_brief` / `weekly_review`)을 모든 활성 사용자에 대해 실행한다.
한 사용자 실패가 배치를 멈추지 않도록 개별 try/except — job 이 idempotent 라 재실행 안전.
세션·repo 는 호출자(런타임 job)가 주입한다 → 테스트는 fake 주입.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from reaction_backend.scheduler.morning_brief import run_morning_brief_for_user
from reaction_backend.scheduler.weekly_review_precompute import (
    run_weekly_review_for_user,
    week_start_of,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from reaction_backend.repositories.action_item_repo import ActionItemRepo
    from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo
    from reaction_backend.repositories.goal_repo import GoalRepo
    from reaction_backend.repositories.review_repo import ReviewRepo
    from reaction_backend.repositories.user_repo import UserRepo

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class SweepResult:
    """sweep 결과 — 관측/로그용."""

    total: int
    ok: int
    failed: int


async def run_morning_brief_sweep(
    now_kst_dt: datetime,
    *,
    user_repo: UserRepo,
    action_repo: ActionItemRepo,
    brief_repo: DailyBriefRepo,
    session: AsyncSession,
    goal_repo: GoalRepo | None = None,
) -> SweepResult:
    """매일 06:00 — 활성 사용자별 Morning Brief 생성(idempotent). 사용자 톤 반영."""
    users = await user_repo.list_active()
    ok = failed = 0
    for user in users:
        try:
            await run_morning_brief_for_user(
                user.id,
                now_kst_dt,
                action_repo=action_repo,
                brief_repo=brief_repo,
                session=session,
                goal_repo=goal_repo,
                tone_mode=user.tone_mode,
            )
            ok += 1
        except Exception:  # noqa: BLE001 — 한 사용자 실패가 배치를 멈추지 않게
            failed += 1
            _log.exception("morning_brief sweep failed for user %s", user.id)
    await session.commit()
    return SweepResult(total=len(users), ok=ok, failed=failed)


async def run_weekly_review_sweep(
    now_kst_dt: datetime,
    *,
    user_repo: UserRepo,
    review_repo: ReviewRepo,
    session: AsyncSession,
) -> SweepResult:
    """일요일 18~23시 30분 폴 — 활성 사용자별 주간 리뷰 precompute(idempotent).

    일요일이 아니면 즉시 no-op(쿼리 없이 반환) — 이 함수가 계산하는 주(`week_start_of`
    (오늘))는 아직 진행 중인 주라, 월~토에 돌면 그 주의 일부만 보고 `force=False` idempotent
    skip 으로 영구히 잠긴 스냅샷을 만들게 된다. 트리거를 일요일로 좁혀도(`scheduler/
    runtime.py`) 여기서 한 번 더 막는다 — 수동 호출·설정 드리프트에 대한 방어(ADR-0008 §8 "E").
    """
    if now_kst_dt.weekday() != 6:  # 월=0 ... 일=6(일요일)
        return SweepResult(total=0, ok=0, failed=0)
    week_start = week_start_of(now_kst_dt.date())
    users = await user_repo.list_active()
    ok = failed = 0
    for user in users:
        try:
            await run_weekly_review_for_user(user.id, week_start, now_kst_dt, repo=review_repo)
            ok += 1
        except Exception:  # noqa: BLE001
            failed += 1
            _log.exception("weekly_review sweep failed for user %s", user.id)
    await session.commit()
    return SweepResult(total=len(users), ok=ok, failed=failed)
