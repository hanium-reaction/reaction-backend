"""Today / Execution 도메인 스키마 (api-contract §10) — S10~S13.

Issue #19-A 범위: **조회만** (agenda + action detail). Focus 실행 로깅(start/pause/
resume/check-ins)은 #19-B (scheduled_blocks 의존).
"""

from __future__ import annotations

from reaction_backend.schemas.common import CamelModel


class MorningBrief(CamelModel):
    """S10 상단 Morning Brief 카드 (daily_briefs). 없으면 agenda.brief=null."""

    headline: str
    big_rock_action_id: str | None
    adjustment_hints: list[str]
    fallback_used: bool


class AgendaCard(CamelModel):
    """S10 어젠다의 ActionItem 카드 (조회 표현)."""

    action_id: str
    title: str
    category: str
    status: str
    priority: int
    estimated_minutes: int
    source: str
    why_now: str | None
    first_step: str | None


class AgendaHabit(CamelModel):
    """S10 습관 row — 이번 주 habit_instance 진행."""

    instance_id: str
    habit_id: str
    title: str
    target_count: int
    done_count: int


class AgendaFixedSchedule(CamelModel):
    """S10 고정 일정 row — 오늘 요일에 걸린 것."""

    schedule_id: str
    title: str
    start_time: str  # HH:MM
    end_time: str  # HH:MM


class TodayAgenda(CamelModel):
    """GET /today/agenda 응답 — 단일 조회 (daily_brief + cards + habits + fixed)."""

    date: str  # YYYY-MM-DD (KST)
    brief: MorningBrief | None
    cards: list[AgendaCard]
    habits: list[AgendaHabit]
    fixed_schedules: list[AgendaFixedSchedule]


class ActionDetail(CamelModel):
    """GET /today/actions/{id} 응답 — S11 카드 상세."""

    action_id: str
    title: str
    category: str
    status: str
    priority: int
    estimated_minutes: int
    target_date: str  # YYYY-MM-DD
    source: str
    why_now: str | None
    first_step: str | None
    goal_id: str | None
