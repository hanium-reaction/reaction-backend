"""다일 계획 스케줄러 단위 테스트 (`orchestrator/plan_scheduler.py`).

핵심 회귀 방지: 마감까지 여러 날이 있는데도 첫날 하루에 모든 카드가 몰리던 버그.
"""

from __future__ import annotations

import uuid
from datetime import date, time

from reaction_backend.orchestrator.goal_structuring import (
    BusyBlock,
    time_policies_to_busy,
)
from reaction_backend.orchestrator.plan_scheduler import (
    PlanAction,
    PlanWindow,
    schedule_actions_multiday,
)

START = date(2026, 7, 8)


def _action(title: str, minutes: int) -> PlanAction:
    return PlanAction(
        id=uuid.uuid4(), node_id=title, title=title, category="study", estimated_minutes=minutes
    )


def _hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _active_window_busy(day: date, start: time, end: time) -> list[BusyBlock]:
    """활동창 밖(수면)을 busy 로 — 활동창 [start,end) 만 free 로 남긴다."""
    policies = [
        type(
            "P",
            (),
            {
                "policy_type": "sleep",
                "payload": {"start_time": _hhmm(end), "end_time": _hhmm(start)},
                "is_active": True,
            },
        )()
    ]
    return time_policies_to_busy(day, policies)


def _busy_09_2330(day: date) -> list[BusyBlock]:
    return _active_window_busy(day, time(9, 0), time(23, 30))


def test_spreads_across_days_instead_of_cramming_one_day() -> None:
    """8개 × 50분 = 400분. cap 180 → 하루 최대 3개꼴 → 3일 이상에 분산."""
    actions = [_action(f"작업{i}", 50) for i in range(8)]
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=date(2026, 7, 21),
        actions=actions,
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert not warnings
    assert len(blocks) == 8
    days_used = {b.interval.start.date() for b in blocks}
    # 첫날에 다 몰리지 않는다 — 최소 3일에 걸쳐 분산.
    assert len(days_used) >= 3
    # 하루 집중 총량 상한(180분)을 넘지 않는다.
    for day in days_used:
        total = sum(b.interval.duration_minutes for b in blocks if b.interval.start.date() == day)
        assert total <= 180


def test_respects_peak_window_preference() -> None:
    """피크=오후(12~18)면 첫 블록이 활동창 시작(09:00)이 아니라 12:00 이후에 놓인다."""
    actions = [_action("집중 작업", 50)]
    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,
        actions=actions,
        busy_for_day=_busy_09_2330,
        peak_windows=[PlanWindow(time(12, 0), time(18, 0))],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert len(blocks) == 1
    assert blocks[0].interval.start.time() >= time(12, 0)


def test_inserts_breaks_between_blocks_same_day() -> None:
    """같은 날 연속 블록 사이에 break_min 만큼 간격이 있다."""
    actions = [_action("A", 30), _action("B", 30)]
    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,
        actions=actions,
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=15,
        daily_focus_cap_min=180,
    )
    same_day = sorted(
        (b for b in blocks if b.interval.start.date() == START), key=lambda b: b.interval.start
    )
    assert len(same_day) == 2
    gap_min = (same_day[1].interval.start - same_day[0].interval.end).total_seconds() / 60
    assert gap_min >= 15


def test_splits_long_action_into_sessions() -> None:
    """focus_chunk 50분인데 120분 카드 → 여러 세션으로 분할되고 제목에 (i/n) 표기."""
    actions = [_action("긴 작업", 120)]
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=date(2026, 7, 12),
        actions=actions,
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=50,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert not warnings
    assert len(blocks) >= 2
    assert all("(" in b.title for b in blocks)
    # 세션 합이 원래 추정 분과 일치.
    assert sum(b.interval.duration_minutes for b in blocks) == 120


def test_no_free_time_yields_warnings_not_crash() -> None:
    """활동창이 아주 좁아 배치할 수 없으면 warnings 로 남는다 (예외 X)."""
    actions = [_action("큰 작업", 120)]

    def tiny_window(day: date) -> list[BusyBlock]:
        # 09:00~09:30 만 free (30분) — 120분 카드는 못 들어감.
        return _active_window_busy(day, time(9, 0), time(9, 30))

    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,
        actions=actions,
        busy_for_day=tiny_window,
        peak_windows=[],
        focus_chunk_min=120,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert not blocks
    assert warnings


def test_blocks_stay_within_free_and_are_ordered() -> None:
    """모든 블록이 활동창 안(09:00~23:30)이고 시간순으로 정렬돼 반환된다."""
    actions = [_action(f"작업{i}", 45) for i in range(6)]
    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=date(2026, 7, 15),
        actions=actions,
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )
    for b in blocks:
        assert time(9, 0) <= b.interval.start.time()
        assert b.interval.end.time() <= time(23, 30)
    starts = [b.interval.start for b in blocks]
    assert starts == sorted(starts)
