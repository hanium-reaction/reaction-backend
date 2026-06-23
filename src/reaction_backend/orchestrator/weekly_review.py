"""Weekly Review KPI 계산 — 순수 함수 (Issue #21-A, S21).

MVP 는 **룰 기반** (LLM 한 줄 평은 P2 — 이슈 #21 본문). DB/ORM 비의존:
`ReviewRepo` 가 ORM row 를 `ExecutionStat` / `RecoveryStat` 로 매핑해 넘기면
여기서는 집계만 한다. `orchestrator/recovery.py` 의 `select_strategies` 처럼
세션 없이 단위 테스트 가능하게 유지한다.

집계 대상 KPI (Memory Structure Weekly Report · DB 설계서 §5.27):
- adherence_rate       완료(done/over_done) / 종결 실행
- consistency_days     완료 실행이 있는 날의 최장 연속 일수
- resilience_rate      실패(failed/partial_done) 중 회복 카드를 **수락**한 비율 (#21-A 정의)
- avg_delay_minutes    계획 대비 실제 시작 지연 평균
- average_recovery_minutes  수락된 회복의 평균 소요 (대부분 #20-B 후 채워짐)
- category_success_rate     카테고리별 완료율
- peak/drain_point_window   (요일×시간대) 성공률 최고/최저 버킷
- one_liner            peak 윈도우에서 룰로 뽑은 한 줄

#21-A 에서 null 로 두는 것 (후속): restart_success_rate / repeated_failure_count
(각각 interruption·failure_tag 조인 필요) / policy_update_candidates (P2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from reaction_backend.schemas.common import to_kst

# 종결(terminal) 상태 — in_progress 제외. adherence 분모.
_TERMINAL_STATUSES = ("done", "partial_done", "failed", "over_done")
# 성공으로 치는 상태. adherence 분자.
_SUCCESS_STATUSES = ("done", "over_done")
# 회복 대상 — 실패/부분완료 (recovery 라우트 _ELIGIBLE_STATUSES 와 동일 정의).
_FAILURE_STATUSES = ("failed", "partial_done")

_WEEKDAY_EN = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_WEEKDAY_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")
_DAYPART_KO = {"morning": "오전", "afternoon": "오후", "evening": "저녁"}


@dataclass(frozen=True)
class ExecutionStat:
    """집계에 필요한 실행 1건의 평탄화된 뷰 (ORM 비의존)."""

    completion_status: str
    category: str
    plan_start_at: datetime
    actual_start_at: datetime | None = None
    delay_minutes: int | None = None
    # 이 실행(failed/partial)에 대해 회복 카드를 수락했는가 (resilience 분자).
    is_recovered: bool = False


@dataclass(frozen=True)
class RecoveryStat:
    """수락된 회복 1건의 평탄화된 뷰 — average_recovery_minutes 용."""

    recovery_duration_minutes: int | None = None


@dataclass(frozen=True)
class WeeklyKpi:
    """주간 KPI 집계 결과 — `period_summaries` 컬럼과 1:1."""

    adherence_rate: float | None = None
    consistency_days: int | None = None
    resilience_rate: float | None = None
    avg_delay_minutes: float | None = None
    restart_success_rate: float | None = None
    repeated_failure_count: int | None = None
    average_recovery_minutes: float | None = None
    category_success_rate: dict[str, float] = field(default_factory=dict)
    drain_point_window: str | None = None
    peak_point_window: str | None = None
    one_liner: str | None = None
    policy_update_candidates: list[dict[str, object]] = field(default_factory=list)


def _daypart(hour: int) -> str:
    """시(0~23) → 시간대 버킷. 저녁은 18~다음날 새벽."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    return "evening"


def _kst_date_and_window(dt: datetime) -> tuple[date, str, str]:
    """실행 시각 → (KST 날짜, 요일_en, 'weekday_daypart') 윈도우 키."""
    local = to_kst(dt)
    weekday = _WEEKDAY_EN[local.weekday()]
    return local.date(), weekday, f"{weekday}_{_daypart(local.hour)}"


