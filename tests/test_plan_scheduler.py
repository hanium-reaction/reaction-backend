"""다일 계획 스케줄러 단위 테스트 (`orchestrator/plan_scheduler.py`).

핵심 회귀 방지: 마감까지 여러 날이 있는데도 첫날 하루에 모든 카드가 몰리던 버그.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta

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


def test_shifts_later_within_peak_when_slot_taken_by_another_goal() -> None:
    """선호 시간대 앞자락이 **다른 목표의 계획**으로 차 있으면, 같은 창 안에서 뒤로 민다.

    회귀 배경: 예전엔 같은 날 시작한 다른 목표의 블록을 busy 에서 빼버려서(#118 의 날짜 기반
    제외) 남의 슬롯 한복판에 겹쳐 배치됐다(실측: MVP 17:15~19:15 위에 토익 18:15~18:49).
    이제 다른 목표 블록은 busy 로 들어오고, 스케줄러는 '저녁'이라는 선호를 지키면서 시작
    시각만 늦춘다 — 18시가 막혀 있으면 20~21시.

    폴백(창 밖 가장 이른 free)으로 새 나가 **오전**에 잡히면 안 된다. 선호 시간대는 사용자가
    고른 값이라, 겹침 회피의 대가로 조용히 버려지면 안 된다.
    """
    evening = PlanWindow(start=time(18, 0), end=time(23, 0))
    taken = TimeInterval(_dt(START, 18, 0), _dt(START, 19, 45))  # 다른 목표가 선점

    def busy(day: date) -> list[BusyBlock]:
        blocks = list(_busy_09_2330(day))
        if day == START:
            blocks.append(BusyBlock(taken, "scheduled_block", "기존 일정"))
        return blocks

    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,  # 하루뿐 → 다른 날로 도망갈 수 없다
        actions=[_action("토익 단어 암기", 60)],
        busy_for_day=busy,
        peak_windows=[evening],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )

    assert not warnings
    assert len(blocks) == 1
    placed = blocks[0].interval
    assert placed.start >= taken.end  # 선점 구간을 넘겨 뒤로 밀렸다
    assert placed.end <= _dt(START, 23, 0)  # 선호 창 안을 유지 (오전 폴백 아님)
    assert placed.start.hour in (19, 20, 21)  # "18시가 아니라 20~21시쯤"


# ── #190 하루 상한을 '이미 확정된 시간' 에서 이어 센다 ──────────────────────


def test_daily_cap_counts_already_committed_minutes() -> None:
    """다른 목표의 승인된 계획이 이미 하루를 채웠으면, 새 계획은 그날을 건너뛴다.

    회귀 배경: `used_by_day` 가 항상 0에서 시작해 **자기 계획 것만** 셌다. 기존 블록은
    free 시간만 깎고 상한엔 안 잡혀서, 목표가 늘면 각 계획이 저마다 상한을 지켜도 합계는
    아무도 지키지 않았다 — 실측(목표 3개)에서 하루 240분(상한 180분).
    """
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=START + timedelta(days=3),
        actions=[_action("새 카드", 60)],
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
        committed_min_by_day={START: 150},  # 이미 150분 확정 → 60분 더하면 210 > 180
    )

    assert not warnings
    assert len(blocks) == 1
    assert blocks[0].interval.start.date() != START  # 상한에 막혀 다음 날로


def test_committed_minutes_do_not_block_when_room_remains() -> None:
    """확정 시간이 있어도 상한 안에 들어가면 그날에 그대로 배치한다(과잉 회피 방지)."""
    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=START + timedelta(days=3),
        actions=[_action("새 카드", 60)],
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
        committed_min_by_day={START: 60},  # 60 + 60 = 120 ≤ 180
    )
    assert blocks[0].interval.start.date() == START


# ── #191 다른 목표의 계획과 딱 붙지 않게 ───────────────────────────────────


def test_keeps_break_from_existing_blocks_when_day_is_roomy() -> None:
    """하루가 넉넉하면 기존 블록 끝 정각이 아니라 휴식 뒤에 시작한다.

    회귀 배경: 계획 **안에서는** 카드 사이 `break_min` 휴식을 두면서 다른 목표의 계획과는
    0분으로 붙었다 — 실측 19:15~21:15 → 21:15~22:15 → 22:15~23:15, 4시간 연속.
    """
    existing = TimeInterval(_dt(START, 18, 0), _dt(START, 19, 0))

    def hard(day: date) -> list[BusyBlock]:
        b = list(_busy_09_2330(day))
        if day == START:
            b.append(BusyBlock(existing, "scheduled_block", "기존 일정"))
        return b

    def roomy(day: date) -> list[BusyBlock]:
        b = list(_busy_09_2330(day))
        if day == START:
            padded = TimeInterval(
                existing.start - timedelta(minutes=20), existing.end + timedelta(minutes=20)
            )
            b.append(BusyBlock(padded, "scheduled_block", "기존 일정"))
        return b

    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,
        actions=[_action("새 카드", 60)],
        busy_for_day=hard,
        peak_windows=[PlanWindow(start=time(18, 0), end=time(23, 0))],
        focus_chunk_min=60,
        break_min=20,
        daily_focus_cap_min=180,
        roomy_busy_for_day=roomy,
    )

    assert len(blocks) == 1
    assert blocks[0].interval.start >= existing.end + timedelta(minutes=20)


def test_gives_up_break_rather_than_dropping_the_session() -> None:
    """하루가 빡빡하면 여백을 포기하고 붙여서라도 배치한다 — 세션을 떨어뜨리지 않는다.

    여백은 '있으면 좋은 것'이지 배치 실패의 이유가 되면 안 된다. 1차(여유 뷰)가 실패하면
    2차가 여백 없는 뷰에서 남은 자리를 채운다.
    """
    existing = TimeInterval(_dt(START, 18, 0), _dt(START, 22, 0))

    def hard(day: date) -> list[BusyBlock]:
        return [
            *_active_window_busy(day, time(18, 0), time(23, 0)),
            BusyBlock(existing, "scheduled_block", "기존 일정"),
        ]

    def roomy(day: date) -> list[BusyBlock]:
        padded = TimeInterval(
            existing.start, min(existing.end + timedelta(minutes=30), _dt(day, 23, 0))
        )
        return [
            *_active_window_busy(day, time(18, 0), time(23, 0)),
            BusyBlock(padded, "scheduled_block", "기존 일정"),
        ]

    # 22:00~23:00 만 비어 있다. 여백 30분을 지키면 60분이 안 들어간다 → 여백을 포기해야 한다.
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,
        actions=[_action("새 카드", 60)],
        busy_for_day=hard,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=30,
        daily_focus_cap_min=180,
        roomy_busy_for_day=roomy,
    )

    assert not warnings
    assert len(blocks) == 1
    assert blocks[0].interval.start == existing.end  # 붙여서라도 배치


def test_roomy_and_hard_views_never_double_book() -> None:
    """1차가 여유 뷰에서 잡은 자리를 2차가 다시 내주지 않는다(이중 배치 방지).

    두 뷰가 따로 캐시되므로, 한 배치를 **양쪽에서** 빼지 않으면 같은 시간에 두 세션이 겹친다.
    """

    def roomy(day: date) -> list[BusyBlock]:
        return list(_busy_09_2330(day))

    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=START,
        actions=[_action(f"카드{i}", 60) for i in range(6)],  # 상한 180 초과 → 2차까지 간다
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
        roomy_busy_for_day=roomy,
    )

    ordered = sorted((b.interval for b in blocks), key=lambda iv: iv.start)
    for a, b in zip(ordered, ordered[1:], strict=False):
        assert a.end <= b.start, f"겹침: {a} vs {b}"


# ── #252 자정을 넘는 활동창 ───────────────────────────────────────────────
#
# `compute_free_blocks` 는 하루 [00:00,24:00) 안에서만 free 를 계산해서, 22:00~02:00 활동창은
# 같은 날 [00:00~02:00] · [22:00~24:00] 두 조각이 됐다. 22:00→02:00 연속 4시간이 어디에도
# 없으니 2시간 초과 세션은 **구조적으로 100% 실패**했다(실측: 블록 0개 + 같은 경고 12줄).
# `_join_midnight` 이 자정에서 두 조각을 이어붙여 그 구멍을 막는다.


def _busy_22_02(day: date) -> list[BusyBlock]:
    """활동창 22:00~02:00 (자정 넘김)."""
    return _active_window_busy(day, time(22, 0), time(2, 0))


def test_midnight_window_places_session_longer_than_either_fragment() -> None:
    """3시간 세션 — 조각(각 2시간)보다 길지만 이어붙인 4시간에는 들어간다."""
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=START + timedelta(days=6),
        actions=[_action("심야작업", 180)],
        busy_for_day=_busy_22_02,
        peak_windows=[],
        focus_chunk_min=180,
        break_min=10,
        daily_focus_cap_min=240,
    )

    assert not warnings, f"배치 실패 경고가 남았다 — {warnings}"
    assert len(blocks) == 1
    iv = blocks[0].interval
    assert iv.duration_minutes == 180
    assert iv.start.hour >= 22, f"활동창 밖에서 시작 — {iv.start}"
    assert iv.end.date() == iv.start.date() + timedelta(days=1), "자정을 넘겨야 한다"
    assert iv.end.hour <= 2, f"활동창 밖에서 종료 — {iv.end}"


def test_midnight_window_does_not_double_book_the_next_morning() -> None:
    """자정을 넘긴 배치가 **다음 날 새벽 조각도 소비**해야 한다.

    회귀: day D 의 조인 뷰와 day D+1 의 본체가 같은 [D+1 00:00~02:00) 을 가리키므로, 한쪽에서만
    빼면 같은 시간이 두 번 나간다. 하루치를 단일 소스로 두고 조인을 읽기 시점 뷰로 만든 이유가
    이것 — 이 테스트가 그 계약을 고정한다.
    """
    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=START + timedelta(days=3),
        actions=[_action(f"작업{i}", 180) for i in range(4)],
        busy_for_day=_busy_22_02,
        peak_windows=[],
        focus_chunk_min=180,
        break_min=10,
        daily_focus_cap_min=240,
    )

    ordered = sorted((b.interval for b in blocks), key=lambda iv: iv.start)
    for a, b in zip(ordered, ordered[1:], strict=False):
        assert a.end <= b.start, f"자정 경계에서 이중 배치 — {a} vs {b}"


def test_normal_window_is_untouched_by_the_join() -> None:
    """자정에 안 닿는 활동창은 조인 대상이 아니다 — 일반 계획의 동작은 그대로."""
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=START + timedelta(days=6),
        actions=[_action(f"작업{i}", 60) for i in range(3)],
        busy_for_day=_busy_09_2330,
        peak_windows=[],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )

    assert not warnings
    for b in blocks:
        assert b.interval.start.date() == b.interval.end.date(), (
            f"자정을 안 넘는 창인데 날짜를 넘겼다 — {b.interval}"
        )


def test_session_longer_than_the_joined_span_still_fails() -> None:
    """이어붙여도 모자라면(4시간 창 + 5시간 세션) 여전히 실패 — 조인이 만능은 아니다."""
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=START + timedelta(days=6),
        actions=[_action("너무긴작업", 300)],
        busy_for_day=_busy_22_02,
        peak_windows=[],
        focus_chunk_min=300,
        break_min=10,
        daily_focus_cap_min=600,
    )

    assert not blocks
    assert warnings


def test_midnight_crossing_placement_is_consumed_in_both_free_views() -> None:
    """1차(여유 뷰)에서 자정을 넘겨 배치하면 **2차(빡빡한 뷰)도** 그 시간을 잃어야 한다.

    회귀(#252 + #191 상호작용): 1차는 `roomy_free_by_day` 만 캐시한다. 소비를 '시작한 날' 로만
    하면 `free_by_day[D+1]` 이 캐시되지 않은 채 남고, 나중에 `busy_for_day` 로 새로 계산되면서
    **자정을 넘긴 블록이 없던 일이 된다** — 2차가 그 새벽 시간을 다시 내주어 두 블록이 겹친다.
    두 뷰를 쓰는 건 실제 운영 경로(`first_plan` 이 `roomy_busy_for_day` 를 넘긴다)라 실전 버그다.

    cap 을 세션 하나 크기로 잡아 1차가 하루 1개만 놓고 나머지를 2차로 흘려보낸다.
    """
    blocks, _ = schedule_actions_multiday(
        start_day=START,
        horizon_day=START + timedelta(days=1),
        actions=[_action("긴작업", 180), _action("작업B", 120), _action("작업C", 120)],
        busy_for_day=_busy_22_02,
        peak_windows=[],
        focus_chunk_min=180,
        break_min=10,
        daily_focus_cap_min=180,
        roomy_busy_for_day=_busy_22_02,  # 별도 캐시를 강제 — 내용은 같아도 dict 가 다르다
    )

    ordered = sorted((b.interval for b in blocks), key=lambda iv: iv.start)
    for a, b in zip(ordered, ordered[1:], strict=False):
        assert a.end <= b.start, (
            f"자정을 넘긴 배치가 다음 날 뷰에서 안 빠져 겹쳤다 — {a.start}~{a.end} vs {b.start}~{b.end}"
        )


def test_window_ending_exactly_at_midnight_does_not_join_into_next_day_sleep() -> None:
    """자정에 **끝나되 넘지 않는** 창은 이어붙이면 안 된다 — 다음 날 수면으로 넘어간다.

    회귀: 활동창 22:00~24:00 은 day D 의 free 가 정확히 자정에서 끝난다(`tail.end == D+1 00:00`).
    '자정에 닿았다' 만 보고 이어붙이면 다음 날 free(22:00~24:00)와 붙어 `[D 22:00, D+1 24:00)`
    라는 26시간짜리 가짜 구간이 생기고, 3시간 세션이 D+1 01:00 —— **자는 시간** —— 까지 흐른다.
    그래서 '다음 날 free 가 자정부터 시작하는가' 를 함께 봐야 한다.
    """
    blocks, warnings = schedule_actions_multiday(
        start_day=START,
        horizon_day=START + timedelta(days=6),
        actions=[_action("긴작업", 180)],
        busy_for_day=lambda d: _active_window_busy(d, time(22, 0), time(0, 0)),
        peak_windows=[],
        focus_chunk_min=180,
        break_min=10,
        daily_focus_cap_min=240,
    )

    # 하루 2시간뿐인 창에 3시간 세션 → 배치 불가가 정답. 억지로 이어붙여 수면을 침범하면 안 된다.
    assert not blocks, (
        f"수면 시간대로 넘어간 배치가 생겼다 — {[(b.interval.start, b.interval.end) for b in blocks]}"
    )
    assert warnings
