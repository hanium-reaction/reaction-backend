"""Plan 직접 편집(S15) 순수 로직 — 15분 snap · 시간 충돌 · 정책 위반 (Issue #21-B).

DB/세션 비의존 — `ScheduledBlockRepo` 가 충돌 후보를, 라우터가 정책 목록을 넘기면
여기서 판정만 한다. `orchestrator/recovery.py` 처럼 단위 테스트 가능하게 유지.

정책 위반 검사 대상(#21-B): `sleep` · `lunch` · `late_night_block` 윈도우.
`no_touch`/`break_min`/`custom` 은 후속 (요일·휴식 규칙은 별도 슬라이스).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reaction_backend.db.models.time_policy import TimePolicy

_SNAP_MINUTES = 15
_MINUTES_PER_DAY = 24 * 60


def snap_to_15min(dt: datetime) -> datetime:
    """가장 가까운 15분 경계로 스냅 (초/마이크로 제거). 23:53 → 다음날 00:00 가능."""
    base = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    snapped = round((dt.hour * 60 + dt.minute) / _SNAP_MINUTES) * _SNAP_MINUTES
    return base + timedelta(minutes=snapped)


def _intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """반열린 구간 [start, end) 겹침."""
    return a_start < b_end and b_start < a_end


def _block_day_intervals(start_kst: datetime, end_kst: datetime) -> list[tuple[int, int]]:
    """블록을 0~1440 분 축의 [a,b) 구간(자정 넘으면 2개)으로 변환."""
    start_min = start_kst.hour * 60 + start_kst.minute
    duration = max(int((end_kst - start_kst).total_seconds() // 60), 0)
    end_abs = start_min + duration
    if end_abs <= _MINUTES_PER_DAY:
        return [(start_min, end_abs)]
    return [(start_min, _MINUTES_PER_DAY), (0, end_abs - _MINUTES_PER_DAY)]


def _window_intervals(win_start: time, win_end: time) -> list[tuple[int, int]]:
    """시간대 윈도우 [start, end) → 분 구간. 자정 가로지르면(예: 23:00~07:00) 2개로 분할."""
    start_min = win_start.hour * 60 + win_start.minute
    end_min = win_end.hour * 60 + win_end.minute
    if end_min > start_min:
        return [(start_min, end_min)]
    # wrap: [start, 24:00) ∪ [00:00, end)
    intervals = [(start_min, _MINUTES_PER_DAY)]
    if end_min > 0:
        intervals.append((0, end_min))
    return intervals


def _touches_window(block: list[tuple[int, int]], window: list[tuple[int, int]]) -> bool:
    return any(_intervals_overlap(bs, be, ws, we) for bs, be in block for ws, we in window)


def _parse_time(raw: object) -> time | None:
    if not isinstance(raw, str):
        return None
    try:
        return time.fromisoformat(raw)
    except ValueError:
        return None


def find_policy_violation(
    start_kst: datetime,
    end_kst: datetime,
    category: str,
    policies: list[TimePolicy],
) -> str | None:
    """이동된 블록이 활성 시간 정책 윈도우에 들어가면 위반 policy_type 반환, 없으면 None."""
    block_intervals = _block_day_intervals(start_kst, end_kst)

    for policy in policies:
        if not policy.is_active:
            continue
        payload = policy.payload or {}
        ptype = policy.policy_type

        if ptype in ("sleep", "lunch"):
            win_start = _parse_time(payload.get("start_time"))
            win_end = _parse_time(payload.get("end_time"))
            if win_start is None or win_end is None:
                continue
            if _touches_window(block_intervals, _window_intervals(win_start, win_end)):
                return ptype

        elif ptype == "late_night_block":
            win_start = _parse_time(payload.get("start_time"))
            if win_start is None:
                continue
            blocked = payload.get("blocked_categories") or []
            # 카테고리 제한이 있으면 해당 카테고리만, 없으면 전부 금지.
            if blocked and category not in blocked:
                continue
            window = _window_intervals(win_start, time(0, 0))  # [start, 24:00)
            if _touches_window(block_intervals, window):
                return ptype

    return None
