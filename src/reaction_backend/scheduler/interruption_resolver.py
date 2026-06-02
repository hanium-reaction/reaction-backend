"""Interruption timeout cron job — 6h 미재개 일시정지 자동 종결 (Issue #19-C).

`interruption_events.resumed_after_interrupt IS NULL` 이고 6시간 넘게 재개 안 된 행을
`false` 로 처리한다 (사용자가 [계속] 안 누르고 떠난 경우). 시간당 1회 실행 가정.

⚠️ 본 모듈은 **job 함수**다. 실제 스케줄 트리거(6h/1h마다)는 Issue #24 에서 등록.
**idempotent** — 이미 false/true 인 행은 건드리지 않음 (NULL 만 대상). 다회 실행 안전.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reaction_backend.repositories.interruption_event_repo import InterruptionEventRepo

_STALE_AFTER = timedelta(hours=6)


async def run_interruption_resolver(
    now_kst_dt: datetime,
    *,
    repo: InterruptionEventRepo,
) -> int:
    """6시간 넘게 재개 안 된 일시정지를 `resumed_after_interrupt=false` 로 종결.

    Returns:
        처리한 행 수 (관측/로그용).
    """
    cutoff = now_kst_dt - _STALE_AFTER
    stale = await repo.list_stale_unresolved(before=cutoff)
    for event in stale:
        await repo.mark_unresumed(event)
    return len(stale)
