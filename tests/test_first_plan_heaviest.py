"""'이번 계획이 다루는 목표' 를 고르는 규칙이 한 곳에 있다 (planB-18).

같은 `next(...)` 식이 스무 곳 넘게 복사돼 있었고, 라우트의 계획 지평 판정만
`is_heaviest` 가 없을 때 None 으로 떨어져 계획(첫 목표)과 다른 목표를 봤다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.api.routes import planning
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


def _outcome(*goals: GoalCandidate) -> InterviewOutcome:
    return InterviewOutcome(
        session_id="t",
        generated_at=datetime.now(KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=list(goals),
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["저녁"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True),
        horizon=None,
    )


def _goal(title: str, *, heaviest: bool = False) -> GoalCandidate:
    return GoalCandidate(
        title=title,
        category="study",
        is_heaviest=heaviest,
        tentative_tier="focus",
        confidence=0.9,
    )


def test_heaviest_goal_prefers_flag_then_first() -> None:
    assert (
        first_plan_adapter.heaviest_goal(_outcome(_goal("a"), _goal("b", heaviest=True))).title
        == "b"
    )
    assert first_plan_adapter.heaviest_goal(_outcome(_goal("a"), _goal("b"))).title == "a"
    # 경계 스키마는 목표 1개 이상을 강제하지만, 검증을 건너뛴 값에도 종전과 같이 군다.
    empty = _outcome(_goal("a")).model_copy(update={"core_goals": []})
    assert first_plan_adapter.heaviest_goal_or_none(empty) is None
    with pytest.raises(IndexError):
        first_plan_adapter.heaviest_goal(empty)


async def test_plan_horizon_follows_the_goal_the_plan_actually_covers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_heaviest 표시가 없고 첫 목표가 만다라 승격 목표면 2주 상한이 걸린다(예전엔 4주)."""

    async def promoted(_session: Any, _user_id: Any) -> set[str]:
        return {"건강한 몸 만들기"}

    monkeypatch.setattr(planning.mandala_adapter, "fetch_promoted_goal_titles_for_user", promoted)
    outcome = _outcome(_goal("건강한 몸 만들기"), _goal("토익"))

    weeks = await planning._max_plan_weeks(None, uuid4(), outcome)  # type: ignore[arg-type]

    assert weeks == first_plan_adapter.max_plan_weeks_for(is_mandala_derived=True) == 2
