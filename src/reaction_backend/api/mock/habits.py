"""Habits mock fixture — #3-D 스텁용 (S27).

데모 습관 + 이번 주 인스턴스 (habit_instances).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DemoHabit:
    """습관 한 건 (api-contract §7)."""

    habit_id: str
    title: str
    category: str
    frequency_per_week: int
    minutes_per_session: int
    time_preference: str  # morning | afternoon | evening | anytime
    priority_level: int


DEMO_HABITS: tuple[DemoHabit, ...] = (
    DemoHabit("habit_workout", "운동 30분", "건강", 3, 30, "morning", 2),
    DemoHabit("habit_meditation", "명상 10분", "건강", 7, 10, "evening", 3),
)


@dataclass(frozen=True, slots=True)
class DemoHabitInstance:
    """이번 주 Habit 인스턴스 (habit_instances)."""

    instance_id: str
    habit_id: str
    week_start: str  # YYYY-MM-DD (월요일)
    target_count: int
    done_count: int


DEMO_HABIT_INSTANCES: tuple[DemoHabitInstance, ...] = (
    DemoHabitInstance("inst_workout_w21", "habit_workout", "2026-05-18", 3, 2),
    DemoHabitInstance("inst_meditation_w21", "habit_meditation", "2026-05-18", 7, 5),
)
