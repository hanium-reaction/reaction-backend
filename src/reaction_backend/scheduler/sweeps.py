"""Cron sweep — 전체 활성 사용자 순회 wrapper (Issue #24).

per-user job(`morning_brief` / `weekly_review`)을 모든 활성 사용자에 대해 실행한다.
한 사용자 실패가 배치를 멈추지 않도록 개별 try/except — job 이 idempotent 라 재실행 안전.
세션·repo 는 호출자(런타임 job)가 주입한다 → 테스트는 fake 주입.

트랜잭션 규약은 `notify_sweeps.py` · `habit_instances.py` 와 같다 — **사용자 단위 commit +
except 에서 rollback**. 예전엔 배치 말미에 한 번만 commit 하고 except 에서 rollback 도 안 했다.
그러면 한 사용자의 DB 예외(겹친 실행의 unique 위반, 끊긴 연결 등)로 세션이 aborted 로 남아
뒤따르는 사용자 전원이 `PendingRollbackError` 로 죽고, 마지막 commit 까지 터져 **이미 만든
다른 사용자들의 브리프까지 전부 사라졌다** — 브리프는 이 job 말고는 만드는 곳이 없다.

순회는 ORM 객체가 아니라 **미리 떠 둔 원시값**으로 한다. rollback 은 세션이 들고 있던 ORM
객체를 전부 만료(expire)시키는데, 그 뒤 `user.id` 를 읽으면 비동기 세션은 lazy refresh 를 못
해 `MissingGreenlet` 로 죽는다 — except 블록의 로그가 같은 속성을 다시 읽다가 루프 밖으로
튀어 격리 자체가 무너진다(fake 세션 테스트로는 안 보이던 구멍, 실 DB 테스트로 고정).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from reaction_backend.scheduler.morning_brief import run_morning_brief_for_user
from reaction_backend.scheduler.weekly_review_precompute import (
    is_final_summary,
    latest_final_week_start,
    run_weekly_review_for_user,
    week_start_of,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from reaction_backend.repositories.action_item_repo import ActionItemRepo
    from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo
    from reaction_backend.repositories.execution_repo import ExecutionRepo
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
    execution_repo: ExecutionRepo | None = None,
) -> SweepResult:
    """매일 06~10시 폴 — 활성 사용자별 Morning Brief 생성(idempotent). 사용자 톤 반영.

    이미 오늘 브리프가 있는 사용자는 job 이 즉시 건너뛴다(`get_by_date`) — 그래서 같은 날 여러
    번 돌아도 안전하고, 앞선 폴에서 실패했거나 놓친 사용자만 다음 폴이 채운다.
    `execution_repo` 가 있으면 캘린더를 연결한 사용자의 오늘 블록 × 캘린더 겹침을 브리프에 싣는다.
    """
    users = await user_repo.list_active()
    targets = [(u.id, u.tone_mode) for u in users]  # rollback 뒤에도 읽을 수 있게 원시값으로
    ok = failed = 0
    for user_id, tone_mode in targets:
        try:
            await run_morning_brief_for_user(
                user_id,
                now_kst_dt,
                action_repo=action_repo,
                brief_repo=brief_repo,
                session=session,
                goal_repo=goal_repo,
                tone_mode=tone_mode,
                execution_repo=execution_repo,
            )
            # 사용자 단위 commit — 뒤 사용자의 실패가 앞 사용자의 브리프를 되돌리지 않게.
            await session.commit()
            ok += 1
        except Exception:  # noqa: BLE001 — 한 사용자 실패가 배치를 멈추지 않게
            failed += 1
            _log.exception("morning_brief sweep failed for user %s", user_id)
            await session.rollback()  # aborted 세션이 다음 사용자를 전멸시키지 않게
    return SweepResult(total=len(targets), ok=ok, failed=failed)


async def run_weekly_review_sweep(
    now_kst_dt: datetime,
    *,
    user_repo: UserRepo,
    review_repo: ReviewRepo,
    session: AsyncSession,
) -> SweepResult:
    """일요일 18~23시 30분 폴 — 활성 사용자별 이번 주 리뷰를 매 폴 다시 집계(idempotent).

    `force=True` — 예전엔 `force=False` 라 18:00 첫 폴의 스냅샷이 그 주 내내 잠겨, 21:00 회고
    알림을 받고 체크인한 결과가 리포트에 영영 안 들어갔다. 룰 기반 결정적 upsert 라(LLM 없음)
    매 폴 덮어써도 같은 입력이면 같은 행이다. 이 행은 아직 **확정본이 아니다** — 늦은 회고까지
    담는 확정은 `run_weekly_review_finalize_sweep` 이 한다.

    일요일이 아니면 즉시 no-op(쿼리 없이 반환) — 이 함수가 계산하는 주(`week_start_of`
    (오늘))는 아직 진행 중인 주라, 월~토에 돌면 그 주의 일부만 본 스냅샷이 남는다.
    트리거를 일요일로 좁혀도(`scheduler/runtime.py`) 여기서 한 번 더 막는다 — 수동 호출·설정
    드리프트에 대한 방어(ADR-0008 §8 "E").
    """
    if now_kst_dt.weekday() != 6:  # 월=0 ... 일=6(일요일)
        return SweepResult(total=0, ok=0, failed=0)
    week_start = week_start_of(now_kst_dt.date())
    users = await user_repo.list_active()
    user_ids = [u.id for u in users]  # rollback 뒤에도 읽을 수 있게 원시값으로
    ok = failed = 0
    for user_id in user_ids:
        try:
            await run_weekly_review_for_user(
                user_id, week_start, now_kst_dt, repo=review_repo, force=True
            )
            await session.commit()  # 사용자 단위 commit — 모듈 docstring
            ok += 1
        except Exception:  # noqa: BLE001
            failed += 1
            _log.exception("weekly_review sweep failed for user %s", user_id)
            await session.rollback()
    return SweepResult(total=len(user_ids), ok=ok, failed=failed)


async def run_weekly_review_finalize_sweep(
    now_kst_dt: datetime,
    *,
    user_repo: UserRepo,
    review_repo: ReviewRepo,
    session: AsyncSession,
) -> SweepResult:
    """매일 04:30 — 회고 창까지 닫힌 가장 최근 주를 확정 집계로 덮는다(idempotent).

    일요일 저녁 폴은 그 주의 일요일 밤까지만 본다. 그 뒤 월·화에 지난 카드를 회고하면 실행
    상태가 바뀌는데, 이 확정 집계가 없으면 `period_summaries`(정책 제안의 입력, `GET`
    의 과거 주 저장본)에는 그 결과가 영영 안 들어간다.

    대상 주는 `latest_final_week_start(now)` — 목요일 04:30 에 지난주가 처음 확정되고, 그 뒤
    며칠은 이미 확정본이 있어 사용자당 조회 1번으로 끝난다. 매일 도는 이유는 `habit_instances`
    와 같다(MemoryJobStore 라 주 1회 job 은 재기동 한 번에 그 주가 통째로 빠진다) — 목요일을
    놓쳐도 다음 날 자가치유한다.
    """
    week_start = latest_final_week_start(now_kst_dt)
    users = await user_repo.list_active()
    user_ids = [u.id for u in users]  # rollback 뒤에도 읽을 수 있게 원시값으로
    ok = failed = 0
    for user_id in user_ids:
        try:
            existing = await review_repo.get_weekly(user_id, week_start)
            if existing is None or not is_final_summary(existing, week_start):
                await run_weekly_review_for_user(
                    user_id, week_start, now_kst_dt, repo=review_repo, force=True
                )
                await session.commit()  # 사용자 단위 commit — 모듈 docstring
            ok += 1
        except Exception:  # noqa: BLE001
            failed += 1
            _log.exception("weekly_review finalize failed for user %s", user_id)
            await session.rollback()
    return SweepResult(total=len(user_ids), ok=ok, failed=failed)
