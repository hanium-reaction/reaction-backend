"""Reviews 스키마 — S21 Weekly Review (Issue #21-A).

응답 규약(common.py): 성공은 도메인 객체 직접 반환(envelope 없음), camelCase 직렬화,
시간은 KstDatetime. 핵심 필드는 api-contract.md §13 기준.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime


class WeeklyGenerateRequest(CamelModel):
    """POST /reviews/weekly/generate — 수동 재생성 (디버그).

    `weekStart` 생략 시 이번 주(월요일)로 계산한다.
    """

    week_start: str | None = Field(default=None, description="YYYY-MM-DD (해당 주 월요일)")


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

    generated_at: KstDatetime
