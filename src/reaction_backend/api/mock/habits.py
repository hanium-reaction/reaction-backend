"""Habits mock fixture — Goal Structuring 라우터 연동용 데모 데이터.

실제 Habit CRUD 가 구현되기 전(`/habits` 501)까지, `POST /plans/generate` 가
오케스트레이터에 흘려 넣을 데모 습관 집합을 제공한다.
v0.7.1 §5.7 의 Habit 필드 중 스케줄링에 필요한 부분만 노출한다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DemoHabit:
    """데모 습관 한 건 — `orchestrator.goal_structuring.HabitLike` 를 만족한다."""

    id: uuid.UUID
    title: str
    category: str
    minutes_per_session: int
    time_preference: str  # morning | afternoon | evening | anytime
    priority_level: int  # 1 (가장 높음) ~ 5


# 시드 UUID 는 테스트 안정성을 위해 고정 (uuid5).
_NS = uuid.UUID("00000000-0000-0000-0000-00000000ab18")

DEMO_HABITS: tuple[DemoHabit, ...] = (
    DemoHabit(
        id=uuid.uuid5(_NS, "habit-morning-run"),
        title="아침 러닝",
        category="health",
        minutes_per_session=30,
        time_preference="morning",
        priority_level=2,
    ),
    DemoHabit(
        id=uuid.uuid5(_NS, "habit-evening-read"),
        title="저녁 독서",
        category="self_dev",
        minutes_per_session=45,
        time_preference="evening",
        priority_level=3,
    ),
)
