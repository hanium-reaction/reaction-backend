"""Goal Structuring Orchestrator — 첫 계획 생성의 규칙 기반 스케줄링 뼈대.

architecture.md §2.1 상태머신:
    VALIDATING → PLANNING → REVIEWING → HITL → SAVING → DONE

이 모듈(#18)의 책임:
- 시간 정책(TimePolicy) + 고정 일정(FixedSchedule) + 습관(Habit) 을 조합해
  하루 단위 free/busy 블록을 계산한다.
- 계산 결과는 **활성화되지 않은 초안(DraftPlan)** 으로만 반환한다. 실제 적용
  (scheduled_blocks INSERT)은 HITL [수락] 이후 SAVING 단계에서만 일어난다.
- 수면 등 절대 정책을 위반하는 블록이 삽입되려 하면 `PolicyViolationError` 를
  던져 SAVING 트랜잭션을 안전하게 롤백시킨다.

AGENTS.md 준수:
- §1 (잠금): AI 출력 = Draft Layer, 자동 적용 금지 → `DraftPlan.is_active` 는 항상 False.
- §2 (금지): LLM SDK 직접 import 금지 → 본 모듈은 LLM 호출이 전혀 없는 순수 규칙 기반.
- §1/§2: 정책 위반 블록 생성 금지 → `guard_*` 가드 + `policy_guarded_transaction`.
- 시간은 `schemas.common.now_kst()` / `KST` 사용 (UTC 저장, KST 계산).

본 모듈은 ORM/LLM 의존성이 없다. 입력은 구조적 타입(Protocol)으로만 받으므로
DB 없이 단위 테스트가 가능하다 (AGENTS §6). 가드 로직이 향후 다른 오케스트레이터와
공유되면 `safety/` 로 추출할 수 있다 (AGENTS §4).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Protocol

from reaction_backend.schemas.common import KST, now_kst

__all__ = [
    "BusyBlock",
    "DraftPlan",
    "DraftScheduledBlock",
    "GoalStructuringInput",
    "GoalStructuringOrchestrator",
    "OrchestratorState",
    "PolicyViolationError",
    "TimeInterval",
    "compute_free_blocks",
    "fixed_schedules_to_busy",
    "guard_draft_block",
    "guard_draft_plan",
    "policy_guarded_transaction",
    "reserve_habit_sessions",
    "time_policies_to_busy",
]


# ─────────────────────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────────────────────

# date.weekday() 인덱스(월=0) ↔ FixedSchedule/no_touch 의 요일 키.
WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# 위반 시 트랜잭션 롤백 대상이 되는 "절대" 정책. break_min/lunch 는 소프트 제약이라 제외.
ABSOLUTE_POLICY_TYPES: frozenset[str] = frozenset({"sleep", "no_touch", "late_night_block"})

# Habit.time_preference → 배치 허용 윈도우.
_TIME_PREFERENCE_WINDOWS: dict[str, tuple[time, time]] = {
    "morning": (time(6, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(18, 0)),
    "evening": (time(18, 0), time(23, 0)),
    "anytime": (time(6, 0), time(23, 0)),
}


# ─────────────────────────────────────────────────────────────────────────────
# 입력 구조적 타입 (Protocol) — ORM 모델이 런타임에 자연스럽게 만족한다.
# ─────────────────────────────────────────────────────────────────────────────


class TimePolicyLike(Protocol):
    """`db.models.time_policy.TimePolicy` 의 스케줄링에 필요한 부분."""

    policy_type: str
    payload: Mapping[str, Any]
    is_active: bool


class FixedScheduleLike(Protocol):
    """`db.models.fixed_schedule.FixedSchedule` 의 스케줄링에 필요한 부분."""

    title: str
    days_of_week: Sequence[str]
    start_time: time
    end_time: time


class HabitLike(Protocol):
    """`db.models.habit.Habit` 의 스케줄링에 필요한 부분."""

    id: uuid.UUID
    title: str
    category: str
    minutes_per_session: int
    time_preference: str
    priority_level: int


class SupportsTransaction(Protocol):
    """`commit` / `rollback` 가능한 세션 (예: SQLAlchemy AsyncSession)."""

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# 도메인 프리미티브
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """KST tz-aware 반열린 구간 [start, end)."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeInterval 은 tz-aware datetime 만 허용한다 (KST).")
        if self.end < self.start:
            raise ValueError("TimeInterval.end 가 start 보다 빠를 수 없다.")

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def is_empty(self) -> bool:
        return self.start == self.end

    def overlaps(self, other: TimeInterval) -> bool:
        """두 구간이 0 이상 길이로 겹치면 True (경계 접촉은 겹침 아님)."""
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class BusyBlock:
    """가용 시간에서 제외되는 점유 구간."""

    interval: TimeInterval
    source: str  # "sleep" | "lunch" | "no_touch" | "late_night_block" | "fixed_schedule"
    label: str


