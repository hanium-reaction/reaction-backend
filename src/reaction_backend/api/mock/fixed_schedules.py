"""Fixed Schedules mock fixture — #3-C 스텁용 (S05).

캘린더 미연결 사용자의 데모 고정 일정 (수업·알바).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DemoFixedSchedule:
    """고정 일정 한 건 (api-contract §19)."""

    schedule_id: str
    title: str
    days_of_week: tuple[str, ...]
    start_time: str  # HH:MM
    end_time: str  # HH:MM


DEMO_FIXED_SCHEDULES: tuple[DemoFixedSchedule, ...] = (
    DemoFixedSchedule("fixed_demo_algo", "알고리즘 수업", ("mon", "wed"), "09:00", "10:30"),
    DemoFixedSchedule("fixed_demo_parttime", "카페 알바", ("tue", "thu"), "18:00", "22:00"),
)
