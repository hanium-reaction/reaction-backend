"""민감도 검증 자체를 검증한다 — **조건이 실제로 배치를 바꾸는가.**

⚠️ 민감도 검사가 **한 번도 안 터지면 아무것도 증명하지 못한다.** 합성 달력이 조용히
아무 일도 안 하고 있어도 "지표가 둔감하다" 로 보인다. 그래서 이 파일은 두 가지를 지킨다:

1. **기본값은 지금까지와 같다** — `calendar` 를 안 주면 배치가 바뀌지 않는다(회귀 방지)
2. **조건이 실제로 배치를 바꾼다** — 안 바뀌면 민감도 결론이 무효다
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from scripts import l1_7_schedule_eval as sched
from scripts import l1_7_schedule_stress as stress

_TODAY = date(2026, 9, 3)


def _case(case_id: str) -> dict[str, Any]:
    for row in sched.load_decompose_cases():
        if row["case_id"] == case_id:
            return row
    raise AssertionError(f"골든셋에 {case_id} 가 없다")


def _plan(case: dict[str, Any]) -> list[dict[str, Any]]:
    return sched.rule_only_plan(sched.build_outcome(case, today=_TODAY), today=_TODAY)


# ── 1. 기본값 회귀 — calendar 를 안 주면 아무것도 안 바뀐다 ─────────────────


def test_default_placement_is_unchanged_by_the_new_parameter() -> None:
    """`calendar=None` 이 기존 동작과 **같은 결과**를 낸다.

    민감도 배선을 넣으면서 기본 경로를 바꾸면 지금까지의 M20·M21·M22 수치가 조용히
    달라진다. 빈 달력(`BusyCalendar()`)과도 같아야 한다.
    """
    case = _case("normal-toeic-near")
    outcome = sched.build_outcome(case, today=_TODAY)
    items = _plan(case)
    a, _, _, _ = sched.place(items, outcome, today=_TODAY)
    b, _, _, _ = sched.place(items, outcome, today=_TODAY, calendar=sched.BusyCalendar())
    assert [x.interval.start for x in a] == [x.interval.start for x in b]


def test_empty_calendar_blocks_nothing() -> None:
    cal = sched.BusyCalendar()
    assert cal.busy_on(_TODAY) == []
    assert cal.committed_min_by_day(_TODAY, _TODAY + timedelta(days=7)) == {}


# ── 2. 달력이 실제로 무언가를 한다 ──────────────────────────────────────────


def test_busy_calendar_actually_blocks_time() -> None:
    cal = sched.BusyCalendar(busy_minutes_per_day=120, busy_start_hour=9)
    blocks = cal.busy_on(_TODAY)
    assert len(blocks) == 1
    iv = blocks[0].interval
    assert iv.start.hour == 9
    assert (iv.end - iv.start).total_seconds() / 60 == 120


def test_skip_weekday_leaves_that_day_free() -> None:
    """일요일만 비우는 조건이 실제로 일요일만 비우는가."""
    cal = sched.BusyCalendar(busy_minutes_per_day=480, skip_weekday=6)
    sunday = _TODAY + timedelta(days=(6 - _TODAY.weekday()) % 7)
    assert cal.busy_on(sunday) == []
    assert cal.busy_on(sunday + timedelta(days=1)) != []


def test_committed_minutes_cover_the_window() -> None:
    cal = sched.BusyCalendar(busy_minutes_per_day=90)
    got = cal.committed_min_by_day(_TODAY, _TODAY + timedelta(days=3))
    assert len(got) == 4
    assert set(got.values()) == {90}


# ── 3. 민감도 결론이 공허하지 않은가 ────────────────────────────────────────


def test_a_stress_condition_changes_placement() -> None:
    """조건이 **실제로 배치를 바꾼다.**

    안 바뀌면 "지표가 둔감하다" 는 결론이 무효다 — 달력이 아무 일도 안 한 것이다.
    """
    case = _case("normal-toeic-near")
    outcome = sched.build_outcome(case, today=_TODAY)
    items = _plan(case)
    base, _, _, _ = sched.place(items, outcome, today=_TODAY)
    hard, _, _, _ = sched.place(
        items,
        outcome,
        today=_TODAY,
        calendar=sched.BusyCalendar(busy_minutes_per_day=480, skip_weekday=6),
    )
    base_days = {b.interval.start.date() for b in base}
    hard_days = {b.interval.start.date() for b in hard}
    assert hard_days != base_days, "스트레스 조건이 배치를 전혀 안 바꿨다 — 민감도 결론이 무효"
    assert len(hard_days) < len(base_days), "일요일만 비었는데 배치 날짜가 안 줄었다"


def test_m21_can_actually_fail_under_a_strong_enough_condition() -> None:
    """**M21 이 발화할 수 있는가.**

    한 번도 안 터지는 지표는 "통과" 가 아무 뜻이 없다. 극단 조건에서 실패가 나와야
    baseline 의 34/34 를 "잡을 수 있는데 안 잡혔다" 로 읽을 수 있다.
    """
    names = {s.name for s in stress.SCENARIOS}
    assert "busy-extreme" in names, "M21 발화를 확인할 극단 조건이 사라졌다"
    r = stress.run(_TODAY)
    extreme = r["by_scenario"]["busy-extreme"]["m21"]
    assert extreme[0] < extreme[1], "가장 강한 조건에서도 M21 이 한 건도 실패하지 않았다"


def test_m20_detects_cramming_that_m21_misses() -> None:
    """**두 지표가 다른 것을 본다** — 이 실행의 핵심 발견.

    일요일만 비우면 세션이 전부 배치되지만(M21 통과) 하루에 몰린다(M20 실패).
    프로덕션 `cadence_shortfall_notice` docstring 이 적은 실제 사고가 그 모양이었다 —
    *"'매일' 이라고 답했는데 초반 격일 / 후반 하루 2세션(8시간)"*.
    """
    r = stress.run(_TODAY)
    sunday = r["by_scenario"]["busy-except-sunday"]
    base = r["by_scenario"]["baseline"]
    assert sunday["m21"][0] == sunday["m21"][1], "이 조건에서는 전부 배치돼야 한다"
    assert sunday["m20"][0] < base["m20"][0], "케이던스가 무너졌는데 M20 이 안 잡았다"


def test_baseline_scenario_matches_the_plain_eval() -> None:
    """스트레스 러너의 `baseline` 이 기존 하네스와 **같은 조건**인가."""
    assert stress.SCENARIOS[0].name == "baseline"
    assert stress.SCENARIOS[0].calendar == sched.BusyCalendar()


@pytest.mark.parametrize("name", ["baseline", "busy-light", "busy-heavy", "busy-except-sunday"])
def test_every_scenario_is_documented(name: str) -> None:
    """시나리오마다 **왜 그 조건인지**가 적혀 있어야 한다."""
    sc = next(s for s in stress.SCENARIOS if s.name == name)
    assert sc.why.strip(), f"{name} 에 사유가 없다"
