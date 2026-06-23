"""Habit Penalty 감지 — 순수 로직 (Issue #21-C, S22).

베이스라인 §1.4: **3주 연속 미달**(`done_count < target_count * 0.5`) 시 빈도 재설계 제안
(비난 아닌 재설계). DB/세션 비의존 — repo 가 최근 인스턴스를 넘기면 판정만 한다.

`suggested_frequency` = 최근 3주 평균 달성 횟수(round, 최소 1). 현재 빈도보다 작게 보장.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reaction_backend.db.models.habit_instance import HabitInstance

_WINDOW_WEEKS = 3
_WEEK_DELTA_DAYS = 7


@dataclass(frozen=True)
class PenaltyEval:
    """페널티 제안 — 최근 3주 미달 패턴 + 재설계 빈도."""

    suggested_frequency: int
    avg_done: float
    recent: list[tuple[int, int]]  # (done, target) 오래된→최신 순


def _below_half(instance: HabitInstance) -> bool:
    """done_count < target_count * 0.5 (정수 비교: 2*done < target)."""
    return instance.done_count * 2 < instance.target_count


def evaluate_penalty(
    instances_desc: list[HabitInstance], current_frequency: int
) -> PenaltyEval | None:
    """최근 인스턴스(week_start 내림차순) → 페널티 제안 or None.

    조건: 최근 3개가 (1) 존재 (2) 연속 주 (3) 모두 50% 미만 달성.
    """
    if len(instances_desc) < _WINDOW_WEEKS:
        return None
    last3 = instances_desc[:_WINDOW_WEEKS]

    # 연속 주 검사 (내림차순: 최신 - 직전 = 7일)
    for earlier, later in zip(last3[1:], last3[:-1], strict=True):
        if (later.week_start - earlier.week_start).days != _WEEK_DELTA_DAYS:
            return None

    if not all(_below_half(i) for i in last3):
        return None

    avg = sum(i.done_count for i in last3) / _WINDOW_WEEKS
    suggested = max(1, round(avg))
    if suggested >= current_frequency:
        suggested = max(1, current_frequency - 1)

    recent = [(i.done_count, i.target_count) for i in reversed(last3)]
    return PenaltyEval(suggested_frequency=suggested, avg_done=round(avg, 2), recent=recent)
