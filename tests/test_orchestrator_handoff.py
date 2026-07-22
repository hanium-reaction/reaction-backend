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
    HarvestedSlot,
    InterviewOutcome,
    InterviewSummary,
    NextQuestionSchema,
    SlotHarvest,
)
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalDecomposition,
    GoalNodeDraft,
    PlanReview,
)

# ─────────────────────────────────────────────────────────────────────────────
# 대표 slot_answers (db/models/interview_slot_answer.py value 형식)
# ─────────────────────────────────────────────────────────────────────────────

SLOT_ANSWERS: dict[str, dict[str, Any] | None] = {
    "identity.role": {"type": "chip", "values": ["대3"]},
    "identity.season": {"type": "chip", "values": ["학기중"]},
    "identity.major": {"type": "text", "raw": "컴퓨터공학"},
    "goals.list": {"type": "text", "raw": "캡스톤, 토익", "normalized": ["캡스톤", "토익"]},
    "goals.heaviest": {"type": "text", "raw": "캡스톤"},
    "goals.current_level": {"type": "text", "raw": "기획서 초안까지 씀"},
    "goals.weekly_time": {"type": "chip", "values": ["6시간"]},
    "goals.session_length": {"type": "chip", "values": ["1시간"]},
    "goals.preferred_time": {"type": "chip", "values": ["오전"]},
    "goals.deadlines": {"type": "text", "raw": "2026-06-20"},
    "goals.success_image": {"type": "text", "raw": "데모 동작"},
    "goals.approach": {"type": "text", "raw": "PintOS 과제 순서대로, 강의 자료 위주로"},
    "goals.materials": {"type": "text", "raw": "1주차 스레드, 2주차 유저프로그램, 3주차 VM"},
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
    assert heaviest.current_level == "기획서 초안까지 씀"  # #B baseline
    assert heaviest.weekly_hours == 6  # goals.weekly_time chip "6시간" → 6 (#weekly)
    assert heaviest.session_length_min == 60  # goals.session_length chip "1시간" → 60 (#per-goal)
    assert (
        heaviest.approach_note == "PintOS 과제 순서대로, 강의 자료 위주로"
    )  # goals.approach (#approach)
    assert (
        heaviest.materials_note == "1주차 스레드, 2주차 유저프로그램, 3주차 VM"
    )  # goals.materials (#materials)
    assert heaviest.preferred_time == "오전"  # goals.preferred_time (#per-goal-time)
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
    # weekly_time '6시간' ÷ 목표별 session_length '1시간'(60분) → 6세션/주 (density=standard×1.0).
    assert ctx["prompt_vars"]["sessions_per_week"] == "6"
    assert ctx["prompt_vars"]["weekly_hours"] == "6시간"
    assert ctx["prompt_vars"]["session_length"] == "60분"  # 목표별 집중 길이 (#per-goal)
    # 사용자 접근/자료가 분해 프롬프트에 실린다 (#approach grounding).
    assert ctx["prompt_vars"]["approach_note"] == "PintOS 과제 순서대로, 강의 자료 위주로"
    assert (
        ctx["prompt_vars"]["materials"] == "1주차 스레드, 2주차 유저프로그램, 3주차 VM"
    )  # 자료 원문 (#materials)
    # 완료 기준(성공 이미지)·카테고리가 decompose 프롬프트에 실린다 (#B — 그동안 버려지던 맥락).
    assert ctx["prompt_vars"]["success_image"] == "데모 동작"
    assert ctx["prompt_vars"]["current_level"] == "기획서 초안까지 씀"  # #B baseline 주입
    assert ctx["prompt_vars"]["category"]  # 비어있지 않음


def test_missing_current_level_is_unknown_not_beginner() -> None:
    """current_level 미응답은 '(미입력)' — '처음 시작' 으로 단정하지 않는다 (#B 리뷰).

    회귀: 슬롯 신설(#B) 이전 세션과 [충분해요] 조기 종료는 goals.current_level 이 빈다.
    이때 '처음 시작' 을 실으면 프롬프트 규칙("'처음이에요' 면 입문 단계부터")이 발동해,
    이미 진도 나간 사용자에게 입문 단계를 다시 시키는 계획이 나온다 — 즉 '모름'이 '입문자'로
    둔갑한다. 미응답은 success_image 와 동일한 '(미입력)' 센티넬로 실려야 한다.
    """
    slots = {k: v for k, v in SLOT_ANSWERS.items() if k != "goals.current_level"}
    outcome = interview_adapter.build_outcome(
        session_id="iv_5",
        slot_answers=slots,
        ambiguity_final=0.4,
        end_reason="early_user",
        analysis_source="llm",
    )
    assert "goals.current_level" in outcome.unresolved_slots  # 데이터는 '모름' 이라고 말한다
    ctx = first_plan_adapter.context_from_outcome(outcome)
    assert ctx["prompt_vars"]["current_level"] == "(미입력)"  # 프롬프트도 '모름' 이라고 말해야


def test_every_required_slot_has_a_rule_fallback_question() -> None:
    """필수 슬롯은 모두 LLM 죽었을 때 쓸 기본 질문을 가져야 한다 (#B 리뷰).

    회귀: #B 가 goals.current_level 을 REQUIRED_SLOT_KEYS 에만 추가하고
    _DEFAULT_SLOT_QUESTIONS 에는 빠뜨려, LLM 실패 시 그 슬롯에서 "조금만 더 구체적으로
    알려주실 수 있을까요?" 라는 맥락 없는 질문이 나왔다 (무엇을 묻는지 알 수 없음).
    슬롯을 새로 추가할 때 이 짝을 강제한다.
    """
    missing = set(interview_adapter.REQUIRED_SLOT_KEYS) - set(interview._DEFAULT_SLOT_QUESTIONS)
    assert not missing, f"필수 슬롯인데 LLM 폴백 질문이 없다: {sorted(missing)}"


def test_density_maps_to_sessions_per_week() -> None:
    """주당 가용 시간 미입력 시 — density 프리셋이 '주당 세션 수' 폴백으로 쓰인다."""
    assert first_plan_adapter.sessions_per_week_for("light") == 3
    assert first_plan_adapter.sessions_per_week_for("standard") == 5
    assert first_plan_adapter.sessions_per_week_for("intense") == 8
    assert first_plan_adapter.sessions_per_week_for("bogus") == 5  # 폴백=표준

    # weekly_time 이 없으면 density 프리셋(3/5/8)으로 폴백 (하위호환).
    no_weekly = {k: v for k, v in SLOT_ANSWERS.items() if k != "goals.weekly_time"}
    outcome = interview_adapter.build_outcome(
        session_id="iv_density",
        slot_answers=no_weekly,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    for density, expected in (("light", "3"), ("standard", "5"), ("intense", "8")):
        ctx = first_plan_adapter.context_from_outcome(outcome, density=density)
        assert ctx["prompt_vars"]["sessions_per_week"] == expected


def test_weekly_hours_drives_sessions_over_density() -> None:
    """주당 가용 시간(#weekly) ÷ 목표별 세션 길이(#per-goal)로 세션 수 산정 + density 가감.

    SLOT_ANSWERS: weekly_time '6시간' + session_length '1시간'(60분) → capacity 6*60/60 = 6 세션.
    (전역 focus_duration '50분'보다 목표별 session_length 가 우선.) density 배율: 0.7/1.0/1.3.
    """
    outcome = interview_adapter.build_outcome(
        session_id="iv_weekly",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    # standard: round(6*1.0)=6 — density 프리셋(5)이 아니라 실제 시간 기반.
    assert first_plan_adapter.target_sessions_per_week(outcome, "standard") == 6
    assert first_plan_adapter.target_sessions_per_week(outcome, "light") == 4  # round(6*0.7)=4
    assert first_plan_adapter.target_sessions_per_week(outcome, "intense") == 8  # round(6*1.3)=8
    ctx = first_plan_adapter.context_from_outcome(outcome, density="standard")
    assert ctx["prompt_vars"]["sessions_per_week"] == "6"
    assert ctx["prompt_vars"]["weekly_hours"] == "6시간"
    # 목표별 session_length(60) 가 전역 focus_duration(50) 을 이긴다.
    assert first_plan_adapter.session_min_for(outcome) == 60


def test_normalize_action_minutes_unifies_to_session_length() -> None:
    """목표별 세션 길이가 있으면 각 세션을 그 길이로 통일 — 9분 같은 garbage 제거 + 총합 예측.

    세션 길이 미지정이면 원본 유지.
    """
    outcome = interview_adapter.build_outcome(
        session_id="iv_norm",
        slot_answers=SLOT_ANSWERS,  # session_length "1시간" 은 아래에서 90 으로 덮어씀
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    heaviest = next(g for g in outcome.core_goals if g.is_heaviest)
    heaviest.session_length_min = 90

    def _item(minutes: int) -> ActionItemDraft:
        return ActionItemDraft(
            node_id="n", title="t", estimated_minutes=minutes, category="study", first_step="s"
        )

    items = [_item(9), _item(45), _item(80), _item(200)]
    out = first_plan_adapter.normalize_action_minutes(outcome, items)
    assert [i.estimated_minutes for i in out] == [90, 90, 90, 90]  # 전부 세션 길이로 통일

    # 세션 길이 미지정(전역 fallback) → 원본 그대로.
    heaviest.session_length_min = None
    passthrough = first_plan_adapter.normalize_action_minutes(outcome, items)
    assert [i.estimated_minutes for i in passthrough] == [9, 45, 80, 200]


def test_shape_action_plan_caps_sessions_to_weekly_target() -> None:
    """세션 길이가 크면 LLM 이 세션을 과다 생성해도, 주당 시간 target 로 잘라 overshoot 방지.

    weekly 6시간 + session_length 90분 → target 6*60/90 = 4 세션. LLM 이 8개(각 20분) 내면
    → 정규화(밴드 [68,90]) + 4개로 절단 + 고아 leaf 제거.
    """
    outcome = interview_adapter.build_outcome(
        session_id="iv_shape",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    heaviest = next(g for g in outcome.core_goals if g.is_heaviest)
    heaviest.session_length_min = 90  # target = 4

    nodes = [
        GoalNodeDraft(
            node_id="root",
            parent_id=None,
            title="목표",
            node_type="root",
            order_index=0,
            is_leaf=False,
        )
    ]
    actions = []
    for i in range(8):
        nodes.append(
            GoalNodeDraft(
                node_id=f"leaf{i}",
                parent_id="root",
                title=f"l{i}",
                node_type="leaf",
                order_index=i,
                is_leaf=True,
            )
        )
        actions.append(
            ActionItemDraft(
                node_id=f"leaf{i}",
                title=f"t{i}",
                estimated_minutes=20,
                category="study",
                first_step="s",
            )
        )
    gp = GoalDecomposition(goal_nodes=nodes, action_items=actions, policy_violations=[])

    shaped = first_plan_adapter.shape_action_plan(outcome, "standard", gp)
    assert len(shaped.action_items) == 4  # target 로 절단 (8 → 4)
    assert all(a.estimated_minutes == 90 for a in shaped.action_items)  # 세션 길이로 통일
    assert sum(a.estimated_minutes for a in shaped.action_items) == 360  # 4 × 90 = 6시간(weekly)
    leaf_ids = {n.node_id for n in shaped.goal_nodes if n.is_leaf}
    assert len(leaf_ids) == 4  # 고아 leaf 제거
    assert all(a.node_id in leaf_ids for a in shaped.action_items)


def test_peak_windows_for_plan_prefers_goal_time() -> None:
    """목표별 preferred_time 이 전역 peak 를 덮는다 — '오전' 목표는 오전 창으로(#per-goal-time)."""
    from datetime import time

    outcome = interview_adapter.build_outcome(
        session_id="iv_pt",
        slot_answers=SLOT_ANSWERS,  # 전역 peak=["오전","저녁"], 목표 preferred_time="오전"
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    wins = first_plan_adapter.peak_windows_for_plan(outcome)
    assert len(wins) == 1  # 목표별 preferred_time 하나로 좁혀짐(전역 2창이 아니라)
    assert wins[0].start == time(6, 0) and wins[0].end == time(12, 0)  # 오전 창

    # preferred_time 없으면 전역 peak(오전+저녁 2창)로 폴백.
    heaviest = next(g for g in outcome.core_goals if g.is_heaviest)
    heaviest.preferred_time = None
    assert len(first_plan_adapter.peak_windows_for_plan(outcome)) == 2


def test_preferred_time_outside_activity_becomes_available() -> None:
    """활동창이 저녁뿐이어도 목표 선호 시간(오전)이 있으면 오전이 가용해진다.

    회귀: 예전엔 활동창(20:00~24:00) 밖이라 아침이 수면(busy)으로 잡혀, 아침 운동이 저녁으로
    폴백했다(사용자 발견). 이제 선호 시간대를 가용에 포함한다(#per-goal-time-availability).
    """
    from datetime import date

    from reaction_backend.orchestrator.goal_structuring import (
        compute_free_blocks,
        time_policies_to_busy,
    )

    sa = {
        **SLOT_ANSWERS,
        "time.activity_window": {"type": "range", "start": "20:00", "end": "24:00"},
        "goals.preferred_time": {"type": "chip", "values": ["오전"]},
    }
    outcome = interview_adapter.build_outcome(
        session_id="iv_av",
        slot_answers=sa,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    pols = first_plan_adapter.time_policies_from_outcome(outcome)
    day = date(2026, 7, 23)
    free = compute_free_blocks(day, time_policies_to_busy(day, pols))
    # 오전(06~12)에 가용 구간이 생겨야 한다.
    assert any(f.start.hour < 12 for f in free), [
        (str(f.start.time()), str(f.end.time())) for f in free
    ]


def test_shape_action_plan_covers_horizon_not_just_one_week() -> None:
    """마감까지 여러 주면 세션 상한이 target×주수 로 늘어, 유한 목표(20강)를 끝까지 커버(#horizon-cap).

    회귀: 예전엔 1주치(target)로 잘라 마감 전 뒷부분 세션을 아예 안 만들었다(사용자 발견).
    """
    from datetime import date

    outcome = interview_adapter.build_outcome(
        session_id="iv_hz",
        slot_answers=SLOT_ANSWERS,  # weekly 6 + session 60분 → target 6/주, 마감 2026-06-20
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    nodes = [
        GoalNodeDraft(
            node_id="root",
            parent_id=None,
            title="목표",
            node_type="root",
            order_index=0,
            is_leaf=False,
        )
    ]
    actions = []
    for i in range(12):
        nodes.append(
            GoalNodeDraft(
                node_id=f"l{i}",
                parent_id="root",
                title=f"l{i}",
                node_type="leaf",
                order_index=i,
                is_leaf=True,
            )
        )
        actions.append(
            ActionItemDraft(
                node_id=f"l{i}",
                title=f"t{i}",
                estimated_minutes=60,
                category="study",
                first_step="s",
            )
        )
    gp = GoalDecomposition(goal_nodes=nodes, action_items=actions, policy_violations=[])

    # target_date 2주 전 → 마감(06-20)까지 2주 → 상한 6×2=12 → 12세션 전부 유지.
    two_weeks_before = date(2026, 6, 6)
    shaped = first_plan_adapter.shape_action_plan(
        outcome, "standard", gp, target_date=two_weeks_before
    )
    assert len(shaped.action_items) == 12
    # target_date 없으면 1주치(6)로 잘림(하위호환).
    assert len(first_plan_adapter.shape_action_plan(outcome, "standard", gp).action_items) == 6


def test_daily_cap_scales_with_density() -> None:
    """하루 집중 총량 상한(분)도 density 에 연동 — standard 는 기존 기본값."""
    assert first_plan_adapter.daily_cap_for("light") == 120
    assert first_plan_adapter.daily_cap_for("standard") == 180
    assert first_plan_adapter.daily_cap_for("intense") == 240
    assert (
        first_plan_adapter.daily_cap_for("bogus") == first_plan_adapter.DEFAULT_DAILY_FOCUS_CAP_MIN
    )


def test_rule_fallback_respects_density() -> None:
    """Gemini 폴백(_rule_decomposition)도 LLM 경로와 같은 분량 규칙을 따른다 (빈 계획 방지).

    SLOT_ANSWERS: weekly_time '6시간' ÷ session_length '1시간'(60분) → capacity 6 세션, density 가감
    (light 0.7→4 / standard 1.0→6 / intense 1.3→8).
    """
    outcome = interview_adapter.build_outcome(
        session_id="iv_fbden",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    for density, n in (("light", 4), ("standard", 6), ("intense", 8)):
        state = first_plan.initial_state(
            user_id=uuid4(), outcome=outcome, target_date="2026-06-01", density=density
        )
        decomp = first_plan._rule_decomposition(state)
        assert len(decomp.action_items) == n  # density 만큼 세션
        assert len(decomp.goal_nodes) == n + 1  # root + n leaves
        assert all(a.estimated_minutes <= 60 for a in decomp.action_items)


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


async def test_schedule_blocks_does_not_place_today_sessions_in_the_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저녁에 만든 계획이 '오늘 이미 지난 시간대'에 세션을 잡지 않는다 (now-clamp).

    생성 시각이 20:00 인데 활동창(09:00~23:00) 앞부분에 세션이 배치되면 시작 불가.
    오늘의 [00:00, 지금) 을 busy 로 막으므로 모든 오늘 블록은 20:00 이후에 놓여야 한다.
    """
    from datetime import datetime, time

    from reaction_backend.schemas.common import KST

    today = "2026-06-20"  # == SLOT_ANSWERS goals.deadlines → horizon 이 오늘 하루로 수렴
    frozen = datetime(2026, 6, 20, 20, 0, tzinfo=KST)  # 저녁 8시 생성
    monkeypatch.setattr(first_plan, "now_kst", lambda: frozen)

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is GoalDecomposition:
            value: Any = GoalDecomposition(
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
                action_items=[
                    {
                        "node_id": "n1",
                        "title": f"작업{i}",
                        "estimated_minutes": 30,
                        "category": "study",
                        "first_step": "시작",
                    }
                    for i in range(3)
                ],
                policy_violations=[],
            )
        elif schema is PlanReview:
            value = PlanReview(approved=True, feedback=[])
        else:  # pragma: no cover - 방어
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)
    outcome = interview_adapter.build_outcome(
        session_id="iv_x",
        slot_answers=SLOT_ANSWERS,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    graph = first_plan.build_first_plan_graph()
    state = first_plan.initial_state(user_id=uuid4(), outcome=outcome, target_date=today)

    final = await graph.ainvoke(state)

    blocks = final["scheduled_blocks"]
    assert blocks, "오늘 활동창 후반(20:00~23:00)에 세션이 배치돼야 한다"
    for b in blocks:
        assert b.start.date().isoformat() == today
        assert b.start.time() >= time(20, 0), f"과거 시각에 배치됨: {b.start}"


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


def test_goal_decompose_prompt_locks_category_enum() -> None:
    """프롬프트 계약 잠금 — action_item.category 전체 enum 명시 + 게으른 'other' 금지 규칙.

    enum 이 빠지면 LLM 이 대부분 'other' 를 반환해 주간 그리드가 전부 '기타' 로
    렌더되던 문제가 조용히 재발한다 (api-change-log v1.17).
    """
    from reaction_backend.db.models.action_item import ACTION_CATEGORY_VALUES
    from reaction_backend.prompts import registry as prompt_registry

    body = prompt_registry.get("planning/goal_decompose").body
    for value in ACTION_CATEGORY_VALUES:
        assert value in body  # 응답 형식/규칙 어딘가에 전체 enum 이 명시돼 있어야 한다
    assert "other 를 쓰지 마라" in body  # 게으른 기본값 방지 규칙


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


# ─────────────────────────────────────────────────────────────────────────────
# 다음 질문 러닝 컨텍스트 (P2-a)
# ─────────────────────────────────────────────────────────────────────────────


def test_answered_context_summarizes_filled_slots() -> None:
    """앞서 채워진 슬롯이 '태그=값' 러닝 요약으로 next_question 에 실린다 (P2-a)."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = {
        "identity.role": {"type": "chip", "values": ["3학년"]},
        "goals.list": {"type": "text", "raw": "캡스톤, 토익", "normalized": ["캡스톤", "토익"]},
    }

    ctx = interview._answered_context(state)
    assert "학년/시기=3학년" in ctx
    assert "목표=캡스톤, 토익" in ctx


def test_answered_context_empty_when_no_answers() -> None:
    """아직 아무 답도 없으면 명시 문구 — 프롬프트가 빈 맥락을 오해하지 않게."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    assert interview._answered_context(state) == "(아직 답한 내용 없음)"


# ─────────────────────────────────────────────────────────────────────────────
# 슬롯 하베스팅 — 한 답에 섞인 다른 슬롯 미리 채우기 (재질문 감소)
# ─────────────────────────────────────────────────────────────────────────────

_HARVEST_META = {
    "goals.deadlines": {"label": "마감", "answer_type": "date_picker", "options": []},
    "time.peak_window": {
        "label": "집중 시간대",
        "answer_type": "chip",
        "options": ["오전", "오후", "저녁", "심야", "변동"],
    },
    "recovery.tone": {
        "label": "회복 톤",
        "answer_type": "chip",
        "options": ["담백", "따뜻", "유머", "코치처럼"],
    },
    "identity.role": {
        "label": "학년/시기",
        "answer_type": "chip",
        "options": ["1학년", "2학년", "3학년", "4학년", "졸업유예", "대학원", "기타"],
    },
}


async def test_harvest_prefills_confident_unfilled_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """자유서술 답에서 확신 있는 다른 슬롯을 미리 채운다 — answer_type 별 구조화 + 신뢰도 게이트."""

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        assert kwargs["schema"] is SlotHarvest  # 이 노드는 하베스팅만 호출
        return RunResult(
            value=SlotHarvest(
                slots=[
                    HarvestedSlot(
                        slot_key="goals.deadlines", normalized_value="2026-08-20", confidence=0.9
                    ),
                    HarvestedSlot(
                        slot_key="time.peak_window", normalized_value=["오전"], confidence=0.85
                    ),
                    # 신뢰도 낮음 → 채우지 않는다 (재질문보다 나쁜 오채움 방지)
                    HarvestedSlot(
                        slot_key="recovery.tone", normalized_value="담백", confidence=0.4
                    ),
                    HarvestedSlot(
                        slot_key="identity.role", normalized_value="3학년", confidence=0.95
                    ),
                ]
            ),
            fell_back=False,
            reason=None,
            prompt_id="interview/slot_extraction",
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", fake_run)

    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = {
        "goals.list": {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]}
    }
    config: Any = {"configurable": {"session": None, "slot_meta": _HARVEST_META}}

    new_state = await interview.harvest_slots(
        state,
        config,
        answer_text="캡스톤은 8월 20일 마감이고 난 3학년이고 오전에 집중이 잘돼",
        answered_slot="goals.list",
    )

    sa = new_state["slot_answers"]
    assert new_state["harvested"] == ["goals.deadlines", "time.peak_window", "identity.role"]
    assert sa["goals.deadlines"] == {"type": "text", "raw": "2026-08-20"}  # date_picker 구조화
    assert sa["time.peak_window"] == {"type": "chip", "values": ["오전"]}  # chip 구조화
    assert sa["identity.role"] == {"type": "chip", "values": ["3학년"]}
    assert "recovery.tone" not in sa  # 신뢰도 0.4 < 0.7 → 스킵


async def test_harvest_noop_when_no_open_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """채울 미충족 슬롯이 없으면 LLM 호출 없이 빈 결과 — 불필요한 호출/비용 방지."""
    called = {"n": 0}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:  # pragma: no cover - 호출되면 실패
        called["n"] += 1
        raise AssertionError("harvest should not call LLM when no open slots")

    monkeypatch.setattr(aiClient, "run", fake_run)

    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = {
        k: {"type": "text", "raw": "x"} for k in interview.REQUIRED_SLOT_SEQUENCE
    }
    config: Any = {"configurable": {"session": None, "slot_meta": {}}}

    new_state = await interview.harvest_slots(
        state, config, answer_text="뭐든", answered_slot="goals.list"
    )
    assert new_state["harvested"] == []
    assert called["n"] == 0
