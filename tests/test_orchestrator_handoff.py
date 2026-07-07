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
    InterviewSummary,
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
    "recovery.downscope_unit": {"type": "chip", "values": ["10분"]},
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
# Interview Cyclic 종료 조건 (순수 함수 _terminal_reason / should_continue)
#
# 완료는 필수 슬롯 완료(FSM)가 단독으로 운전한다 — float ambiguity_score 임계로는 조기
# 종료하지 않는다(그러면 명료성이 100%에 못 닿음). turn_limit 도 없다(슬롯별 시도 상한이
# 완료 수렴을 보장 — _decide_storage). 조기 종료는 [충분해요](early_finish)뿐.
# ─────────────────────────────────────────────────────────────────────────────

_ALL_REQUIRED_FILLED = {k: {"type": "text", "raw": "x"} for k in interview.REQUIRED_SLOT_SEQUENCE}


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"slot_answers": _ALL_REQUIRED_FILLED}, "completed"),  # 필수 슬롯 완료 = 명료성 100%
        ({"ambiguity_score": 0.7, "early_finish": True}, "early_user"),  # 충분해요
        # 회귀: 필수 슬롯 완료 전에는 낮은 LLM 모호함만으로 종료하지 않음
        ({"ambiguity_score": 0.05}, None),
        # 회귀: turn_limit 없음 — 턴이 많아도 필수 슬롯 완료가 우선
        ({"ambiguity_score": 0.7, "total_turns": 15}, None),
        ({"ambiguity_score": 0.7}, None),  # 계속
    ],
)
def test_interview_termination_conditions(patch: dict[str, Any], expected: str | None) -> None:
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state.update(patch)  # type: ignore[typeddict-item]
    assert interview._terminal_reason(state) == expected
    assert interview.should_continue(state) == ("finish" if expected else "continue")


