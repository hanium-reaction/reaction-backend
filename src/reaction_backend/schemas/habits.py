"""Habits 도메인 스키마 (api-contract §7) — S27."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from reaction_backend.schemas.common import CamelModel
from reaction_backend.schemas.goals import clean_title

TimePreference = Literal["morning", "afternoon", "evening", "anytime"]
# db.models.habit.HABIT_CATEGORY_VALUES 와 일치 — 여기서 막지 않으면 DB CheckConstraint
# 위반으로 떨어져 raw IntegrityError → 500 COMMON_INTERNAL_ERROR 가 된다(422 대신).
HabitCategory = Literal["study", "health", "routine", "self_dev", "relationship", "other"]

# `habits.title` 컬럼 길이(db/models/habit.py `String(200)`) — 넘으면 DB 에서 500 이 났다.
HABIT_TITLE_MAX_LENGTH = 200
# 한 번 하는 데 드는 시간 상한 — 하루(1440분). 컬럼이 32bit 정수라 큰 값은 500 이 났다.
HABIT_MINUTES_PER_SESSION_MAX = 1440


class Habit(CamelModel):
    """습관 — GET/POST/PATCH 응답 항목."""

    habit_id: str
    title: str
    category: str
    frequency_per_week: int
    minutes_per_session: int
    time_preference: str
    priority_level: int
    # 만다라 반복형 칸에서 만들어졌으면 그 노드 id, 아니면 null(ADR-0008 §1).
    goal_node_id: str | None = None


class HabitCreateRequest(CamelModel):
    """POST /habits 요청."""

    title: str = Field(min_length=1, max_length=HABIT_TITLE_MAX_LENGTH)
    category: HabitCategory
    frequency_per_week: int = Field(ge=1, le=7)
    minutes_per_session: int = Field(ge=1, le=HABIT_MINUTES_PER_SESSION_MAX)
    time_preference: TimePreference
    priority_level: int = Field(ge=1, le=5)

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v: object) -> object:
        return clean_title(v, noun="습관 이름", max_length=HABIT_TITLE_MAX_LENGTH)


class HabitUpdateRequest(CamelModel):
    """PATCH /habits/{id} 요청 — 제목·빈도 (api-contract §7)."""

    title: str | None = Field(default=None, min_length=1, max_length=HABIT_TITLE_MAX_LENGTH)
    frequency_per_week: int | None = Field(default=None, ge=1, le=7)

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v: object) -> object:
        return clean_title(v, noun="습관 이름", max_length=HABIT_TITLE_MAX_LENGTH)


class HabitInstance(CamelModel):
    """주별 Habit 인스턴스 — GET /habit-instances 응답 항목."""

    instance_id: str
    habit_id: str
    week_start: str  # YYYY-MM-DD (월요일)
    target_count: int
    done_count: int