@dataclass(frozen=True, slots=True)
class DraftScheduledBlock:
    """초안 스케줄 블록 — DB `scheduled_blocks` 에 대응하나 **아직 영속화되지 않음**.

    HITL [수락] 전까지는 어떤 경우에도 `scheduled_blocks` 로 INSERT 되지 않는다
    (AGENTS §1: 자동 적용 금지).
    """

    interval: TimeInterval
    origin: str  # "habit" | "goal"
    origin_id: uuid.UUID | None
    title: str
    category: str
    source: str = "ai_plan"  # scheduled_blocks.source
    block_status: str = "scheduled"  # scheduled_blocks.block_status


@dataclass(frozen=True, slots=True)
class DraftPlan:
    """오케스트레이터 산출물 — 항상 비활성 초안 (Draft Layer).

    `is_active` 는 `init=False` + 기본 False 로 고정되어 생성자에서도 켤 수 없다.
    활성화는 오직 HITL [수락] → SAVING 경로에서만 일어난다 (AGENTS §1).
    """

    target_date: date
    blocks: tuple[DraftScheduledBlock, ...]
    free_blocks: tuple[TimeInterval, ...]
    busy_blocks: tuple[BusyBlock, ...]
    warnings: tuple[str, ...]
    generated_at: datetime
    is_active: bool = field(default=False, init=False)


class OrchestratorState(StrEnum):
    """Goal Structuring 상태머신 단계 (architecture.md §2.1)."""

    VALIDATING = "validating"
    PLANNING = "planning"
    REVIEWING = "reviewing"
    HITL = "hitl"
    SAVING = "saving"
    DONE = "done"


# ─────────────────────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────────────────────


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=KST)


def _day_end(day: date) -> datetime:
    """day 의 끝(= 다음 날 00:00) KST."""
    return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=KST)


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=KST)


def _parse_hhmm(raw: str) -> time:
    """\"HH:MM\" 문자열을 time 으로. 형식 오류는 ValueError.

    `"24:00"`(자정 = 하루 끝)은 흔한 사용자 입력("밤 12시까지")이지만 `time` 은 24:00 을
    담지 못한다. 이를 거부해 계획 생성이 500 으로 죽던 문제를 막기 위해 그날의 **마지막
    순간**(`time.max`)으로 표현한다 — 활동창 종료로 쓰이면 [start, 자정) 윈도우가, 수면
    정책 시작으로 쓰이면 자정 넘김이 올바르게 계산된다(`_window_intervals`).
    """
    try:
        hh_s, mm_s = raw.split(":")
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"잘못된 시각 형식: {raw!r} (HH:MM 기대)") from exc
    if hh == 24 and mm == 0:
        return time.max  # 23:59:59.999999 — 하루 끝(자정) 표현
    return time(hh, mm)


