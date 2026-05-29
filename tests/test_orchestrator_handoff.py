"""Deep Interview(#6) → First Plan(#32) 경계 계약 + LangGraph 베이스라인 테스트.

ADR-0005 §7.3 패턴: aiClient.run 만 stub, Node 는 일반 async 함수라 직접 pytest.
- 경계 계약 InterviewOutcome 결정적 빌드 (LLM 0회) + camelCase 직렬화
- Interview Cyclic 그래프 종료 조건 4종 (순수 함수)
- 두 그래프 ainvoke end-to-end (stub 성공 path / 룰 fallback path)
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.llm import RunResult, aiClient
from reaction_backend.orchestrator import (
    first_plan,
    first_plan_adapter,
    interview,
    interview_adapter,
)
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    InterviewOutcome,
    NextQuestionSchema,
)
from reaction_backend.schemas.planning import GoalDecomposition, PlanReview

# ─────────────────────────────────────────────────────────────────────────────
# 대표 slot_answers (db/models/interview_slot_answer.py value 형식)
# ─────────────────────────────────────────────────────────────────────────────

SLOT_ANSWERS: dict[str, dict[str, Any] | None] = {
    "identity.role": {"type": "chip", "values": ["대3"]},
    "identity.season": {"type": "chip", "values": ["학기중"]},
    "identity.major": {"type": "text", "raw": "컴퓨터공학"},
    "goals.list": {"type": "text", "raw": "캡스톤, 토익", "normalized": ["캡스톤", "토익"]},
    "goals.heaviest": {"type": "text", "raw": "캡스톤"},
    "goals.deadlines": {"type": "text", "raw": "2026-06-20"},
    "goals.success_image": {"type": "text", "raw": "데모 동작"},
    "time.activity_window": {"type": "range", "start": "09:00", "end": "23:00"},
    "time.peak_window": {"type": "chip", "values": ["오전", "저녁"]},
    "time.no_touch": {"type": "chip", "values": ["일요일"]},
    "time.fixed_blocks": {"type": "text", "raw": "화목 수업", "normalized": ["화목 수업"]},
    "recovery.tone": {"type": "chip", "values": ["담백"]},
    "recovery.rest_ok": {"type": "chip", "values": ["네"]},
    "recovery.downscope_unit": {"type": "chip", "values": ["네"]},
    "energy.focus_duration": {"type": "chip", "values": ["50분"]},
}


# ─────────────────────────────────────────────────────────────────────────────
# 경계 계약 — build_outcome (LLM 0회 결정적 투영)
# ─────────────────────────────────────────────────────────────────────────────


def test_build_outcome_projects_required_slots() -> None:
    outcome = interview_adapter.build_outcome(
        session_id="iv_1",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.12,
        end_reason="completed",
        analysis_source="llm",
    )
    assert outcome.identity.role == "대3"
    assert outcome.identity.major == "컴퓨터공학"
    # heaviest 목표가 focus tier + deadline 승계
    heaviest = next(g for g in outcome.core_goals if g.is_heaviest)
    assert heaviest.title == "캡스톤"
    assert heaviest.tentative_tier == "focus"
    assert heaviest.deadline == "2026-06-20"
    assert {g.title for g in outcome.core_goals} == {"캡스톤", "토익"}
    assert outcome.availability.activity_window.start == "09:00"
    assert outcome.availability.peak_window == ["오전", "저녁"]
    assert outcome.preferences.recovery_tone == "담백"
    assert outcome.preferences.rest_ok is True
    assert outcome.preferences.focus_duration_min == 50
    assert outcome.horizon == "2026-06-20"
    assert outcome.unresolved_slots == []  # 필수 슬롯 모두 채움


def test_build_outcome_defaults_and_unresolved_when_empty() -> None:
    """early_finish/정체로 빈 슬롯 — 안전 default + unresolved_slots 기록, core_goals≥1 보장."""
    outcome = interview_adapter.build_outcome(
        session_id="iv_2",
        slot_answers={},
        ambiguity_final=0.5,
        end_reason="early_user",
        analysis_source="rule",
    )
    assert len(outcome.core_goals) >= 1  # min_length 계약 유지
    assert "goals.list" in outcome.unresolved_slots
    assert "identity.role" in outcome.unresolved_slots
    assert outcome.analysis_source == "rule"
    assert outcome.availability.activity_window.start == "09:00"  # default


def test_interview_outcome_serializes_camel_case() -> None:
    """envelope-less 도메인 객체 — camelCase 직렬화 + 역직렬화 round-trip."""
    outcome = interview_adapter.build_outcome(
        session_id="iv_3",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    dumped = outcome.model_dump(by_alias=True)
    assert "sessionId" in dumped
    assert "coreGoals" in dumped
    assert "ambiguityFinal" in dumped
    assert "isHeaviest" in dumped["coreGoals"][0]
    # generatedAt 은 KST(+09:00) ISO 8601
    json_str = outcome.model_dump_json(by_alias=True)
    assert "+09:00" in json_str
    restored = InterviewOutcome.model_validate(dumped)
    assert restored.session_id == "iv_3"


# ─────────────────────────────────────────────────────────────────────────────
# Interview Cyclic 종료 조건 4종 (순수 함수 _terminal_reason / should_continue)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"ambiguity_score": 0.2}, "completed"),  # 모호함 ≤ 0.2
        ({"ambiguity_score": 0.7, "total_turns": 15}, "turn_limit"),  # 15턴
        ({"ambiguity_score": 0.7, "early_finish": True}, "early_user"),  # 충분해요
        ({"ambiguity_score": 0.7, "stall_count": 3}, "completed"),  # 3턴 정체
        ({"ambiguity_score": 0.7}, None),  # 계속
    ],
)
def test_interview_termination_conditions(patch: dict[str, Any], expected: str | None) -> None:
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state.update(patch)  # type: ignore[typeddict-item]
    assert interview._terminal_reason(state) == expected
    assert interview.should_continue(state) == ("finish" if expected else "continue")


# ─────────────────────────────────────────────────────────────────────────────
# First Plan 어댑터 — context_from_outcome
# ─────────────────────────────────────────────────────────────────────────────


def test_context_from_outcome_builds_prompt_vars() -> None:
    outcome = interview_adapter.build_outcome(
        session_id="iv_4",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    ctx = first_plan_adapter.context_from_outcome(outcome)
    assert ctx["prompt_vars"]["goal_title"] == "캡스톤"
    assert ctx["prompt_vars"]["horizon"] == "2026-06-20"
    assert "활동: 09:00~23:00" in ctx["prompt_vars"]["time_policy_summary"]
    assert ctx["horizon"] == "2026-06-20"


# ─────────────────────────────────────────────────────────────────────────────
# 그래프 ainvoke end-to-end (aiClient.run stub — ADR-0005 §7.3)
# ─────────────────────────────────────────────────────────────────────────────


def _stub_factory(new_ambiguity: float, *, fell_back: bool = False):
    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        prompt_id = kwargs["prompt_id"]
        value: Any
        if schema is NextQuestionSchema:
            value = NextQuestionSchema(
                question="다음 질문",
                clarity_score=0.8,
                normalized_value=None,
                empathy_one_liner="좋아요",
            )
        elif schema is AmbiguityUpdate:
            value = AmbiguityUpdate(
                slot_key="goals.list", clarity_score=0.9, new_ambiguity=new_ambiguity
            )
        elif schema is GoalDecomposition:
            value = GoalDecomposition(
                goal_nodes=[
                    {
                        "node_id": "n1",
                        "parent_id": None,
                        "title": "캡스톤",
                        "node_type": "root",
                        "order_index": 0,
                        "is_leaf": True,
                    }
                ],
                action_items=[],
                policy_violations=[],
            )
        elif schema is PlanReview:
            value = PlanReview(approved=True, feedback=[])
        else:  # pragma: no cover - 방어
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value, fell_back=fell_back, reason=None, prompt_id=prompt_id, prompt_version="v1"
        )

    return stub_run


async def test_interview_graph_runs_to_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 성공 path — update_ambiguity 가 0.1 반환 → completed 종료 + outcome 빌드."""
    monkeypatch.setattr(aiClient, "run", _stub_factory(0.1))
    graph = interview.build_interview_graph()
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())

    final = await graph.ainvoke(state)

    assert final["end_reason"] == "completed"
    assert isinstance(final["outcome"], InterviewOutcome)
    assert final["outcome"].analysis_source == "llm"
    assert final["used_fallback"] is False


async def test_interview_graph_marks_rule_source_on_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """룰 fallback path — fell_back=True → outcome.analysis_source='rule'."""
    monkeypatch.setattr(aiClient, "run", _stub_factory(0.1, fell_back=True))
    graph = interview.build_interview_graph()
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())

    final = await graph.ainvoke(state)

    assert final["used_fallback"] is True
    assert final["outcome"].analysis_source == "rule"


async def test_first_plan_graph_runs_to_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """First Plan Sequential — decompose → review(approved) → END."""
    monkeypatch.setattr(aiClient, "run", _stub_factory(0.0))
    outcome = interview_adapter.build_outcome(
        session_id="iv_5",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    graph = first_plan.build_first_plan_graph()
    state = first_plan.initial_state(user_id=uuid4(), outcome=outcome, target_date="2026-05-30")

    final = await graph.ainvoke(state)

    assert final["goal_plan"] is not None
    assert final["review"].approved is True
    assert final["missing_fields"] == []  # 모든 필수 슬롯 충족
    assert final["used_fallback"] is False
