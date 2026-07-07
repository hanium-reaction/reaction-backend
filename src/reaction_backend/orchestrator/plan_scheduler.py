"""다일(multi-day) 계획 스케줄러 — First Plan 전용 (룰 only, LLM 0회).

배경(왜 이 모듈이 생겼나):
    이전에는 First Plan 의 배치를 `goal_structuring.reserve_habit_sessions` 로 처리했다.
    그건 원래 **"습관 하루 1세션"** 용이라 (1) `target_date` **단 하루**의 free 구간에
    (2) 가장 이른 슬롯부터 (3) 간격 없이 모든 action_item 을 몰아넣었다. 결과적으로
    마감이 2주 뒤여도 첫날 아침부터 백투백으로 전부 쌓이고, 피크 시간대(오후 등)도
    무시됐다. 계획은 **여러 날에 걸친 작업**인데 습관 배치기를 전용한 게 근본 원인.

이 모듈의 책임:
    분해된 action_item 을 `start_day ~ horizon_day` 범위에 걸쳐 배치한다.
    - **다일 분산**: 하루 집중 총량(`daily_focus_cap_min`)까지 채우면 다음 날로 넘어간다.
    - **피크 우선**: 사용자의 피크 시간대(오후 등) free 슬롯을 먼저 쓰고, 없으면 그날의
      다른 free 로 폴백한다.
    - **세션 분할**: `focus_chunk_min` 보다 긴 카드는 균등한 세션들로 쪼갠다("제목 (1/2)").
    - **간격**: 카드 사이 `break_min` 휴식을 둔다.
    - **순서 보존**: 분해 순서(= 의도된 진행 순서)를 유지해 앞 단계가 앞 날짜에 놓인다.

AGENTS.md 준수:
    - §1 (잠금): 산출물은 미영속 초안(`DraftScheduledBlock`). 자동 적용 금지.
    - §2 (금지): LLM SDK 직접 import 없음 — 순수 규칙 기반.
    - ORM 의존 없음. `goal_structuring` 의 도메인 프리미티브만 재사용해 DB 없이 단위 테스트 가능.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from reaction_backend.orchestrator.goal_structuring import (
    BusyBlock,
    DraftScheduledBlock,
    TimeInterval,
    compute_free_blocks,
)
from reaction_backend.schemas.common import KST

__all__ = [
    "PlanAction",
    "PlanWindow",
    "schedule_actions_multiday",
]

# 세션 분할 시 개별 세션이 이보다 짧아지지 않게 한다(자잘한 꼬리 세션 방지 → UX 저하 회피).
_MIN_SESSION_MIN = 15


@dataclass(frozen=True, slots=True)
class PlanAction:
    """스케줄러가 배치하는 단위 — 분해된 action_item 의 배치용 투영.

    `id` 는 배치 결과 블록의 `origin_id` 로 실려 호출자가 node_id 를 복원하는 키.
    """

    id: uuid.UUID
    node_id: str
    title: str
    category: str
    estimated_minutes: int


@dataclass(frozen=True, slots=True)
class PlanWindow:
    """하루 안의 선호 시간 윈도우 [start, end) (같은 날 안에서만, 자정 안 넘김)."""

    start: time
    end: time


# ─────────────────────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────────────────────


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=KST)


def _split_minutes(total: int, chunk: int) -> list[int]:
    """total 분을 chunk 이하의 균등한 세션들로 나눈다.

    - total <= chunk → [total] (분할 없음).
    - 그 외 n = ceil(total/chunk) 개로 균등 분할(꼬리 세션이 유난히 짧지 않게).
    - 균등 분할 결과 개별 세션이 `_MIN_SESSION_MIN` 미만이 되면 세션 수를 줄인다.
    """
    if total <= chunk or chunk <= 0:
        return [total]
    n = -(-total // chunk)  # ceil
    while n > 1 and total // n < _MIN_SESSION_MIN:
        n -= 1
    base, rem = divmod(total, n)
    # 앞쪽 rem 개 세션에 1분씩 더 실어 합이 정확히 total 이 되게 한다.
    return [base + 1 if i < rem else base for i in range(n)]


def _peak_intervals(day: date, windows: Sequence[PlanWindow]) -> list[TimeInterval]:
    return [TimeInterval(_at(day, w.start), _at(day, w.end)) for w in windows if w.end > w.start]


def _earliest_fit(
    free_blocks: Sequence[TimeInterval],
    need: timedelta,
    prefer: Sequence[TimeInterval],
) -> tuple[int, datetime] | None:
    """need 가 들어가는 배치 시작점을 고른다.

    1순위: prefer(피크) 윈도우와 겹치면서 need 가 들어가는 가장 이른 지점.
    2순위: prefer 로 못 넣으면 free 중 need 가 들어가는 가장 이른 지점(활동창 안 폴백).
    반환: (free_blocks 인덱스, 시작 datetime) 또는 None.
    """
    best: tuple[int, datetime] | None = None
    for index, iv in enumerate(free_blocks):
        for win in prefer:
            start = max(iv.start, win.start)
            end = min(iv.end, win.end)
            if end - start >= need and (best is None or start < best[1]):
                best = (index, start)
    if best is not None:
        return best
    for index, iv in enumerate(free_blocks):
        if iv.end - iv.start >= need:
            return index, iv.start
    return None


def _subtract(
    free_blocks: Sequence[TimeInterval], index: int, placed: TimeInterval, gap: timedelta
) -> list[TimeInterval]:
    """free_blocks[index] 에서 placed(+뒤쪽 gap)를 잘라낸 새 리스트."""
    result: list[TimeInterval] = []
    for i, iv in enumerate(free_blocks):
        if i != index:
            result.append(iv)
            continue
        if placed.start > iv.start:
            result.append(TimeInterval(iv.start, placed.start))
        tail_start = min(placed.end + gap, iv.end)
        if tail_start < iv.end:
            result.append(TimeInterval(tail_start, iv.end))
    return [iv for iv in result if not iv.is_empty]


# ─────────────────────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────────────────────


def schedule_actions_multiday(
    *,
    start_day: date,
    horizon_day: date,
    actions: Sequence[PlanAction],
    busy_for_day: Callable[[date], Sequence[BusyBlock]],
    peak_windows: Sequence[PlanWindow],
    focus_chunk_min: int,
    break_min: int,
    daily_focus_cap_min: int,
) -> tuple[list[DraftScheduledBlock], list[str]]:
    """action_item 들을 start_day~horizon_day 에 걸쳐 배치한다.

    Parameters
    ----------
    start_day / horizon_day:
        배치 가능한 날짜 범위(양끝 포함). horizon_day < start_day 면 start_day 하루로 클램프.
    actions:
        분해 순서(의도된 진행 순서)대로 정렬된 배치 단위.
    busy_for_day:
        해당 날짜의 busy(수면/노터치/고정일정 등)를 돌려주는 콜백. free 는 여기서 파생.
    peak_windows:
        선호 시간 윈도우(피크). 비면 폴백(활동창 전체)만 사용.
    focus_chunk_min:
        이 분(min)보다 긴 카드는 균등 세션으로 분할.
    break_min:
        카드 사이 최소 휴식(분).
    daily_focus_cap_min:
        하루에 배치할 집중 작업 총량 상한(분). 이 상한을 채우면 다음 날로 넘어간다.

    Returns
    -------
    (배치된 초안 블록들, 배치 실패 warnings). 결과는 초안 — 영속화되지 않는다.
    """
    end_day = max(horizon_day, start_day)
    total_days = (end_day - start_day).days + 1
    days: list[date] = [start_day + timedelta(days=i) for i in range(total_days)]

    gap = timedelta(minutes=max(break_min, 0))
    cap = max(daily_focus_cap_min, 1)
    chunk = max(focus_chunk_min, _MIN_SESSION_MIN)

    # 날짜별 free/사용량 상태 (free 는 최초 접근 시 지연 계산)
    free_by_day: dict[date, list[TimeInterval]] = {}
    used_by_day: dict[date, int] = dict.fromkeys(days, 0)

    def free_of(day: date) -> list[TimeInterval]:
        cached = free_by_day.get(day)
        if cached is None:
            cached = compute_free_blocks(day, list(busy_for_day(day)))
            free_by_day[day] = cached
        return cached

    blocks: list[DraftScheduledBlock] = []
    warnings: list[str] = []
    cursor = 0  # 이 인덱스 이전 날짜는 cap 이 찼다고 보고 건너뛴다.

    for action in actions:
        sessions = _split_minutes(action.estimated_minutes, chunk)
        multi = len(sessions) > 1
        for si, minutes in enumerate(sessions):
            need = timedelta(minutes=minutes)
            placed_flag = False
            for di in range(cursor, total_days):
                day = days[di]
                # 하루 집중 상한 — 빈 날이면 단일 세션이 상한을 넘어도 1개는 허용(드롭 방지).
                if used_by_day[day] > 0 and used_by_day[day] + minutes > cap:
                    continue
                free = free_of(day)
                slot = _earliest_fit(free, need, _peak_intervals(day, peak_windows))
                if slot is None:
                    continue
                index, start = slot
                interval = TimeInterval(start, start + need)
                title = f"{action.title} ({si + 1}/{len(sessions)})" if multi else action.title
                blocks.append(
                    DraftScheduledBlock(
                        interval=interval,
                        origin="goal",
                        origin_id=action.id,
                        title=title,
                        category=action.category,
                    )
                )
                free_by_day[day] = _subtract(free, index, interval, gap)
                used_by_day[day] += minutes
                # 이 날의 상한이 찼으면 다음 세션은 다음 날부터 탐색.
                if used_by_day[day] >= cap:
                    cursor = di + 1
                placed_flag = True
                break
            if not placed_flag:
                label = f"{action.title} ({si + 1}/{len(sessions)})" if multi else action.title
                warnings.append(
                    f"'{label}' 을(를) 배치할 가용 시간을 찾지 못했어요. 다른 시간으로 옮겨볼까요?"
                )

    blocks.sort(key=lambda b: b.interval.start)
    return blocks, warnings