def _payload_time(payload: Mapping[str, Any], key: str, policy_type: str) -> time:
    if key not in payload:
        raise ValueError(f"{policy_type} 정책 payload 에 '{key}' 누락")
    return _parse_hhmm(str(payload[key]))


def _window_intervals(day: date, start_t: time, end_t: time) -> list[TimeInterval]:
    """[start_t, end_t) 윈도우를 해당 day 안의 TimeInterval 로 변환.

    `start_t > end_t` 면 자정을 넘는 윈도우(예: 수면 23:00→07:00)로 간주하고
    [00:00, end_t) 와 [start_t, 24:00) 두 조각으로 나눈다.
    """
    if start_t == end_t:
        return []
    if start_t < end_t:
        return [TimeInterval(_at(day, start_t), _at(day, end_t))]
    parts: list[TimeInterval] = []
    if end_t > time(0, 0):
        parts.append(TimeInterval(_day_start(day), _at(day, end_t)))
    parts.append(TimeInterval(_at(day, start_t), _day_end(day)))
    return parts


def _merge_intervals(intervals: Sequence[TimeInterval]) -> list[TimeInterval]:
    """겹치거나 맞닿은 구간을 병합. start 오름차순으로 반환."""
    ordered = sorted((iv for iv in intervals if not iv.is_empty), key=lambda iv: iv.start)
    merged: list[TimeInterval] = []
    for iv in ordered:
        if merged and iv.start <= merged[-1].end:
            last = merged[-1]
            if iv.end > last.end:
                merged[-1] = TimeInterval(last.start, iv.end)
        else:
            merged.append(iv)
    return merged


def _spanned_dates(interval: TimeInterval) -> list[date]:
    """interval 이 걸치는 모든 날짜 (자정을 넘는 블록 대비)."""
    last = interval.end.date()
    # end 가 정확히 자정이면 그 날짜는 점유하지 않으므로 제외.
    if interval.end.time() == time(0, 0) and interval.end > interval.start:
        last = (interval.end - timedelta(microseconds=1)).date()
    days: list[date] = []
    cursor = interval.start.date()
    while cursor <= last:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _break_min(policies: Iterable[TimePolicyLike]) -> int:
    """활성 break_min 정책의 카드 간 최소 휴식 분 (없으면 0)."""
    for policy in policies:
        if policy.is_active and policy.policy_type == "break_min":
            try:
                return max(int(policy.payload.get("min_minutes", 0)), 0)
            except (TypeError, ValueError):
                return 0
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# free/busy 계산 (요구사항 1)
# ─────────────────────────────────────────────────────────────────────────────


def time_policies_to_busy(day: date, policies: Iterable[TimePolicyLike]) -> list[BusyBlock]:
    """활성 시간 정책을 해당 날짜의 busy 블록으로 전개한다.

    busy 로 전개: sleep / lunch / no_touch / late_night_block.
    break_min / custom 은 점유 구간이 아니므로 무시한다 (break_min 은 예약 시 간격으로 사용).
    """
    weekday_key = WEEKDAY_KEYS[day.weekday()]
    busy: list[BusyBlock] = []
    for policy in policies:
        if not policy.is_active:
            continue
        ptype = policy.policy_type
        payload = policy.payload
        if ptype == "sleep":
            window = _window_intervals(
                day,
                _payload_time(payload, "start_time", ptype),
                _payload_time(payload, "end_time", ptype),
            )
            busy.extend(BusyBlock(iv, "sleep", "수면") for iv in window)
        elif ptype == "lunch":
            window = _window_intervals(
                day,
                _payload_time(payload, "start_time", ptype),
                _payload_time(payload, "end_time", ptype),
            )
            busy.extend(BusyBlock(iv, "lunch", "점심") for iv in window)
        elif ptype == "no_touch":
            days_of_week = payload.get("days_of_week")
            if days_of_week is not None and weekday_key not in days_of_week:
                continue
            window = _window_intervals(
                day,
                _payload_time(payload, "start_time", ptype),
                _payload_time(payload, "end_time", ptype),
            )
            busy.extend(BusyBlock(iv, "no_touch", "노터치") for iv in window)
        elif ptype == "late_night_block":
            # 보수적 처리: 심야 시작~자정 전체를 점유로 본다. blocked_categories 별
            # 세분화는 후속 작업(카드 카테고리 인지 가드)으로 남긴다.
            start_dt = _at(day, _payload_time(payload, "start_time", ptype))
            busy.append(
                BusyBlock(TimeInterval(start_dt, _day_end(day)), "late_night_block", "심야 차단")
            )
    return busy


