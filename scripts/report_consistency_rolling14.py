"""연속성 지표 (`consistency_rolling14`) — 읽기 전용 실측 (근거 대장 §7.1/§7.2, C1/C2).

**C1/C2**: 옛 `consistency_days`(`_longest_streak`, 최장 연속 일수)는 1회 결손을 0으로
만든다 — all-or-nothing 붕괴를 시스템이 직접 제조한다(§7.1). 이 지표는 그 대체다:
**연속성 보너스가 없다** — 하루 쉬어도 다음날 다시 하면 그 하루만큼만 깎인다.

**정의(사용자별)**: 최근 14일 중 `done`/`over_done`/`partial_done` 실행이 하루라도 있는
날의 수 ÷ 14. `failed` 만 있는 날, 아무 실행도 없는 날은 분자에 안 들어간다 — 완전 실패나
무실행이 "그래도 부분 점수"를 받으면 이 지표도 옛 지표와 같은 관대화 함정에 빠진다.

**리포트 집계**: 지표 자체는 사용자별이라, 이 스크립트는 활성 사용자 전체의 분포(평균 ·
중앙값 · 최솟값 · 최댓값)로 보고한다. 가입 14일 미만인 사용자는 분모(14)가 실제 가능한
날수보다 커서 비율이 구조적으로 낮게 나온다 — 제외하지 않고 별도로 몇 명인지만 밝힌다
(무언의 절삭 금지, `report_recovery_followthrough.py` 와 같은 원칙).

**아무것도 쓰지 않는다** — SELECT 뿐. `--apply` 같은 옵션 자체가 없다.

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.report_consistency_rolling14
"""

from __future__ import annotations

import asyncio
import statistics
from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.schemas.common import now_kst, to_kst

if TYPE_CHECKING:
    from datetime import date

_WINDOW_DAYS = 14
_COUNTED_STATUSES = ("done", "over_done", "partial_done")


async def _fetch_active_user_ids(session: AsyncSession) -> set[UUID]:
    stmt = select(User.id).where(
        User.onboarding_state == "ACTIVE", User.is_anonymized.is_(False), User.archived_at.is_(None)
    )
    return set((await session.execute(stmt)).scalars().all())


async def _fetch_qualifying_days(
    session: AsyncSession, *, since: datetime
) -> dict[UUID, set[date]]:
    """user_id → `since`(KST-aware 시각) 이후 done/over_done/partial_done 이 있었던 KST 날짜 집합.

    `since` 는 반드시 tz-aware `datetime` 이어야 한다 — naive `date` 를 넘기면 asyncpg 가
    이를 UTC 자정으로 해석해 KST 와 최대 9시간 어긋난 경계로 비교된다(타임존 없는 값과
    `timestamptz` 컬럼을 비교할 때의 일반적 함정).
    """
    stmt = select(ExecutionEvent.user_id, ExecutionEvent.plan_start_at).where(
        ExecutionEvent.completion_status.in_(_COUNTED_STATUSES),
        ExecutionEvent.plan_start_at >= since,
    )
    out: dict[UUID, set[date]] = defaultdict(set)
    for user_id, plan_start_at in (await session.execute(stmt)).all():
        out[user_id].add(to_kst(plan_start_at).date())
    return out


def _consistency_rate(qualifying_day_count: int, *, window_days: int = _WINDOW_DAYS) -> float:
    return min(qualifying_day_count, window_days) / window_days


async def _preview(session: AsyncSession) -> None:
    now = now_kst()
    # 오늘을 포함한 KST 달력일 14일 — `since` 는 (오늘 - 13일)의 KST 자정.
    window_start_date = now.date() - timedelta(days=_WINDOW_DAYS - 1)
    since = datetime.combine(window_start_date, time.min, tzinfo=now.tzinfo)
    print(
        f"기준 시각: {now.isoformat()} (최근 {_WINDOW_DAYS}일 = {window_start_date} ~ {now.date()})"
    )
    print(
        "분모: 14(고정). 분자: 그 사용자의 최근 14일 중 "
        "done/over_done/partial_done 이 있는 날 수 — 연속성 보너스 없음."
    )
    print()

    active_user_ids = await _fetch_active_user_ids(session)
    if not active_user_ids:
        print("활성 사용자 0명 — 잴 데이터가 없다.")
        return

    qualifying_by_user = await _fetch_qualifying_days(session, since=since)

    rates = [_consistency_rate(len(qualifying_by_user.get(uid, set()))) for uid in active_user_ids]
    no_activity_n = sum(1 for uid in active_user_ids if not qualifying_by_user.get(uid))

    print(f"■ 분모(활성 사용자) = {len(active_user_ids)}명")
    print(f"■ consistency_rolling14 평균 = {statistics.mean(rates):.1%}")
    print(f"■ consistency_rolling14 중앙값 = {statistics.median(rates):.1%}")
    print(f"■ 최솟값 = {min(rates):.1%} / 최댓값 = {max(rates):.1%}")
    if no_activity_n:
        print(
            f"  ⚠️ 최근 {_WINDOW_DAYS}일간 실행 기록이 전혀 없는 사용자 {no_activity_n}명 포함(0%)"
        )
    print()

    print(
        "※ 가입 14일 미만 사용자는 분모(14)가 실제 가능한 날수보다 커서 비율이 구조적으로 "
        "낮게 나온다 — 이 리포트는 가입일을 따로 걸러내지 않는다(신규 가입자 규모가 "
        "커지면 별도 세그먼트 분석이 필요할 수 있다)."
    )
    print()
    print(
        "※ 이 스크립트는 아무것도 쓰지 않았다. 지표 정의는 "
        "docs/experiments/experiment-plan-v1.md §5 M6 을 따른다."
    )


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[report-consistency-rolling14] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
