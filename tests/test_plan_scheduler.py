"""다일 계획 스케줄러 단위 테스트 (`orchestrator/plan_scheduler.py`).

핵심 회귀 방지: 마감까지 여러 날이 있는데도 첫날 하루에 모든 카드가 몰리던 버그.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from reaction_backend.orchestrator.first_plan import _schedule_end
from reaction_backend.orchestrator.goal_structuring import (
    BusyBlock,
    TimeInterval,
    _parse_hhmm,
    compute_free_blocks,
    time_policies_to_busy,
)
from reaction_backend.orchestrator.plan_scheduler import (
    PlanAction,
    PlanWindow,
    schedule_actions_multiday,
)
from reaction_backend.schemas.common import KST

START = date(2026, 7, 8)  # 수요일


def _dt(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=KST)


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


def test_fills_available_time_when_over_comfort_cap() -> None:
    """마감이 임박해(단일일) 일이 편안한 상한(180분)보다 많으면, 편안한 상한에서 멈추지 않고
    비피크 가용 시간까지 채워 세션을 드롭하지 않는다(#fill-available).

    이전: cap 180 에 닿으면 그 날을 건너뛰어 400분 중 ~180분만 배치, 나머지는 경고로 버려짐.
    지금: 1차(상한 내 분산) 후 남은 세션을 2차로 남은 free 에 상한 무시하고 채운다.
    """
    actions = [_action(f"작업{i}", 50) for i in range(8)]  # 400분 > cap 180
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,  # 단 하루 — 마감 임박
        actions=actions,
        busy_for_day=_busy_09_2330,  # 09:00~23:30 free (충분)
        peak_windows=[PlanWindow(time(18, 0), time(20, 0))],  # 피크 저녁 2h 뿐
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert not warnings  # 전부 배치 — 상한에 막혀 버려지지 않음
    assert len(blocks) == 8
    total = sum(b.interval.duration_minutes for b in blocks)
    assert total == 400  # 하루에 400분(>180)까지 담김 = 비피크 시간까지 활용
    # 피크 우선은 유지 — 적어도 첫 세션은 피크(18:00~) 안에서 시작.
    assert any(time(18, 0) <= b.interval.start.time() < time(20, 0) for b in blocks)


def test_relaxed_deadline_still_respects_comfort_cap() -> None:
    """여유 있는 마감(여러 날)이면 1차 분산만으로 다 담겨 편안한 상한(180분/일)을 지킨다."""
    actions = [_action(f"작업{i}", 50) for i in range(6)]  # 300분, 14일에 여유
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=date(2026, 7, 22),
        actions=actions,
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert not warnings
    days_used = {b.interval.start.date() for b in blocks}
    for day in days_used:
        total = sum(b.interval.duration_minutes for b in blocks if b.interval.start.date() == day)
        assert total <= 180  # 여유로우면 하루 상한 유지(고르게 분산)


def test_spreads_toward_deadline_not_front_loaded() -> None:
    """마감까지 **균등 분산** — 첫 며칠에 몰리지 않고 마지막 세션이 호라이즌 후반부에 놓인다.

    front-fill(오늘부터 cap 채우기)이면 6×50분=300분이 앞 2일에 끝나 뒤가 텅 빈다.
    stride 분산이면 세션이 [start, horizon] 전 구간에 흩어진다.
    """
    actions = [_action(f"작업{i}", 50) for i in range(6)]
    horizon = date(2026, 7, 22)  # START(7/8) 기준 +14일
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=horizon,
        actions=actions,
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert not warnings
    assert len(blocks) == 6
    days = sorted({b.interval.start.date() for b in blocks})
    # 마지막 세션이 호라이즌 후반부(중간 이후)에 — front-fill 이면 전부 7/8~7/9 에 몰린다.
    assert max(days) >= date(2026, 7, 17)
    # 시작~끝이 최소 한 주 이상 벌어져 있다(넓게 분산).
    assert (max(days) - min(days)).days >= 7


def test_split_session_labels_ascend_with_time() -> None:
    """분할 세션 (i/n) 라벨이 실제 시각 순서와 일치 — 뒤 세션이 이른 슬롯으로 폴백해도
    '(2/2)'가 '(1/2)' 앞에 오지 않는다(캘린더 시각순 렌더 역전 방지, #118).

    단일일·심야 피크(22:00~) + 100분(50+50) 카드: (첫 배치)는 22:00 피크, (두 번째)는 피크
    잔여 부족 → 이른 슬롯 폴백. 라벨은 정렬 후 부여하므로 09:00=(1/2), 22:00=(2/2).
    """
    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,  # 단일일
        actions=[_action("긴 카드", 100)],
        busy_for_day=_busy_09_2330,
        peak_windows=[PlanWindow(time(22, 0), time(23, 59))],
        focus_chunk_min=50,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert len(blocks) == 2
    # 반환 순서 = 시각 오름차순, 라벨도 그 순서대로 (1/2)→(2/2).
    starts = [b.interval.start for b in blocks]
    assert starts == sorted(starts)
    assert "(1/2)" in blocks[0].title
    assert "(2/2)" in blocks[1].title


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


def test_fits_around_existing_busy_block() -> None:
    """이미 잡힌 일정(13:00~15:00)이 busy 로 들어오면 그 구간을 피해 배치한다 (비파괴 fit-around)."""
    occupied = BusyBlock(
        TimeInterval(_dt(START, 13, 0), _dt(START, 15, 0)), "scheduled_block", "기존"
    )

    def busy(day: date) -> list[BusyBlock]:
        base = list(_busy_09_2330(day))
        if day == START:
            base.append(occupied)
        return base

    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,
        actions=[_action("새 카드", 50)],
        busy_for_day=busy,
        peak_windows=[PlanWindow(time(12, 0), time(18, 0))],  # 오후 피크
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert len(blocks) == 1
    iv = blocks[0].interval
    # 기존 일정(13~15)과 겹치지 않아야 한다.
    assert not (iv.start < _dt(START, 15, 0) and _dt(START, 13, 0) < iv.end)


# ── scope 종료일 계산 (first_plan._schedule_end) ─────────────────────────────


def test_schedule_end_week_bounds_to_sunday() -> None:
    # 2026-07-08(수) 이 속한 주의 일요일 = 2026-07-12. 마감이 더 멀어도 이번 주까지만.
    assert _schedule_end(date(2026, 7, 8), "2026-07-21", "week") == date(2026, 7, 12)


def test_schedule_end_week_caps_at_earlier_deadline() -> None:
    # 마감(7/10)이 이번 주 일요일(7/12)보다 이르면 마감으로 캡.
    assert _schedule_end(date(2026, 7, 8), "2026-07-10", "week") == date(2026, 7, 10)


def test_schedule_end_horizon_uses_deadline() -> None:
    assert _schedule_end(date(2026, 7, 8), "2026-07-21", "horizon") == date(2026, 7, 21)


def test_schedule_end_horizon_without_deadline_is_single_day() -> None:
    assert _schedule_end(date(2026, 7, 8), None, "horizon") == date(2026, 7, 8)


# ── 자정(24:00) 시각 파싱 — "밤 12시까지" 활동창이 500 으로 죽던 버그 ──────────────


def test_parse_hhmm_accepts_2400_as_end_of_day() -> None:
    """'24:00'(자정)은 time 이 못 담으므로 그날의 마지막 순간(time.max)으로 표현."""
    assert _parse_hhmm("24:00") == time.max  # 23:59:59.999999
    assert _parse_hhmm("09:30") == time(9, 30)
    assert _parse_hhmm("00:00") == time(0, 0)


def test_parse_hhmm_rejects_other_out_of_range() -> None:
    """24:00 만 특례 — 24:30·25:00 등 진짜 무효값은 여전히 ValueError."""
    import pytest

    for bad in ("24:30", "25:00", "9am", "10"):
        with pytest.raises(ValueError):
            _parse_hhmm(bad)


def _sleep_policy(start_hhmm: str, end_hhmm: str) -> object:
    return type(
        "P",
        (),
        {
            "policy_type": "sleep",
            "payload": {"start_time": start_hhmm, "end_time": end_hhmm},
            "is_active": True,
        },
    )()


def test_activity_window_until_midnight_does_not_crash() -> None:
    """활동창 09:00~24:00(밤 12시) → 수면정책 start_time='24:00' 이어도 500 없이 배치.

    회귀: `_parse_hhmm('24:00')` 이 ValueError 를 던져 계획 생성이 통째로 500 이던 버그.
    자정까지 활동창이면 free 구간이 09:00~자정 근처로 잡혀야 한다.
    """
    # 활동창 [09:00, 24:00) 밖 = 수면 [24:00 → 09:00] (start=활동종료, end=활동시작).
    busy = time_policies_to_busy(START, [_sleep_policy("24:00", "09:00")])
    free = compute_free_blocks(START, busy)
    assert free, "자정까지 활동창이면 free 구간이 있어야 한다"
    # free 최대 구간은 09:00 에 시작해 그날 끝 근처까지.
    span = max(free, key=lambda iv: iv.duration_minutes)
    assert span.start.time() == time(9, 0)
    assert span.end.time() >= time(23, 59)