def fixed_schedules_to_busy(day: date, schedules: Iterable[FixedScheduleLike]) -> list[BusyBlock]:
    """고정 일정(수업·알바 등)을 해당 요일의 busy 블록으로 전개한다.

    FixedSchedule 은 "절대 침범 불가" 구역이다 (db/models/fixed_schedule.py).
    """
    weekday_key = WEEKDAY_KEYS[day.weekday()]
    busy: list[BusyBlock] = []
    for sched in schedules:
        if weekday_key not in sched.days_of_week:
            continue
        for iv in _window_intervals(day, sched.start_time, sched.end_time):
            busy.append(BusyBlock(iv, "fixed_schedule", sched.title))
    return busy


def compute_free_blocks(day: date, busy: Sequence[BusyBlock]) -> list[TimeInterval]:
    """하루 [00:00, 24:00) 에서 busy 를 제외한 가용(free) 구간을 계산한다."""
    occupied = _merge_intervals([b.interval for b in busy])
    free: list[TimeInterval] = []
    cursor = _day_start(day)
    end_of_day = _day_end(day)
    for iv in occupied:
        if iv.start > cursor:
            free.append(TimeInterval(cursor, iv.start))
        cursor = max(cursor, iv.end)
    if cursor < end_of_day:
        free.append(TimeInterval(cursor, end_of_day))
    return free


# ─────────────────────────────────────────────────────────────────────────────
# 습관 배치 (요구사항 1 — 반복 습관/루틴 반영)
# ─────────────────────────────────────────────────────────────────────────────


def _find_slot(
    free_blocks: Sequence[TimeInterval], preferred: TimeInterval, need: timedelta
) -> tuple[int, datetime] | None:
    """need 길이가 들어가고 preferred 윈도우와 겹치는 가장 이른 free 블록을 찾는다."""
    for index, iv in enumerate(free_blocks):
        start = max(iv.start, preferred.start)
        end = min(iv.end, preferred.end)
        if end - start >= need:
            return index, start
    return None


def _subtract_with_break(
    free_blocks: Sequence[TimeInterval], index: int, placed: TimeInterval, break_minutes: int
) -> list[TimeInterval]:
    """free_blocks[index] 에서 placed 구간(+ 뒤쪽 break)을 잘라낸 새 리스트를 반환."""
    gap = timedelta(minutes=max(break_minutes, 0))
    result: list[TimeInterval] = []
    for i, iv in enumerate(free_blocks):
        if i != index:
            result.append(iv)
            continue
        if placed.start > iv.start:
            result.append(TimeInterval(iv.start, placed.start))
        tail_start = min(placed.end + gap, iv.end)
        if tail_start < iv.end:
            result.append(TimeInterval(tail_start, iv.end))
    return [iv for iv in result if not iv.is_empty]