def test_interview_terminates_when_required_slots_are_filled() -> None:
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = {
        key: {"type": "text", "raw": "답변"} for key in interview.REQUIRED_SLOT_SEQUENCE
    }

    assert interview._terminal_reason(state) == "completed"
    assert interview.should_continue(state) == "finish"


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
                empathy_one_liner="좋아요",
            )
        elif schema is AmbiguityUpdate:
            value = AmbiguityUpdate(
                slot_key="goals.list", clarity_score=0.9, new_ambiguity=new_ambiguity
            )
        elif schema is InterviewSummary:
            value = InterviewSummary(
                headline="요약",
                goal_summary="목표 요약",
                time_summary="시간 요약",
                preference_summary="선호 요약",
                confirm_question="이대로 계획을 세워볼까요?",
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
    """LLM 성공 path — 필수 슬롯 완료 상태에서 completed 종료 + outcome 빌드."""
    monkeypatch.setattr(aiClient, "run", _stub_factory(0.1))
    graph = interview.build_interview_graph()
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = dict(SLOT_ANSWERS)

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
    state["slot_answers"] = dict(SLOT_ANSWERS)

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


async def test_review_plan_wires_prompt_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """review_plan 이 planning/plan_quality 변수 4종을 채워 LLM 을 실제 호출 (#32, PR #44).

    과거 variables={} 는 render 실패 → 항상 룰 승인 fallback 이었다.
    """
    captured: dict[str, Any] = {}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        if kwargs["schema"] is PlanReview:
            captured.update(kwargs["variables"])
            return RunResult(
                value=PlanReview(approved=True, feedback=[]),
                fell_back=False,
                reason=None,
                prompt_id=kwargs["prompt_id"],
                prompt_version="v1",
            )
        # decompose(goal_decompose) 는 룰 분해로 환원
        return RunResult(
            value=kwargs["fallback"](),
            fell_back=True,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", fake_run)
    outcome = interview_adapter.build_outcome(
        session_id="iv_6",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    cfg: Any = {"configurable": {}}
    state = first_plan.initial_state(user_id=uuid4(), outcome=outcome, target_date="2026-06-01")
    state = await first_plan.validate_inputs(state, cfg)
    state = await first_plan.decompose_goal(state, cfg)
    await first_plan.review_plan(state, cfg)

    assert set(captured) >= {
        "goal_nodes_json",
        "action_items_json",
        "time_policy_summary",
        "conflict_report",
    }
    assert captured["goal_nodes_json"] != "[]"  # 실제 노드 직렬화됨
    assert captured["conflict_report"]  # 비어있지 않음


# ─────────────────────────────────────────────────────────────────────────────
# decompose → review → replan 피드백 배선 (P0-2)
# ─────────────────────────────────────────────────────────────────────────────


def _capture_decompose_vars(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """decompose(goal_decompose) 호출의 variables 를 잡는 aiClient.run stub 설치."""

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        if kwargs["schema"] is GoalDecomposition:
            captured.update(kwargs["variables"])
        return RunResult(
            value=kwargs["fallback"](),
            fell_back=True,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", fake_run)


async def test_decompose_first_pass_has_no_prior_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """첫 분해(리뷰 이전)에는 review_feedback 이 '없음' 신호 — 실제 지적은 실리지 않는다."""
    captured: dict[str, Any] = {}
    _capture_decompose_vars(monkeypatch, captured)

    outcome = interview_adapter.build_outcome(
        session_id="iv_fb0",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    cfg: Any = {"configurable": {}}
    state = first_plan.initial_state(user_id=uuid4(), outcome=outcome, target_date="2026-06-01")
    state = await first_plan.validate_inputs(state, cfg)
    await first_plan.decompose_goal(state, cfg)

    assert captured["review_feedback"] == "(첫 분해 — 이전 피드백 없음)"


async def test_decompose_replan_threads_review_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """replan 재진입 시 직전 리뷰 피드백이 decompose 프롬프트 변수로 실린다 (P0-2).

    회귀: 과거엔 review 피드백이 재분해로 전달되지 않아 같은 프롬프트를 반복 실행,
    cycle 이 계획을 개선하지 못했다.
    """
    captured: dict[str, Any] = {}
    _capture_decompose_vars(monkeypatch, captured)

    outcome = interview_adapter.build_outcome(
        session_id="iv_fb1",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    cfg: Any = {"configurable": {}}
    state = first_plan.initial_state(user_id=uuid4(), outcome=outcome, target_date="2026-06-01")
    state = await first_plan.validate_inputs(state, cfg)
    # review_plan 이 미승인 피드백을 남긴 상태를 모사 (replan 엣지 재진입 직전)
    state = {
        **state,
        "review": PlanReview(
            approved=False,
            feedback=["캡스톤 설계 leaf 를 30분 이내로 더 쪼개기", "토익은 다음 주로 미루기"],
        ),
    }

    await first_plan.decompose_goal(state, cfg)  # type: ignore[arg-type]

    fb = captured["review_feedback"]
    assert "캡스톤 설계 leaf 를 30분 이내로 더 쪼개기" in fb
    assert "토익은 다음 주로 미루기" in fb
    assert fb != "(첫 분해 — 이전 피드백 없음)"


def test_goal_decompose_prompt_drops_freebusy_adds_feedback() -> None:
    """프롬프트 계약 잠금 — 무의미하던 freebusy 변수 제거, review_feedback 변수 추가."""
    from reaction_backend.prompts import registry as prompt_registry

    body = prompt_registry.get("planning/goal_decompose").body
    assert "freebusy" not in body  # 항상 빈 값이라 LLM 에 무의미했던 변수 제거
    assert "{{review_feedback}}" in body  # replan 피드백 주입 지점


# ─────────────────────────────────────────────────────────────────────────────
# 계획 호출 thinking 예산 배선 (P1-3)
# ─────────────────────────────────────────────────────────────────────────────


async def test_planning_calls_enable_thinking_with_longer_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decompose·review 는 인터뷰와 달리 thinking 을 켜고 timeout 을 상향해 호출한다 (P1-3).

    인터뷰 턴은 thinking_budget=None(=flash 0) 을 유지하고, 계획 분해·검토만 settings 의
    planning 예산/타임아웃으로 넘어가는지 aiClient.run kwargs 로 검증한다.
    """
    from reaction_backend.config import get_settings

    calls: dict[str, dict[str, Any]] = {}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        calls[kwargs["prompt_id"]] = kwargs
        return RunResult(
            value=kwargs["fallback"](),
            fell_back=True,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", fake_run)
    settings = get_settings()

    outcome = interview_adapter.build_outcome(
        session_id="iv_think",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    cfg: Any = {"configurable": {}}
    state = first_plan.initial_state(user_id=uuid4(), outcome=outcome, target_date="2026-06-01")
    state = await first_plan.validate_inputs(state, cfg)
    state = await first_plan.decompose_goal(state, cfg)
    await first_plan.review_plan(state, cfg)

    for pid in ("planning/goal_decompose", "planning/plan_quality"):
        assert calls[pid]["thinking_budget"] == settings.llm_planning_thinking_budget
        assert calls[pid]["timeout"] == settings.llm_planning_timeout_seconds


# ─────────────────────────────────────────────────────────────────────────────
# 인터뷰 요약 충실도 (P1-4)
# ─────────────────────────────────────────────────────────────────────────────


def test_summary_variables_include_deadline_and_prefs() -> None:
    """요약 변수가 마감·성공 이미지·노터치·휴식 수용·다운스코프 단위까지 실어낸다 (P1-4)."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = dict(SLOT_ANSWERS)

    v = interview._summary_variables(state)
    assert v["deadlines"] == "2026-06-20"
    assert v["success_image"] == "데모 동작"
    assert v["no_touch"] == "일요일"
    assert v["rest_ok"] == "네"
    assert v["downscope_unit"] == "10분"


def test_rule_summary_weaves_answered_fields() -> None:
    """룰 요약도 값이 있는 항목(마감·휴식·다운스코프)을 문장에 반영한다 (fallback 충실도)."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = dict(SLOT_ANSWERS)

    s = interview._rule_summary(state)
    assert "2026-06-20" in s.goal_summary  # 마감 반영
    assert "10분" in s.preference_summary  # 다운스코프 단위 반영


def test_rule_summary_omits_unset_optional_fields() -> None:
    """미입력 선택 항목은 지어내지 않고 생략 — 마감·휴식·다운스코프 절이 붙지 않는다."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = {
        "goals.list": {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]},
        "goals.heaviest": {"type": "chip", "values": ["캡스톤"]},
        "recovery.tone": {"type": "chip", "values": ["담백"]},
    }

    s = interview._rule_summary(state)
    # 마감/성공 이미지/휴식/다운스코프는 미입력 → 해당 절이 문장에 추가되지 않는다
    assert "마감은" not in s.goal_summary
    assert "모습을 그리셨어요" not in s.goal_summary
    assert "휴식 제안은" not in s.preference_summary
    assert "단위로 줄여" not in s.preference_summary
