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
    """POST /fixed-schedules 요청.

    제목 앞뒤 공백 제거·200자 상한(`fixed_schedules.title` String(200))·요일 중복 제거·
    `HH:MM`(24:00 허용) 검사는 라우터가 한다 — 사용자에게 보일 한국어 문구로 422 를 내려고.
    """

    title: str = Field(min_length=1)
    days_of_week: list[str] = Field(min_length=1)
    start_time: str
    end_time: str


class FixedScheduleUpdateRequest(CamelModel):
    """PATCH /fixed-schedules/{id} 요청 — 부분 수정. 준 필드는 생성과 같은 검사를 받는다
    (빈 제목·빈 요일 목록은 422 — '안 바꿈' 은 필드를 빼는 것이다)."""

    title: str | None = None
    days_of_week: list[str] | None = None
    start_time: str | None = None
    end_time: str | None = None
