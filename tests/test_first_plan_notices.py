"""첫 계획 `warnings` 문구 — 사용자가 한 말만 인용하고, 서로 모순되지 않게 (planB 묶음).

순수 함수(`first_plan_adapter.*_notice`/`*_warning`)만 본다 — DB·LLM 없음.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytest

from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.orchestrator.goal_structuring import BusyBlock, TimeInterval
from reaction_backend.orchestrator.plan_scheduler import PlanAction, schedule_actions_multiday
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)

KST = timezone(timedelta(hours=9))


def _outcome(
    *,
    weekly_hours: int | None = None,
    frequency: int | None = None,
    session_length: int | None = None,
    focus_duration: int | None = None,
    horizon: str | None = None,
    extra: list[GoalCandidate] | None = None,
    **goal: Any,
) -> InterviewOutcome:
    heaviest = GoalCandidate(
        title=goal.pop("title", "토익 900점"),
        category="study",
        is_heaviest=True,
        tentative_tier="focus",
        confidence=0.9,
        weekly_hours=weekly_hours,
        frequency_per_week=frequency,
        session_length_min=session_length,
        deadline=horizon,
        **goal,
    )
    return InterviewOutcome(
        session_id="t",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[heaviest, *(extra or [])],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["저녁"]
        ),
        preferences=PreferenceProfile(
            recovery_tone="담백",
            rest_ok=True,
            downscope_unit_min=10,
            focus_duration_min=focus_duration,
        ),
        horizon=horizon,
    )


# ── planB-7 분량 부족 경고가 말하지 않은 숫자를 인용하지 않는다 ─────────────────


def test_derived_weekly_hours_are_not_quoted_as_the_users_words() -> None:
    """'30분씩 주 3회' 만 답했으면 '주 2시간' 을 사용자 말처럼 인용하지 않는다.

    인터뷰가 주당 시간을 max(1, round(30×3/60)) = 2 로 **올려** 채운다. 예전 경고는
    "주 2시간 쓸 수 있다고 하셨는데" 로 시작했고, 올림분 30분이 허용 오차를 먹어 주 80분
    계획에도 경고가 떴다.
    """
    outcome = _outcome(weekly_hours=2, frequency=3, session_length=30)
    # 주 80분 = 사용자가 말한 90분에서 오차(30분) 안 → 경고 없음.
    assert (
        first_plan_adapter.volume_shortfall_warning(outcome, planned_minutes=80 * 4, span_days=28)
        is None
    )
    # 정말 모자라면 알리되, 사용자가 한 말(주 3회 · 30분)로 말한다.
    short = first_plan_adapter.volume_shortfall_warning(
        outcome, planned_minutes=40 * 4, span_days=28
    )
    assert short is not None
    assert "주 2시간" not in short
    assert "주 3회 30분씩" in short and "1.5시간" in short
    # '매일 30분'(유도 주 4시간) 은 '매일' 로 되읽고, 올린 4시간이 아니라 3.5시간을 기준으로 삼는다.
    daily = _outcome(weekly_hours=4, frequency=7, session_length=30)
    assert (
        first_plan_adapter.volume_shortfall_warning(daily, planned_minutes=190 * 4, span_days=28)
        is None
    )
    daily_short = first_plan_adapter.volume_shortfall_warning(
        daily, planned_minutes=120 * 4, span_days=28
    )
    assert daily_short is not None
    assert "매일 30분씩" in daily_short and "3.5시간" in daily_short
    assert "주 4시간" not in daily_short


def test_derived_weekly_hours_are_shown_with_one_decimal() -> None:
    """길이×빈도가 딱 떨어지지 않아도(20분 × 매일 = 140분) '주 2.33333시간' 으로 말하지 않는다."""
    outcome = _outcome(weekly_hours=2, frequency=7, session_length=20)
    warning = first_plan_adapter.volume_shortfall_warning(
        outcome, planned_minutes=60 * 4, span_days=28
    )
    assert warning is not None
    assert "매일 20분씩(주 2.3시간)" in warning
    assert "2.33" not in warning


def test_directly_answered_weekly_hours_are_still_quoted() -> None:
    """직접 답한 주 5시간 + 주 3회 + 1시간은 진짜 모순이라 종전대로 5시간을 인용한다."""
    outcome = _outcome(weekly_hours=5, frequency=3, session_length=60)
    warning = first_plan_adapter.volume_shortfall_warning(
        outcome, planned_minutes=180 * 4, span_days=28
    )
    assert warning is not None
    assert "주 5시간 쓸 수 있다고 하셨는데" in warning
    assert "60분이라고 하셨거든요" in warning


def test_default_focus_length_is_not_quoted_as_an_answer() -> None:
    """집중 시간을 아무 데서도 답하지 않았으면 기본값 50분을 '~라고 하셨거든요' 로 말하지 않는다."""
    outcome = _outcome(weekly_hours=6, frequency=3)
    warning = first_plan_adapter.volume_shortfall_warning(
        outcome, planned_minutes=150 * 4, span_days=28
    )
    assert warning is not None
    assert "하셨거든요" not in warning
    assert "기본값 50분" in warning
    # 전역 집중 시간을 답했으면 그건 사용자의 말이다.
    answered = _outcome(weekly_hours=6, frequency=3, focus_duration=50)
    told = first_plan_adapter.volume_shortfall_warning(
        answered, planned_minutes=150 * 4, span_days=28
    )
    assert told is not None and "50분이라고 하셨거든요" in told


# ── planB-11 · journey-9 제목 뒤에 틀린 조사를 붙이지 않고, 내부 표기를 싣지 않는다 ──────

# 따옴표로 닫힌 제목 바로 뒤에 조사가 붙은 모양 — "'운동 시작'는", "'토익 900점'을".
_PARTICLE_AFTER_TITLE = re.compile(r"'(?:은|는|을|를|이|가|을\(를\)|은\(는\))")


def _goal(title: str) -> GoalCandidate:
    return GoalCandidate(title=title, category="study", tentative_tier="focus", confidence=0.9)


@pytest.mark.parametrize(
    "notice",
    [
        pytest.param(
            lambda: first_plan_adapter.other_goals_deferred_notice(
                _outcome(extra=[_goal("토익 900점"), _goal("운동 시작")])
            ),
            id="other_goals_deferred",
        ),
        pytest.param(
            lambda: first_plan_adapter.out_of_cycle_notice(["실전 모의고사 반복 및 900점 달성"]),
            id="out_of_cycle",
        ),
        pytest.param(
            lambda: first_plan_adapter.waiting_steps_notice(["서류 합격 발표 대기 중"]),
            id="waiting_steps",
        ),
        pytest.param(lambda: first_plan_adapter.tier_park_notice(["헬스 루틴"]), id="tier_park"),
        pytest.param(
            lambda: first_plan_adapter.missing_milestones_notice(["기초 문법 정복"], confirmed=3),
            id="missing_milestones",
        ),
    ],
)
def test_title_lists_never_take_a_particle(notice: Any) -> None:
    """받침 있는 제목('운동 시작'·'900점 달성'·'대기 중') 뒤에 '는' 이 붙어 나가던 회귀."""
    text = notice()
    assert text is not None
    assert not _PARTICLE_AFTER_TITLE.search(text), text


def test_deferred_and_out_of_cycle_notices_still_name_what_and_when() -> None:
    deferred = first_plan_adapter.other_goals_deferred_notice(
        _outcome(extra=[_goal("토익 900점"), _goal("운동 시작")])
    )
    assert deferred is not None
    assert "'운동 시작'" in deferred and "'토익 900점'" in deferred
    assert "다음 계획" in deferred
    later = first_plan_adapter.out_of_cycle_notice(["운동 시작"])
    assert later is not None and "'운동 시작'" in later and "이어지는 주기" in later


def test_tier_park_notice_uses_screen_words_not_internal_codes() -> None:
    notice = first_plan_adapter.tier_park_notice(["헬스 루틴"])
    assert notice is not None
    for internal in ("Focus", "Maintain", "parked", "/"):
        assert internal not in notice, notice
    assert "보류" in notice


def test_format_title_list_folds_after_three() -> None:
    assert first_plan_adapter.format_title_list(["a"]) == "'a'"
    assert (
        first_plan_adapter.format_title_list(["a", "b", "c", "d", "e"]) == "'a' · 'b' · 'c' 외 2개"
    )


def test_unplaced_scheduler_line_has_no_developer_particle() -> None:
    """스케줄러의 배치 실패 줄도 "'…' 을(를)" 을 쓰지 않는다 — 마커 문장은 그대로."""
    day = date(2026, 9, 22)
    whole_day = BusyBlock(
        TimeInterval(
            datetime.combine(day, time(0, 0), tzinfo=KST),
            datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=KST),
        ),
        "policy",
        "하루 종일",
    )
    _, warnings = schedule_actions_multiday(
        start_day=day,
        horizon_day=day,
        actions=[
            PlanAction(
                id=uuid.uuid4(),
                node_id="n",
                title="단어 암기",
                category="study",
                estimated_minutes=30,
            )
        ],
        busy_for_day=lambda _d: [whole_day],
        peak_windows=[],
        focus_chunk_min=60,
        break_min=10,
        daily_focus_cap_min=180,
    )
    assert warnings, "하루가 전부 막혀 있으니 배치 실패 줄이 있어야 한다"
    assert all("을(를)" not in w for w in warnings), warnings
    assert any("'단어 암기'" in w and "배치할 가용 시간을 찾지 못했어요" in w for w in warnings)


def test_plan_notices_say_dates_the_way_people_do() -> None:
    """'2026-10-15 까지고' 같은 ISO 표기 대신 '10월 15일까지' — 해가 다르면 해도 붙인다."""
    iso = re.compile(r"\d{4}-\d{2}-\d{2}")
    start = date(2026, 9, 18)
    far = first_plan_adapter.horizon_coverage_notice(
        _outcome(horizon="2027-08-30"),
        last_planned_day=date(2026, 10, 15),
        target_date=start,
    )
    assert far is not None and not iso.search(far), far
    assert "10월 15일까지" in far and "2027년 8월 30일" in far
    overdue = first_plan_adapter.overdue_deadline_notice(
        "2026-09-10", start_day=start, last_planned_day=date(2026, 9, 24)
    )
    assert overdue is not None and not iso.search(overdue) and "9월 10일" in overdue
    assert first_plan_adapter.ko_date("not-a-date") == "not-a-date"


# ── planB-9 '마감까지 채웠다' 와 '이번 계획은 4주 뒤까지' 가 한 화면에서 부딪치지 않는다 ──


def test_coverage_extended_warning_does_not_claim_a_far_deadline() -> None:
    """마감이 지평(4주)보다 멀면 보충은 지평까지만이라 마감 날짜로 말하지 않는다."""
    start = date(2026, 9, 18)
    far = first_plan_adapter.coverage_extended_warning(
        8, "2027-08-30", max_weeks=4, target_date=start
    )
    assert far is not None
    assert "2027" not in far and "8월 30일" not in far
    assert "이번 계획 구간(4주)" in far
    # 마감이 지평 안이면 그 마감까지 채운 게 맞다 — 날짜로 말한다.
    near = first_plan_adapter.coverage_extended_warning(
        3, "2026-10-02", max_weeks=4, target_date=start
    )
    assert near is not None and "10월 2일까지 채우려고" in near
    # 만다라 유래(2주) 목표에 3주 뒤 마감이면 역시 지평 기준.
    mandala = first_plan_adapter.coverage_extended_warning(
        3, "2026-10-09", max_weeks=2, target_date=start
    )
    assert mandala is not None and "이번 계획 구간(2주)" in mandala