def reserve_habit_sessions(
    day: date,
    free_blocks: Sequence[TimeInterval],
    habits: Iterable[HabitLike],
    *,
    break_minutes: int = 0,
) -> tuple[list[DraftScheduledBlock], list[TimeInterval]]:
    """습관 세션을 free 블록에 배치한다 (습관당 하루 1세션).

    - priority_level 오름차순(1 = 최우선)으로 배치한다.
    - time_preference 윈도우 안에서 minutes_per_session 이 들어가는 가장 이른 슬롯을 쓴다.
    - 배치된 free 블록은 분할되며, 다음 카드와의 간격으로 break_minutes 를 띄운다.
    - 어떤 free 블록에도 못 들어가는 습관은 draft 에 포함하지 않는다 (호출자가 warning 처리).

    반환: (배치된 초안 블록들, 남은 free 블록들). 결과는 초안일 뿐 영속화되지 않는다.
    """
    remaining = [iv for iv in free_blocks if not iv.is_empty]
    placed: list[DraftScheduledBlock] = []
    for habit in sorted(habits, key=lambda h: h.priority_level):
        win_start, win_end = _TIME_PREFERENCE_WINDOWS.get(
            habit.time_preference, _TIME_PREFERENCE_WINDOWS["anytime"]
        )
        preferred = TimeInterval(_at(day, win_start), _at(day, win_end))
        need = timedelta(minutes=habit.minutes_per_session)
        slot = _find_slot(remaining, preferred, need)
        if slot is None:
            continue
        index, slot_start = slot
        block_interval = TimeInterval(slot_start, slot_start + need)
        placed.append(
            DraftScheduledBlock(
                interval=block_interval,
                origin="habit",
                origin_id=habit.id,
                title=habit.title,
                category=habit.category,
            )
        )
        remaining = _subtract_with_break(remaining, index, block_interval, break_minutes)
    return placed, remaining


# ─────────────────────────────────────────────────────────────────────────────
# 안전성 가드 (요구사항 2 — AGENTS §1/§2)
# ─────────────────────────────────────────────────────────────────────────────


class PolicyViolationError(RuntimeError):
    """초안/계획 블록이 절대 시간 정책을 위반했을 때 발생.

    SAVING 트랜잭션 안에서 발생하면 즉시 rollback 되어야 한다 (AGENTS §1/§2).
    """

    def __init__(self, block: DraftScheduledBlock, violated: BusyBlock) -> None:
        self.block = block
        self.violated = violated
        super().__init__(
            f"초안 블록 '{block.title}' "
            f"({block.interval.start.isoformat()}~{block.interval.end.isoformat()}) 가 "
            f"절대 정책 '{violated.source}'({violated.label}) 을 위반한다 — 트랜잭션을 롤백한다."
        )


def absolute_busy_blocks(day: date, policies: Iterable[TimePolicyLike]) -> list[BusyBlock]:
    """절대(위반 불가) 정책만 busy 로 전개. sleep / no_touch / late_night_block."""
    return [b for b in time_policies_to_busy(day, policies) if b.source in ABSOLUTE_POLICY_TYPES]


def guard_draft_block(block: DraftScheduledBlock, policies: Iterable[TimePolicyLike]) -> None:
    """단일 초안 블록이 절대 정책을 침범하면 `PolicyViolationError` 를 던진다."""
    policy_list = list(policies)
    for day in _spanned_dates(block.interval):
        for busy in absolute_busy_blocks(day, policy_list):
            if block.interval.overlaps(busy.interval):
                raise PolicyViolationError(block, busy)


def guard_draft_plan(plan: DraftPlan, policies: Iterable[TimePolicyLike]) -> None:
    """초안 계획의 모든 블록을 검증. 첫 위반에서 `PolicyViolationError`."""
    policy_list = list(policies)
    for block in plan.blocks:
        guard_draft_block(block, policy_list)


@asynccontextmanager
async def policy_guarded_transaction(
    session: SupportsTransaction,
    plan: DraftPlan,
    policies: Iterable[TimePolicyLike],
) -> AsyncIterator[SupportsTransaction]:
    """SAVING 단계 트랜잭션 가드 (요구사항 2).

    블록을 영속화하기 **이전에** 절대 정책 위반을 검사하고, 위반 또는 임의 예외
    발생 시 `session.rollback()` 으로 트랜잭션을 안전하게 취소한다. HITL [수락]
    이후 호출되는 단 하나의 영속화 경로다 (AGENTS §1: 자동 적용 금지).

    Usage::

        async with policy_guarded_transaction(session, plan, policies):
            ...  # scheduled_blocks INSERT 등 — 위반이 없을 때만 도달
    """
    try:
        guard_draft_plan(plan, policies)
    except PolicyViolationError:
        await session.rollback()
        raise
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise


