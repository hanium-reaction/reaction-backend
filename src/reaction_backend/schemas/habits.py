"""Habits 도메인 스키마 (api-contract §7) — S27."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel

TimePreference = Literal["morning", "afternoon", "evening", "anytime"]


class Habit(CamelModel):
    """습관 — GET/POST/PATCH 응답 항목."""

    habit_id: str
    title: str
    category: str
    frequency_per_week: int
    minutes_per_session: int
    time_preference: str
    priority_level: int


class HabitCreateRequest(CamelModel):
    """POST /habits 요청."""

    title: str = Field(min_length=1)
    category: str
    frequency_per_week: int = Field(ge=1, le=7)
    minutes_per_session: int = Field(ge=1)
    time_preference: TimePreference
    priority_level: int = Field(ge=1, le=5)


class HabitUpdateRequest(CamelModel):
    """PATCH /habits/{id} 요청 — 제목·빈도 (api-contract §7)."""

    title: str | None = None
    frequency_per_week: int | None = Field(default=None, ge=1, le=7)


class HabitInstance(CamelModel):
    """주별 Habit 인스턴스 — GET /habit-instances 응답 항목."""

    instance_id: str
    habit_id: str
    week_start: str  # YYYY-MM-DD (월요일)
    target_count: int
    done_count: int
