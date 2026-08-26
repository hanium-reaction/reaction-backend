"""실패 다음날 복귀율 (`next_day_return_rate`) — 읽기 전용 실측 (근거 대장 §7.2/§7.3 SQL#3).

**C3**(Sharif & Shu): 실패 다음날 복귀율 0.37/0.44/0.55 — 이 지표가 그 문헌과 **직접 대조
가능한 유일한 외부 벤치마크**(§7.2)다.

**정의**: 실패(`completion_status='failed'`)가 1건 이상 있었던 (사용자, KST 날짜) 조합 중,
바로 다음 KST 날짜에 그 **같은 사용자**의 `done`/`over_done` 실행이 1건 이상 있는 비율.

근거 대장 §7.3 SQL#3 은 `:user_id` 로 한 사용자만 잰다 — 이 리포트는 다른 report_*.py
와 같은 관례(전 사용자 풀링)로 일반화한다. 날짜 쌍이 아니라 **(사용자, 날짜) 쌍**으로
집합을 잡는 이유: "다음날"은 사용자마다 독립된 KST 달력일이라, 사용자를 안 붙이면
A 의 실패일 바로 다음날 B 가 뭘 했는지를 세는 식으로 새어 들어간다.

**주 정의 / 민감도** (§7.2 원문 그대로): 주 정의는 `failed` 만 분모로 잡는다(벤치마크
비교 가능성 — Sharif & Shu 도 명시적 실패만 본다). `partial_done` 을 분모에 더하는
민감도 버전도 같이 출력한다 — 얼마나 갈리는지 자체가 "부분 완료를 실패로 볼지"의
민감도 정보다.

**아무것도 쓰지 않는다** — SELECT 뿐. `--apply` 같은 옵션 자체가 없다.

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.report_next_day_return
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.schemas.common import now_kst, to_kst

if TYPE_CHECKING:
    from datetime import date

_ONE_DAY = timedelta(days=1)


async def _fetch_days_by_status(
    session: AsyncSession, statuses: tuple[str, ...]
) -> set[tuple[UUID, date]]:
    """`completion_status IN statuses` 인 실행들의 (user_id, KST 날짜) 집합.

    같은 (사용자, 날짜)에 여러 실행이 있어도 집합이라 중복 없이 한 번만 잡힌다 —
    "그 날 그 사용자에게 그 상태가 있었는가"만 필요하고 몇 건인지는 이 지표와 무관.
    """
    stmt = select(ExecutionEvent.user_id, ExecutionEvent.plan_start_at).where(
        ExecutionEvent.completion_status.in_(statuses)
    )
    rows = (await session.execute(stmt)).all()
    return {(user_id, to_kst(plan_start_at).date()) for user_id, plan_start_at in rows}


def _is_next_day_return(user_id: UUID, fail_day: date, win_days: set[tuple[UUID, date]]) -> bool:
    """이 사용자의 실패일 바로 다음 KST 날짜에 그 사용자의 승리일이 있는가."""
    return (user_id, fail_day + _ONE_DAY) in win_days


def _rate(
    fail_days: set[tuple[UUID, date]], win_days: set[tuple[UUID, date]]
) -> tuple[int, int, float]:
    total = len(fail_days)
    hits = sum(1 for user_id, d in fail_days if _is_next_day_return(user_id, d, win_days))
    return hits, total, (hits / total if total else 0.0)


async def _preview(session: AsyncSession) -> None:
    print(f"기준 시각: {now_kst().isoformat()}")
    print(
        "분모: 실패(failed)가 1건 이상 있었던 (사용자, KST 날짜). "
        "분자: 그 사용자의 바로 다음 KST 날짜에 done/over_done 실행이 1건 이상."
    )
    print()

    fail_days_primary = await _fetch_days_by_status(session, ("failed",))
    fail_days_sensitivity = fail_days_primary | await _fetch_days_by_status(
        session, ("partial_done",)
    )
    win_days = await _fetch_days_by_status(session, ("done", "over_done"))

    if not fail_days_primary:
        print("실패 실행 0건 — 잴 데이터가 없다.")
        return

    hits, total, rate = _rate(fail_days_primary, win_days)
    print(f"■ 분모(실패일, failed 만) = {total}건")
    print(f"■ next_day_return_rate(주 정의) = {hits:4d} / {total} = {rate:.1%}")
    print("  ↔ Sharif & Shu 벤치마크 0.37 / 0.44 / 0.55 와 직접 대조 가능")
    print()

    hits_s, total_s, rate_s = _rate(fail_days_sensitivity, win_days)
    print(f"■ 분모(실패일, failed + partial_done 민감도) = {total_s}건")
    print(f"■ next_day_return_rate(민감도) = {hits_s:4d} / {total_s} = {rate_s:.1%}")
    print()

    print(
        "※ 이 스크립트는 아무것도 쓰지 않았다. 지표 정의는 "
        "docs/experiments/experiment-plan-v1.md §5 M4 / 근거 대장 §7.3 SQL#3 을 따른다."
    )


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[report-next-day-return] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
