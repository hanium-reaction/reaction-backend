"""study_method_agent (ADR-0010 파이프라인 1단계) — Worker Agent 단위 검증.

`aiClient.run` 만 stub 한다(`test_mandala_route.py` 와 동일 패턴). 라우터가 아직 없어
HTTP 경계 대신 agent 함수를 직접 호출한다.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.agents import study_method_agent
from reaction_backend.llm import RunResult, aiClient
from reaction_backend.schemas.interview import GoalCandidate
from reaction_backend.schemas.study_method import StudyMethodPlan


def _goal(**overrides: Any) -> GoalCandidate:
    defaults: dict[str, Any] = {
        "title": "토익 900점 달성",
        "category": "study",
        "current_level": "700점대",
        "weekly_hours": 10,
        "session_length_min": 60,
        "approach_note": None,
        "deadline": None,
        "confidence": 0.9,
    }
    return GoalCandidate(**{**defaults, **overrides})


async def test_run_returns_llm_plan_and_passes_goal_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        captured.update(kwargs)
        value = StudyMethodPlan(
            approach="RC 문법과 LC 딕테이션을 집중하는 게 효율적이에요.",
            focus_points=["RC 파트5 문법", "LC 딕테이션"],
            book_query="해커스 토익 RC 문법 기본서",
            video_query="토익 RC 문법 강의",
            material_mix="both",
        )
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    plan, fell_back = await study_method_agent.run(goal=_goal(), session=None, user_id=uuid4())

    assert fell_back is False
    assert plan.book_query == "해커스 토익 RC 문법 기본서"
    assert plan.video_query == "토익 RC 문법 강의"
    assert plan.material_mix == "both"
    assert captured["prompt_id"] == "planning/study_method"
    assert captured["module"] == "planning"
    assert captured["variables"]["title"] == "토익 900점 달성"
    assert captured["variables"]["current_level"] == "700점대"
    assert captured["variables"]["weekly_hours"] == "10시간"
    assert captured["variables"]["session_length_min"] == "60분"
    assert captured["variables"]["approach_note"] == "(없음)"
    assert captured["variables"]["deadline"] == "(없음)"


async def test_run_falls_back_to_rule_plan_on_llm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        value = kwargs["fallback"]()
        return RunResult(
            value=value,
            fell_back=True,
            reason="timeout",
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    plan, fell_back = await study_method_agent.run(
        goal=_goal(title="자바 완강"), session=None, user_id=uuid4()
    )

    assert fell_back is True
    # 룰 폴백은 목표 제목의 행위 표현을 떼지 않는다(그건 `materials.suggest_query` 의 몫) —
    # 그대로 "목차 커리큘럼" 을 붙인 결정적 질의만 낸다.
    assert plan.book_query == "자바 완강 목차 커리큘럼"
    assert plan.video_query == "자바 완강 강의"
    assert plan.focus_points == []


def test_rule_plan_handles_blank_title() -> None:
    goal = _goal(title="   ")
    plan = study_method_agent._rule_plan(goal)
    assert plan.book_query == "목표 목차 커리큘럼"
    assert plan.video_query == "목표 강의"
    # 판단 근거가 없으면 좁히지 않는다 — 사용자의 최종 선택지를 줄이지 않는다.
    assert plan.material_mix == "both"
