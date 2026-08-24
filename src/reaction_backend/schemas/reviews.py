"""Reviews 스키마 — S21 Weekly Review (Issue #21-A).

응답 규약(common.py): 성공은 도메인 객체 직접 반환(envelope 없음), camelCase 직렬화,
시간은 KstDatetime. 핵심 필드는 api-contract.md §13 기준.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime


class WeeklyGenerateRequest(CamelModel):
    """POST /reviews/weekly/generate — 수동 재생성 (디버그).

    `weekStart` 생략 시 이번 주(월요일)로 계산한다.
    """

    week_start: str | None = Field(default=None, description="YYYY-MM-DD (해당 주 월요일)")


class MandalaHabitWeekStat(CamelModel):
    """만다라 반복형 칸 1개의 이번 주 체크인 현황."""

    axis_title: str | None = None
    cell_title: str
    done_count: int
    target_count: int


class MandalaWeeklySummary(CamelModel):
    """`GET /reviews/weekly` 의 '이번 주 만다라트' 절 (ADR-0008 §8 "E").

    조회 시점에 파생한다(저장 안 함) — 궁극목표가 없거나 아직 승인된 만다라 트리가 없으면
    응답 자체에서 생략된다(`null`).
    """

    completed_this_week: int
    completed_total: int
    total_leaves: int
    touched_this_week: int
    untouched_axis_titles: list[str] = Field(default_factory=list)
    habits: list[MandalaHabitWeekStat] = Field(default_factory=list)


class NextCycleProposal(CamelModel):
    """다음 2주 열기 제안 1건 (ADR-0008 §8 "G") — 승인은 기존 `/plans/generate`(빈 바디)
    + `/plans/{id}/approve` 를 그대로 쓴다. 이 카드는 새 엔드포인트를 만들지 않는다.
    """

    goal_id: UUID
    goal_title: str
    axis_title: str | None = None


class WeeklyReviewResponse(CamelModel):
    """GET /reviews/weekly · generate 응답 — 룰 기반 주간 리뷰 카드 (S21)."""

    week_start: date
    week_end: date

    adherence_rate: float | None = None
    consistency_days: int | None = None
    resilience_rate: float | None = None
    avg_delay_minutes: float | None = None
    restart_success_rate: float | None = None
    repeated_failure_count: int | None = None
    average_recovery_minutes: float | None = None

    category_success_rate: dict[str, float] = Field(default_factory=dict)
    peak_window: str | None = None
    drain_window: str | None = None
    one_liner: str | None = None
    policy_update_candidates: list[dict[str, object]] = Field(default_factory=list)

    mandala: MandalaWeeklySummary | None = None
    next_cycle_proposals: list[NextCycleProposal] = Field(default_factory=list)

    generated_at: KstDatetime


# ── S22 Habit Penalty (#21-C) ──


class HabitWeekStat(CamelModel):
    """페널티 근거 — 한 주의 달성/목표."""

    done_count: int
    target_count: int


class HabitPenaltyCandidate(CamelModel):
    """3주 연속 미달로 빈도 재설계를 제안할 habit."""

    habit_id: str
    title: str
    current_frequency: int
    suggested_frequency: int
    recent_weeks: list[HabitWeekStat] = Field(default_factory=list)
    message: str


class HabitPenaltyListResponse(CamelModel):
    """GET /reviews/habit-penalty — 제안 후보 목록."""

    candidates: list[HabitPenaltyCandidate] = Field(default_factory=list)


class HabitPenaltyAcceptResponse(CamelModel):
    """POST /reviews/habit-penalty/{habitId}/accept — 빈도 다운 결과."""

    habit_id: str
    previous_frequency: int
    new_frequency: int
    message: str
