"""Fixed Schedules 도메인 스키마 (api-contract §19) — S05 수동 고정 일정."""

from __future__ import annotations

from pydantic import Field

from reaction_backend.schemas.common import CamelModel


class FixedSchedule(CamelModel):
    """고정 일정 — GET/POST/PATCH 응답."""

    schedule_id: str
    title: str
    days_of_week: list[str]
    start_time: str  # HH:MM
    end_time: str  # HH:MM


class FixedScheduleCreateRequest(CamelModel):
    """POST /fixed-schedules 요청."""

    title: str = Field(min_length=1)
    days_of_week: list[str] = Field(min_length=1)
    start_time: str
    end_time: str


class FixedScheduleUpdateRequest(CamelModel):
    """PATCH /fixed-schedules/{id} 요청 — 부분 수정."""

    title: str | None = None
    days_of_week: list[str] | None = None
    start_time: str | None = None
    end_time: str | None = None
