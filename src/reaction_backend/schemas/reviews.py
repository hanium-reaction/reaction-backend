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


class GoalCompletionProposal(CamelModel):
    """ "이 목표 끝난 거 맞아요?" 제안 1건 (ADR-0007 6b).

    마일스톤이 **전부** 완료된 목표에 대해 나간다. `NextCycleProposal` 과 **배타적**이다 —
    같은 가드(`has_open_milestone`)의 양쪽 갈래라, 한 목표가 두 카드에 동시에 뜨지 않는다.

    확정은 `POST /goals/{goalId}/complete`.
    """

    goal_id: UUID
    goal_title: str


class StaleAxisProposal(CamelModel):
    """3주 연속 손 못 댄 축 — "줄이거나 바꾸자" 제안 1건 (ADR-0008 §6, §8 "H").

    수정 수단은 이미 있는 것을 그대로 쓴다 — 이 카드는 새 엔드포인트를 만들지 않는다.
    칸/축 텍스트는 `PATCH /goals/mandala/nodes/{id}`, 축 8칸 재생성은
    `POST /plans/mandala/{planId}/regenerate-branch`.
    """

    axis_id: UUID
    axis_title: str


class TopFailureContext(CamelModel):
    """실패 사유 상위 3개(BCT 2.3 Self-monitoring, 근거 A5) — #301.

    `labelKo` 는 `/reflection/failure-tags` 와 같은 마스터(`failure_reason_tags`)에서 온다
    (이중 관리 방지). `share` 는 0~1 비율이고, LIMIT 이전(태그 전체)을 분모로 하므로 반환된
    3건의 share 합이 1.0 이 아닐 수 있다(태그가 4개 이상인 주).
    """

    tag_code: str
    label_ko: str
    count: int
    share: float


class EffortMinutes(CamelModel):
    """이번 주를 **분**으로 본 요약 (ADR-0009 D5).

    `adherenceRate`(건수 비율) 옆에 나란히 둔다. 계획 세션 길이가 작업 내용을 따라가면
    건수와 시간이 갈라진다 — 15분짜리 9개를 끝내고 3시간짜리 1개를 못 하면 건수로는 90%
    지만 실제로는 절반도 못 한 주다.

    `actualMinutes` 를 `completedMinutes` 와 나누면 "예상이 맞았나" 가 나온다
    (1.0 = 예상대로, 1.3 = 30% 더 걸렸다). 둘 다 **완료한 실행**만 센다.
    """

    planned_minutes: int = 0
    completed_minutes: int = 0
    actual_minutes: int = 0
    adherence_rate: float | None = None


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

    # 같은 주를 분으로 다시 센 요약 (ADR-0009 D5). `period_summaries` 에 저장하지 않고
    # 조회 시점에 파생한다 — mandala/proposals 와 같은 방식이라 마이그레이션이 없다.
    effort: EffortMinutes = Field(default_factory=EffortMinutes)
    # 그 주에 잡혀 있었지만 한 번도 시작하지 않고 지나간 블록(세션) 수 (v2.30). 준수율은
    # 시작한 카드만 세므로 이 수는 그 분모 밖이다 — 준수율 정의는 그대로 두고 옆에 싣는다.
    # 조회 시점 파생(저장 안 함).
    unstarted_blocks: int = 0

    category_success_rate: dict[str, float] = Field(default_factory=dict)
    peak_window: str | None = None
    drain_window: str | None = None
    one_liner: str | None = None
    policy_update_candidates: list[dict[str, object]] = Field(default_factory=list)
    # 최근 28일 실패 사유 상위 3개(#301) — mandala/proposals 처럼 저장 안 하고 조회 시점에
    # 파생한다. 없으면(실패 0건) 빈 배열 — FE 는 빈 배열이면 섹션을 렌더하지 않는다.
    top_failure_contexts: list[TopFailureContext] = Field(default_factory=list)

    mandala: MandalaWeeklySummary | None = None
    next_cycle_proposals: list[NextCycleProposal] = Field(default_factory=list)
    goal_completion_proposals: list[GoalCompletionProposal] = Field(default_factory=list)
    stale_axis_proposals: list[StaleAxisProposal] = Field(default_factory=list)

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
