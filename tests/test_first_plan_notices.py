"""첫 계획 `warnings` 문구 — 사용자가 한 말만 인용하고, 서로 모순되지 않게 (planB 묶음).

순수 함수(`first_plan_adapter.*_notice`/`*_warning`)만 본다 — DB·LLM 없음.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from reaction_backend.orchestrator import first_plan_adapter
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
