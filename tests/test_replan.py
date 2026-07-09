"""주간 forward 재계획 오케스트레이터 단위 테스트 (`orchestrator/replan.py`).

순수 로직만 검증(DB 무관): 다음 주부터 재배치 / 기존 action_id 재사용 / 확정 일정 회피.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from reaction_backend.orchestrator.goal_structuring import (
    BusyBlock,
    TimeInterval,
    time_policies_to_busy,
)
from reaction_backend.orchestrator.plan_scheduler import PlanWindow
from reaction_backend.orchestrator.replan import (
    ReplanCandidate,
    ReplanTuning,
    build_forward_replan,
    day_bounds_kst,
    next_week_start,
)
from reaction_backend.schemas.common import KST

WED = date(2026, 7, 8)  # 수요일
NEXT_MON = date(2026, 7, 13)  # 그 다음 주 월요일

_TUNING = ReplanTuning(
    peak_windows=(PlanWindow(time(12, 0), time(18, 0)),),
    focus_chunk_min=60,
    break_min=10,
    daily_focus_cap_min=180,
)


def _dt(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=KST)


def _sleep_busy(days: list[date]) -> list[BusyBlock]:
    """각 날짜에 수면(23:00~08:00) busy — 활동창(08~23)만 free 로 남긴다."""
    policy = type(
        "P",
        (),
        {
            "policy_type": "sleep",
            "payload": {"start_time": "23:00", "end_time": "08:00"},
            "is_active": True,
        },
    )()
    out: list[BusyBlock] = []
    for d in days:
        out.extend(time_policies_to_busy(d, [policy]))
    return out


def _candidate(title: str, minutes: int) -> ReplanCandidate:
    return ReplanCandidate(
        action_id=uuid.uuid4(), title=title, category="study", estimated_minutes=minutes
    )


def test_next_week_start_is_following_monday() -> None:
    assert next_week_start(WED) == NEXT_MON
    assert next_week_start(date(2026, 7, 13)) == date(2026, 7, 20)  # 월요일 → 다음 주 월요일


def test_replans_from_next_week_reusing_action_ids() -> None:
    cands = [_candidate(f"남은 작업{i}", 50) for i in range(3)]
    window_days = [NEXT_MON, date(2026, 7, 14), date(2026, 7, 15)]
    blocks, warnings = build_forward_replan(
        window_start=NEXT_MON,
        horizon_day=date(2026, 7, 17),
        candidates=cands,
        committed_busy=_sleep_busy(window_days + [date(2026, 7, 16), date(2026, 7, 17)]),
        tuning=_TUNING,
    )
    assert not warnings
    assert len(blocks) == 3
    # 모든 블록은 다음 주 월요일 이후 + 기존 action_id 를 그대로 재사용.
    src_ids = {c.action_id for c in cands}
    for b in blocks:
        assert b.start.date() >= NEXT_MON
        assert b.action_id in src_ids
    # 피크(오후) 우선 → 첫 블록은 12:00 이후.
    assert min(b.start for b in blocks).time() >= time(12, 0)


def test_avoids_committed_blocks() -> None:
    """이미 시작/확정된 일정(회의 12~17)이 busy 로 들어오면 그 구간을 피해 배치한다."""
    meeting = BusyBlock(
        TimeInterval(_dt(NEXT_MON, 12, 0), _dt(NEXT_MON, 17, 0)), "scheduled_block", "회의"
    )
    busy = [*_sleep_busy([NEXT_MON, date(2026, 7, 14)]), meeting]
    blocks, _ = build_forward_replan(
        window_start=NEXT_MON,
        horizon_day=date(2026, 7, 14),
        candidates=[_candidate("남은 작업", 50)],
        committed_busy=busy,
        tuning=_TUNING,
    )
    assert len(blocks) == 1
    iv = blocks[0]
    # 회의(12~17)와 겹치지 않아야 한다.
    assert not (iv.start < _dt(NEXT_MON, 17, 0) and _dt(NEXT_MON, 12, 0) < iv.end)


def test_empty_candidates_yield_no_blocks() -> None:
    blocks, warnings = build_forward_replan(
        window_start=NEXT_MON,
        horizon_day=date(2026, 7, 17),
        candidates=[],
        committed_busy=_sleep_busy([NEXT_MON]),
        tuning=_TUNING,
    )
    assert blocks == []
    assert warnings == []


def test_day_bounds_kst_covers_inclusive_range() -> None:
    start_dt, end_dt = day_bounds_kst(NEXT_MON, date(2026, 7, 15))
    assert start_dt == _dt(NEXT_MON, 0, 0)
    assert end_dt == _dt(date(2026, 7, 16), 0, 0)  # 15일 포함 → 16일 00:00 미만