# ─────────────────────────────────────────────────────────────────────────────
# 오케스트레이터 (요구사항 3 — Draft Layer 반환 / 요구사항 4 — 순수 규칙 기반)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GoalStructuringInput:
    """오케스트레이터 입력 — 한 날짜의 계획 생성에 필요한 Planning Layer 메모리 조각."""

    target_date: date
    time_policies: tuple[TimePolicyLike, ...]
    fixed_schedules: tuple[FixedScheduleLike, ...]
    habits: tuple[HabitLike, ...]


class GoalStructuringOrchestrator:
    """첫 계획 생성 상태머신(architecture.md §2.1)의 규칙 기반 코어.

    이 클래스는 VALIDATING/PLANNING 의 **순수 규칙 로직**만 담당한다. LLM 이
    필요한 goal 분해와 REVIEWING/HITL/SAVING 은 별도 에이전트/경로가 맡는다.
    산출물은 항상 비활성 `DraftPlan` — 자동 적용하지 않는다 (AGENTS §1·§2).
    """

    def __init__(self) -> None:
        self.state: OrchestratorState = OrchestratorState.VALIDATING

    def validate(self, payload: GoalStructuringInput) -> list[str]:
        """필수 입력 검증. 누락 항목 목록을 반환한다 (빈 목록 = 통과)."""
        missing: list[str] = []
        has_active_sleep = any(
            policy.is_active and policy.policy_type == "sleep" for policy in payload.time_policies
        )
        if not has_active_sleep:
            # DevBaseline §1.4 / AGENTS §1: 최소 1개의 활성 수면 정책 필수.
            missing.append("time_policies.sleep")
        return missing

    def build_draft_plan(self, payload: GoalStructuringInput) -> DraftPlan:
        """규칙 기반으로 하루치 free/busy + 습관 배치 초안을 만든다.

        절대 정책을 위반하는 블록은 만들지 않으며, 마지막에 `guard_draft_plan` 으로
        자기검증한다. 반환값은 비활성 초안이다 (요구사항 3).

        Raises:
            ValueError: 필수 입력(활성 수면 정책 등)이 누락된 경우.
            PolicyViolationError: 자기검증에서 정책 위반 블록이 발견된 경우 (방어적).
        """
        self.state = OrchestratorState.VALIDATING
        missing = self.validate(payload)
        if missing:
            raise ValueError(f"계획 생성 필수 입력 누락: {', '.join(missing)}")

        self.state = OrchestratorState.PLANNING
        day = payload.target_date

        busy = time_policies_to_busy(day, payload.time_policies)
        busy.extend(fixed_schedules_to_busy(day, payload.fixed_schedules))

        free = compute_free_blocks(day, busy)
        placed, remaining = reserve_habit_sessions(
            day, free, payload.habits, break_minutes=_break_min(payload.time_policies)
        )

        placed_ids = {block.origin_id for block in placed}
        warnings = tuple(
            f"습관 '{habit.title}' 을(를) 배치할 가용 슬롯이 없습니다."
            for habit in payload.habits
            if habit.id not in placed_ids
        )

        plan = DraftPlan(
            target_date=day,
            blocks=tuple(placed),
            free_blocks=tuple(remaining),
            busy_blocks=tuple(busy),
            warnings=warnings,
            generated_at=now_kst(),
        )
        # 방어적 자기검증: 절대 정책 위반 블록이 초안에 섞이지 않았는지 확인 (AGENTS §2).
        guard_draft_plan(plan, payload.time_policies)
        self.state = OrchestratorState.REVIEWING
        return plan