def _ratio(numerator: int, denominator: int) -> float | None:
    """0 나눗셈 방어. 분모 0 → None (데이터 없음)."""
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _longest_streak(days: set[date], week_start: date) -> int:
    """주간(월~일) 내 완료가 있는 날의 최장 연속 일수."""
    best = run = 0
    for offset in range(7):
        if (week_start + timedelta(days=offset)) in days:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _delay_minutes(stat: ExecutionStat) -> int | None:
    """저장된 delay_minutes 우선, 없으면 actual-plan 으로 계산."""
    if stat.delay_minutes is not None:
        return stat.delay_minutes
    if stat.actual_start_at is None:
        return None
    return int((stat.actual_start_at - stat.plan_start_at).total_seconds() // 60)


def _one_liner(peak_window: str | None) -> str:
    """peak 윈도우 기반 룰 한 줄 (금지어 없음, 따뜻한 톤)."""
    if peak_window is None:
        return "이번 주는 데이터가 충분하지 않았어요. 다음 주에 다시 함께 살펴봐요."
    weekday, _, daypart = peak_window.partition("_")
    ko_weekday = _WEEKDAY_KO[_WEEKDAY_EN.index(weekday)] if weekday in _WEEKDAY_EN else weekday
    ko_daypart = _DAYPART_KO.get(daypart, daypart)
    return f"이번 주는 {ko_weekday} {ko_daypart}에 가장 잘 풀렸어요."


def _peak_drain(
    terminal: list[ExecutionStat],
) -> tuple[str | None, str | None]:
    """(요일×시간대) 버킷별 성공률 → (peak, drain) 윈도우.

    동률은 표본 수가 많은 버킷 우선, 그래도 같으면 _WEEKDAY_EN 정렬 순.
    """
    buckets: dict[str, list[int]] = {}
    for stat in terminal:
        _, _, window = _kst_date_and_window(stat.plan_start_at)
        buckets.setdefault(window, []).append(
            1 if stat.completion_status in _SUCCESS_STATUSES else 0
        )
    if not buckets:
        return None, None

    def rate(window: str) -> tuple[float, int]:
        outcomes = buckets[window]
        return sum(outcomes) / len(outcomes), len(outcomes)

    peak = max(buckets, key=lambda w: (rate(w)[0], rate(w)[1]))
    drain = min(buckets, key=lambda w: (rate(w)[0], -rate(w)[1]))
    # 버킷이 1개뿐이면 peak == drain — drain 은 의미 없으니 숨긴다.
    if peak == drain:
        return peak, None
    return peak, drain


def compute_weekly_kpis(
    executions: list[ExecutionStat],
    recoveries: list[RecoveryStat],
    week_start: date,
) -> WeeklyKpi:
    """주간 실행/회복 통계 → KPI 집계 (순수 함수)."""
    terminal = [e for e in executions if e.completion_status in _TERMINAL_STATUSES]
    if not terminal:
        # 표본 없음 — adherence/consistency 등은 None, one_liner 만 안내.
        avg_rec = _avg_recovery(recoveries)
        return WeeklyKpi(
            average_recovery_minutes=avg_rec,
            one_liner=_one_liner(None),
        )

    success = sum(1 for e in terminal if e.completion_status in _SUCCESS_STATUSES)
    adherence = _ratio(success, len(terminal))

    done_days = {
        _kst_date_and_window(e.plan_start_at)[0]
        for e in terminal
        if e.completion_status in _SUCCESS_STATUSES
    }
    consistency = _longest_streak(done_days, week_start)

    failures = [e for e in terminal if e.completion_status in _FAILURE_STATUSES]
    recovered = sum(1 for e in failures if e.is_recovered)
    resilience = _ratio(recovered, len(failures))

    delays = [d for d in (_delay_minutes(e) for e in terminal) if d is not None]
    avg_delay = round(sum(delays) / len(delays), 2) if delays else None

    category_rate = _category_success(terminal)
    peak, drain = _peak_drain(terminal)

    return WeeklyKpi(
        adherence_rate=adherence,
        consistency_days=consistency,
        resilience_rate=resilience,
        avg_delay_minutes=avg_delay,
        average_recovery_minutes=_avg_recovery(recoveries),
        category_success_rate=category_rate,
        peak_point_window=peak,
        drain_point_window=drain,
        one_liner=_one_liner(peak),
    )


def _category_success(terminal: list[ExecutionStat]) -> dict[str, float]:
    totals: dict[str, list[int]] = {}
    for stat in terminal:
        totals.setdefault(stat.category, []).append(
            1 if stat.completion_status in _SUCCESS_STATUSES else 0
        )
    return {cat: round(sum(v) / len(v), 4) for cat, v in totals.items()}


def _avg_recovery(recoveries: list[RecoveryStat]) -> float | None:
    durations = [r.recovery_duration_minutes for r in recoveries if r.recovery_duration_minutes]
    if not durations:
        return None
    return round(sum(durations) / len(durations), 2)
