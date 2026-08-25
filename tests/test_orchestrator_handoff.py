"""Deep Interview(#6) → First Plan(#32) 경계 계약 + LangGraph 베이스라인 테스트.

ADR-0005 §7.3 패턴: aiClient.run 만 stub, Node 는 일반 async 함수라 직접 pytest.
- 경계 계약 InterviewOutcome 결정적 빌드 (LLM 0회) + camelCase 직렬화
- Interview Cyclic 그래프 종료 조건 4종 (순수 함수)
- 두 그래프 ainvoke end-to-end (stub 성공 path / 룰 fallback path)
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.llm import RunResult, aiClient
from reaction_backend.orchestrator import (
    first_plan,
    first_plan_adapter,
    interview,
    interview_adapter,
    interview_catalog,
)
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    AvailabilityProfile,
    GoalCandidate,
    HarvestedSlot,
    IdentityContext,
    InterviewOutcome,
    InterviewSummary,
    NextQuestionSchema,
    PreferenceProfile,
    SlotHarvest,
    TimeRange,
)
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalDecomposition,
    GoalNodeDraft,
    MilestoneDraft,
    PlanReview,
    PolicyViolation,
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
    # '몰아서 · 상관없음' → frequency_per_week=None (볼륨 기반). weekly_hours 산정 경로가 유지된다.
    "goals.frequency": {"type": "chip", "values": ["몰아서 · 상관없음"]},
    "goals.deadlines": {"type": "text", "raw": "2026-06-20"},
    "goals.success_image": {"type": "text", "raw": "데모 동작"},
    "goals.approach": {"type": "text", "raw": "PintOS 과제 순서대로, 강의 자료 위주로"},
    "goals.materials": {"type": "text", "raw": "1주차 스레드, 2주차 유저프로그램, 3주차 VM"},
    "time.activity_window": {"type": "range", "start": "09:00", "end": "23:00"},
    "time.peak_window": {"type": "chip", "values": ["오전", "저녁"]},
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
    assert heaviest.frequency_per_week is None  # '몰아서·상관없음' → None (#per-goal-frequency)
    assert {g.title for g in outcome.core_goals} == {"캡스톤", "토익"}
    assert outcome.availability.activity_window.start == "09:00"
    assert outcome.availability.peak_window == ["오전", "저녁"]
    assert outcome.preferences.recovery_tone == "담백"
    assert outcome.preferences.rest_ok is True
    assert outcome.preferences.focus_duration_min == 50
    assert outcome.horizon == "2026-06-20"
    assert outcome.unresolved_slots == []  # 필수 슬롯 모두 채움


def test_build_outcome_backfills_heaviest_when_absent_from_typed_goals_list() -> None:
    """goals.heaviest 가 goals.list 응답엔 없어도(만다라 승격 목표를 골랐을 때, ADR-0008
    §8 "B" — `routes/interview.py::_question_options` 가 goals.list 밖 제목도 보기로
    내려준다) is_heaviest 후보로 살아남는다. title 매칭 실패로 heaviest 전용 필드가
    통째로 유실되면 마감·주당시간 같은 값이 조용히 사라진다."""
    answers = {**SLOT_ANSWERS, "goals.heaviest": {"type": "text", "raw": "메이저리그 드래프트"}}
    outcome = interview_adapter.build_outcome(
        session_id="iv_mandala",
        slot_answers=answers,
        ambiguity_final=0.0,
        end_reason="completed",
        analysis_source="llm",
    )
    titles = {g.title for g in outcome.core_goals}
    assert "메이저리그 드래프트" in titles  # goals.list 엔 없던 제목인데도 후보에 포함됐다
    assert {"캡스톤", "토익"} <= titles  # goals.list 에 있던 목표들은 그대로 유지(maintain)
    heaviest = next(g for g in outcome.core_goals if g.is_heaviest)
    assert heaviest.title == "메이저리그 드래프트"
    assert heaviest.tentative_tier == "focus"
    assert heaviest.deadline == "2026-06-20"  # heaviest 전용 필드도 여전히 승계됨


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

_ALL_REQUIRED_FILLED = {
    k: {"type": "text", "raw": "x"} for k in interview_catalog.PLAN_CATALOG.required_keys
}


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
        key: {"type": "text", "raw": "답변"} for key in interview_catalog.PLAN_CATALOG.required_keys
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
    # 자료 원문 (#materials) — 인젝션 방어로 울타리에 감싸여 나가므로 포함 여부로 본다.
    assert "1주차 스레드, 2주차 유저프로그램, 3주차 VM" in ctx["prompt_vars"]["materials"]
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
    missing = set(interview_adapter.REQUIRED_SLOT_KEYS) - set(
        interview_catalog.PLAN_CATALOG.default_questions
    )
    assert not missing, f"필수 슬롯인데 LLM 폴백 질문이 없다: {sorted(missing)}"


# ── #187 목표별 질문이 어느 목표인지 밝힌다 ────────────────────────────────
#
# 회귀 배경(실측): 목표를 3개 말하면 계획은 heaviest 하나만 다루는데, 목표별 슬롯 6종은
# 끝까지 "이 목표는 한 번에 어느 정도…" 라고만 물었다. 사용자는 자기가 **무엇에 대해**
# 답하는지 알 수 없다. 프롬프트 규칙이 1차 방어지만, LLM 이 죽으면 이 룰 폴백이 그대로
# 사용자에게 나가므로 결정적으로도 이름이 들어가야 한다.


def _state_with_goals(*titles: str) -> Any:
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"]["goals.list"] = {
        "type": "text",
        "raw": ", ".join(titles),
        "normalized": list(titles),
    }
    state["slot_answers"]["goals.heaviest"] = {"type": "chip", "values": [titles[0]]}
    return state


def test_per_goal_fallback_questions_name_the_goal() -> None:
    """목표별 슬롯의 룰 폴백 질문에 실제 목표 이름이 들어간다 — '이 목표' 지시어 금지."""
    state = _state_with_goals("토익 900점", "캡스톤 마무리", "운동 습관")
    per_goal = sorted(
        interview_catalog.PLAN_CATALOG.per_goal_slots
        & set(interview_catalog.PLAN_CATALOG.default_questions)
    )
    assert per_goal, "목표별 슬롯이 하나도 안 잡히면 이 테스트가 무의미하다"
    for slot in per_goal:
        q = interview._rule_next_question(state, slot).question
        assert "토익 900점" in q, f"{slot}: 목표 이름이 없다 — {q}"
        assert "{goal}" not in q, f"{slot}: 자리표시자가 새어나갔다 — {q}"
        assert "이 목표" not in q and "그 목표" not in q, f"{slot}: 지시어가 남았다 — {q}"


def test_non_goal_fallback_questions_do_not_name_the_goal() -> None:
    """목표와 무관한 슬롯엔 목표 이름을 넣지 않는다 — 과교정 방지."""
    state = _state_with_goals("토익 900점", "캡스톤 마무리")
    for slot in ("identity.role", "time.activity_window", "recovery.tone", "goals.list"):
        q = interview._rule_next_question(state, slot).question
        assert "토익 900점" not in q, f"{slot}: 목표 이름이 끼어들었다 — {q}"


def test_goal_naming_avoids_particle_mismatch() -> None:
    """받침에 안 맞는 조사를 만들지 않는다 — 쉼표로 끊는 문형인지 확인.

    한국어 조사(은/는·이/가)는 목표 제목의 받침에 따라 달라져, "'{goal}'는" 같은 템플릿은
    제목마다 틀린다("토익 900점**는**"). 받침이 있는 제목과 없는 제목 양쪽으로 검사한다.
    """
    for title in ("토익 900점", "운동"):  # 받침 있음 / 없음
        q = interview._rule_next_question(_state_with_goals(title), "goals.session_length").question
        assert f"'{title}'," in q, f"쉼표로 끊는 문형이 아니다 — {q}"
        for bad in (f"'{title}'는", f"'{title}'은", f"'{title}'이", f"'{title}'가"):
            assert bad not in q, f"조사가 붙었다({bad}) — 제목마다 어색해진다: {q}"


def test_fallback_question_survives_missing_goal() -> None:
    """목표가 아직 없으면 자리표시자가 그대로 나가지 않고 기본 표현으로 채워진다."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    q = interview._rule_next_question(state, "goals.session_length").question
    assert "{goal}" not in q
    assert "당신의 목표" in q  # _heaviest_goal_hint 의 최종 폴백


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


def test_frequency_drives_sessions_over_volume_and_density() -> None:
    """빈도(#per-goal-frequency)가 있으면 주당 세션 수 = 빈도값 — 볼륨·density 를 이긴다.

    '매일' → 7, '주 3회' → 3. weekly_hours(6)·density 가감과 무관하게 케이던스를 존중한다.
    이게 '매일 하고 싶다고 했는데 주 1일만 반영'되던 문제를 봉합한다(세션 수→요일 분산).
    """
    daily = interview_adapter.build_outcome(
        session_id="iv_freq_daily",
        slot_answers={**SLOT_ANSWERS, "goals.frequency": {"type": "chip", "values": ["매일"]}},
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    assert next(g for g in daily.core_goals if g.is_heaviest).frequency_per_week == 7
    # 매일 → 7. density 로 가감하지 않고(케이던스 존중), 볼륨(weekly_hours=6)도 이긴다.
    assert first_plan_adapter.target_sessions_per_week(daily, "standard") == 7
    assert first_plan_adapter.target_sessions_per_week(daily, "intense") == 7
    assert first_plan_adapter.context_from_outcome(daily)["prompt_vars"]["sessions_per_week"] == "7"

    thrice = interview_adapter.build_outcome(
        session_id="iv_freq_3",
        slot_answers={**SLOT_ANSWERS, "goals.frequency": {"type": "chip", "values": ["주 3회"]}},
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    assert next(g for g in thrice.core_goals if g.is_heaviest).frequency_per_week == 3
    assert first_plan_adapter.target_sessions_per_week(thrice, "standard") == 3


def _outcome_with(session_id: str, **slots: dict[str, Any]) -> Any:
    return interview_adapter.build_outcome(
        session_id=session_id,
        slot_answers={**SLOT_ANSWERS, **slots},
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )


def test_planned_session_length_reconciles_frequency_with_weekly_hours() -> None:
    """빈도는 *며칠*, 주당 시간은 *총 얼마나* — 세션 **길이**로 둘을 화해시킨다.

    예전엔 빈도가 있으면 주당 시간을 산술에서 통째로 무시하고 세션 길이를 집중 용량으로
    고정해서, 주당 총량이 사용자가 말한 값과 크게 어긋났다:
      · '주 2시간 + 매일'  → 7 × 60분 = 주 7시간 (**3.5배 과부하**)
      · '주 8시간 + 주 1회' → 1 × 30분 = 주 30분
    세션 수는 빈도(케이던스)로 두되 길이를 `주당시간 ÷ 빈도` 로 잡으면 두 답이 모두 산다.
    """
    # 과부하 케이스 — 주 2시간을 매일로 나누면 회당 17분(120/7). 7 × 17 ≈ 120분 = 주 2시간.
    light_daily = _outcome_with(
        "iv_recon_light",
        **{
            "goals.weekly_time": {"type": "chip", "values": ["2시간"]},
            "goals.frequency": {"type": "chip", "values": ["매일"]},
        },
    )
    assert first_plan_adapter.session_min_for(light_daily) == 60  # 집중 '용량' 은 그대로 60분
    per_session = first_plan_adapter.planned_session_min_for(light_daily)
    sessions = first_plan_adapter.target_sessions_per_week(light_daily, "standard")
    assert per_session == 17  # 배분은 17분 (120 ÷ 7)
    assert sessions == 7  # 케이던스는 그대로 매일
    # 핵심: 주당 총량이 사용자가 말한 2시간(120분)에 수렴한다 (예전엔 420분이 나왔다).
    assert sessions * per_session == pytest.approx(120, abs=10)

    # 용량 초과 케이스 — 주 8시간 ÷ 주 1회 = 480분이지만 한 번에 집중 가능한 60분으로 캡한다.
    # 더 길게 잡으면 스케줄러가 focus_chunk 로 쪼개고 그 조각들이 stride 배치에서 **다른 날**로
    # 흩어져 '주 1회' 케이던스가 깨진다. 과부하보다 과소가 안전(남는 분량은 주간 재계획이 잇는다).
    heavy_weekly = _outcome_with(
        "iv_recon_heavy",
        **{
            "goals.weekly_time": {"type": "chip", "values": ["8시간 이상"]},
            "goals.frequency": {"type": "chip", "values": ["주 1회"]},
        },
    )
    assert first_plan_adapter.planned_session_min_for(heavy_weekly) == 60  # 용량으로 캡

    # 빈도가 없으면(몰아서·상관없음) 화해할 게 없으므로 집중 용량 그대로 — 기존 동작 보존.
    volume_only = _outcome_with("iv_recon_none")
    assert first_plan_adapter.planned_session_min_for(volume_only) == 60
    assert first_plan_adapter.session_min_for(volume_only) == 60


def test_volume_shortfall_reports_actual_not_intended() -> None:
    """부족분 경고는 **실제 배치 결과**로 말한다 — 입력만 보면 과대 약속한다.

    실측 회귀: 주 8시간 + 매일 + 한 번에 1시간에서 분해가 8세션만 나와 2주에 퍼졌다
    (= 주 4시간). 예전 경고는 입력만 보고 "주 7.0시간으로 잡았어요" 라고 했는데 실제는
    주 4시간이었다. 사용자에게 없는 계획을 약속한 셈이다.
    """
    conflicting = _outcome_with(
        "iv_shortfall",
        **{
            "goals.weekly_time": {"type": "chip", "values": ["8시간 이상"]},
            "goals.frequency": {"type": "chip", "values": ["매일"]},
        },
    )
    # 8세션 × 60분이 14일에 퍼진 실측 상황 → 주 4시간.
    warning = first_plan_adapter.volume_shortfall_warning(
        conflicting, planned_minutes=8 * 60, span_days=14
    )
    assert warning is not None
    assert "8시간" in warning  # 사용자가 말한 값
    assert "4.0시간" in warning  # 실제로 담긴 값 — 7.0 이라고 하면 안 된다
    assert "7.0시간" not in warning

    # 같은 분량이 8일에 담기면(케이던스 수정 후) 주 7시간 → 여전히 부족하지만 정직하게 7.0.
    tight = first_plan_adapter.volume_shortfall_warning(
        conflicting, planned_minutes=8 * 60, span_days=8
    )
    assert tight is not None
    assert "7.0시간" in tight

    # 말한 만큼 담겼으면 경고 없음(잔소리 방지). 주 8시간 = 480분/7일.
    assert (
        first_plan_adapter.volume_shortfall_warning(conflicting, planned_minutes=480, span_days=7)
        is None
    )
    # 주당 시간 미입력이면 비교 기준이 없으므로 경고 없음.
    assert (
        first_plan_adapter.volume_shortfall_warning(
            _outcome_with("iv_no_hours", **{"goals.weekly_time": {"type": "chip", "values": []}}),
            planned_minutes=60,
            span_days=7,
        )
        is None
    )


def test_planned_session_length_flows_into_items_and_prompt() -> None:
    """화해된 길이가 leaf 정규화와 분해 프롬프트 양쪽에 실제로 흘러간다(둘이 어긋나면 무의미)."""
    outcome = _outcome_with(
        "iv_recon_flow",
        **{
            "goals.weekly_time": {"type": "chip", "values": ["2시간"]},
            "goals.frequency": {"type": "chip", "values": ["매일"]},
        },
    )
    items = [
        ActionItemDraft(
            node_id="n", title="t", estimated_minutes=m, category="study", first_step="s"
        )
        for m in (9, 45, 120)
    ]
    normalized = first_plan_adapter.normalize_action_minutes(outcome, items)
    # 밴드 [15, 17] 클램프(#225) — 세션 길이(17)가 상한, 15분이 하한.
    assert [i.estimated_minutes for i in normalized] == [15, 17, 17]
    # 프롬프트도 같은 값을 봐야 LLM 이 처음부터 그 길이로 만든다(보정에만 의존하지 않게).
    assert (
        first_plan_adapter.context_from_outcome(outcome)["prompt_vars"]["session_length"] == "17분"
    )


def test_normalize_action_minutes_clamps_to_session_band() -> None:
    """목표별 세션 길이가 있으면 각 세션을 **[15분, 세션 길이] 밴드로 클램프** (#225).

    예전엔 전부 세션 길이로 통일해 '비자 수령 확인' 같은 짧은 처리성 작업까지 120분이 됐다
    (FE 실측). 이제 상한만 강제하고 성격에 맞게 짧게 잡은 값은 존중한다 —
    9분 garbage 는 하한(15분)으로 올린다. 세션 길이 미지정이면 원본 유지.
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
    # 9 → 하한 15, 45·80 → 존중(짧은 처리성 작업), 200 → 상한 90.
    assert [i.estimated_minutes for i in out] == [15, 45, 80, 90]

    # 세션 길이 미지정(전역 fallback) → 원본 그대로.
    heaviest.session_length_min = None
    passthrough = first_plan_adapter.normalize_action_minutes(outcome, items)
    assert [i.estimated_minutes for i in passthrough] == [9, 45, 80, 200]


def test_shape_action_plan_caps_sessions_to_weekly_target() -> None:
    """세션 길이가 크면 LLM 이 세션을 과다 생성해도, 주당 시간 target 로 잘라 overshoot 방지.

    weekly 6시간 + session_length 90분 → target 6*60/90 = 4 세션. LLM 이 8개(각 20분) 내면
    → 정규화(밴드 [15,90] — 20분은 존중, #225) + 4개로 절단 + 고아 leaf 제거.
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
    # 20분짜리는 밴드 안이라 존중(#225) — 부풀리지 않는다. 부족분은 shortfall 경고가 알린다.
    assert all(a.estimated_minutes == 20 for a in shaped.action_items)
    leaf_ids = {n.node_id for n in shaped.goal_nodes if n.is_leaf}
    assert len(leaf_ids) == 4  # 고아 leaf 제거
    assert all(a.node_id in leaf_ids for a in shaped.action_items)


def test_drop_waiting_steps_removes_actions_but_keeps_nodes() -> None:
    """'외부 대기' 단계는 세션에서 빠지고 트리(큰 그림)에는 남는다 (#225).

    회귀(FE 실측): '대학별 지원 패키지 발송 및 입학허가서 대기'·'비자 수령' 이 120분
    세션 카드가 돼 오늘 목록에 남았고, 체크할 수도 실패할 수도 없어 회복 제안이 헛돌았다.
    """
    nodes = [
        GoalNodeDraft(
            node_id="root",
            parent_id=None,
            title="교환학생",
            node_type="root",
            order_index=0,
            is_leaf=False,
        ),
        GoalNodeDraft(
            node_id="l1",
            parent_id="root",
            title="지원서 작성",
            node_type="leaf",
            order_index=0,
            is_leaf=True,
        ),
        GoalNodeDraft(
            node_id="l2",
            parent_id="root",
            title="입학허가서 대기",
            node_type="leaf",
            order_index=1,
            is_leaf=True,
        ),
    ]

    def _a(node_id: str, title: str) -> ActionItemDraft:
        return ActionItemDraft(
            node_id=node_id, title=title, estimated_minutes=120, category="career", first_step="s"
        )

    gp = GoalDecomposition(
        goal_nodes=nodes,
        action_items=[
            _a("l1", "지원서 작성"),
            _a("l2", "대학별 지원 패키지 발송 및 입학허가서 대기"),
        ],
        policy_violations=[],
    )
    filtered, dropped = first_plan_adapter.drop_waiting_steps(gp)
    assert dropped == ["대학별 지원 패키지 발송 및 입학허가서 대기"]
    assert [a.title for a in filtered.action_items] == ["지원서 작성"]
    # 노드는 그대로 — 사용자가 여정의 전체 그림은 본다 (FE 제안 1).
    assert {n.node_id for n in filtered.goal_nodes} == {"root", "l1", "l2"}

    # 대기 단계가 없으면 그대로 통과.
    clean, none_dropped = first_plan_adapter.drop_waiting_steps(filtered)
    assert none_dropped == [] and clean.action_items == filtered.action_items

    # 고지 문구 — 조용히 빼지 않는다.
    notice = first_plan_adapter.waiting_steps_notice(dropped)
    assert notice is not None and "입학허가서 대기" in notice and "큰 그림" in notice
    assert first_plan_adapter.waiting_steps_notice([]) is None
    # 4개 이상이면 앞 3개 + 'N개' 요약.
    many = first_plan_adapter.waiting_steps_notice(["a 대기", "b 대기", "c 대기", "d 대기"])
    assert many is not None and "외 1개" in many


def _milestone_plan(milestones: int, leaves_each: int) -> GoalDecomposition:
    """root → 마일스톤 branch × N → 각 branch 아래 leaf × M 인 분해 결과."""
    nodes = [
        GoalNodeDraft(
            node_id="root",
            parent_id=None,
            title="캡스톤",
            node_type="root",
            order_index=0,
            is_leaf=False,
        )
    ]
    items: list[ActionItemDraft] = []
    for m in range(milestones):
        nodes.append(
            GoalNodeDraft(
                node_id=f"m{m}",
                parent_id="root",
                title=f"마일스톤{m + 1}",
                node_type="branch",
                order_index=m,
                is_leaf=False,
            )
        )
        for j in range(leaves_each):
            leaf = f"m{m}l{j}"
            nodes.append(
                GoalNodeDraft(
                    node_id=leaf,
                    parent_id=f"m{m}",
                    title=f"세션{m}-{j}",
                    node_type="leaf",
                    order_index=j,
                    is_leaf=True,
                )
            )
            items.append(
                ActionItemDraft(
                    node_id=leaf,
                    title=f"세션{m}-{j}",
                    estimated_minutes=120,
                    category="project",
                    first_step="s",
                )
            )
    return GoalDecomposition(goal_nodes=nodes, action_items=items, policy_violations=[])


def test_confirmed_milestones_dropped_by_shaping_are_reported() -> None:
    """확정 마일스톤이 세션 수 상한에 잘려나가면 **고지한다** (ADR-0007 §배경 ①).

    Stage A 에서 5개를 확인받아도 `shape_action_plan` 이 주당 rate 로 자르고
    `_prune_to_leaves` 가 leaf 없는 branch 를 버려 뒤쪽이 통째로 사라졌다 — 그런데
    `warnings` 는 한 줄도 나가지 않았다. 이 레포는 다른 모든 축소(대기 단계·회차 보충·
    하루 상한·케이던스)를 고지하면서 **사용자가 직접 확인한 뼈대**만 침묵하고 있었다.
    """
    start = date(2026, 8, 23)
    outcome = _outcome_with(
        "iv_ms_drop",
        **{
            "goals.deadlines": {"type": "text", "raw": "2026-09-20"},  # 4주
            "goals.frequency": {"type": "chip", "values": ["주 3회"]},
            "goals.session_length": {"type": "chip", "values": ["1시간 30분"]},
        },
    )
    confirmed = [MilestoneDraft(title=f"마일스톤{i}") for i in range(1, 6)]

    shaped = first_plan_adapter.shape_action_plan(
        outcome, "standard", _milestone_plan(5, 4), target_date=start
    )
    # 주 3회 × 4주 = 12세션 → 20개 중 12개만 남고 뒤 두 마일스톤의 branch 가 사라진다.
    assert len(shaped.action_items) == 12
    assert [n.title for n in shaped.goal_nodes if n.node_type == "branch"] == [
        "마일스톤1",
        "마일스톤2",
        "마일스톤3",
    ]

    missing = first_plan_adapter.missing_milestone_titles(confirmed, shaped)
    assert missing == ["마일스톤4", "마일스톤5"]

    notice = first_plan_adapter.missing_milestones_notice(missing, confirmed=len(confirmed))
    assert notice is not None
    assert "마일스톤4" in notice and "마일스톤5" in notice
    assert "다음 계획" in notice  # 사라진 게 아니라 이어받는다고 말한다


def test_missing_milestones_is_quiet_when_every_milestone_has_a_place() -> None:
    """전부 자리를 잡았으면 아무 말도 하지 않는다 — 경고는 실제로 빠졌을 때만."""
    plan = _milestone_plan(3, 2)
    confirmed = [MilestoneDraft(title=f"마일스톤{i}") for i in range(1, 4)]
    assert first_plan_adapter.missing_milestone_titles(confirmed, plan) == []
    assert first_plan_adapter.missing_milestones_notice([], confirmed=3) is None

    # leaf 없이 branch 만 남은 마일스톤은 **경고 대상이 아니다** — 프롬프트가 시킨 정상
    # 동작이고(#225 "구간 밖 뒷단계는 branch 로만"), 사용자는 트리에서 그 단계를 본다.
    # 여기서 알리면 4주를 넘는 거의 모든 계획에 경고가 붙는다.
    front_only = GoalDecomposition(
        goal_nodes=[
            *plan.goal_nodes,
            GoalNodeDraft(
                node_id="m3",
                parent_id="root",
                title="마일스톤4",
                node_type="branch",
                order_index=3,
                is_leaf=False,
            ),
        ],
        action_items=plan.action_items,
        policy_violations=[],
    )
    assert (
        first_plan_adapter.missing_milestone_titles(
            [*confirmed, MilestoneDraft(title="마일스톤4")], front_only
        )
        == []
    )
    # 확정 마일스톤이 없는 계획(Stage A 미사용)은 판정 대상 자체가 아니다.
    assert first_plan_adapter.missing_milestone_titles(None, plan) == []
    assert first_plan_adapter.missing_milestone_titles([], plan) == []


def test_missing_milestones_tolerates_small_title_drift() -> None:
    """제목이 조금 늘거나 공백이 달라도 같은 것으로 본다 — 오탐이 미탐보다 나쁘다.

    프롬프트가 개명을 금지하지만 순응은 확률적이다. 있는 걸 없다고 알리면 사용자는
    멀쩡한 계획을 의심하게 되므로, 양방향 containment 로 보수적으로 판정한다.
    """
    plan = _milestone_plan(2, 2)
    plan.goal_nodes[1].title = "마일스톤1 기초 다지기"  # LLM 이 살짝 늘림
    plan.goal_nodes[4].title = "마일스톤 2"  # 공백만 다름
    confirmed = [MilestoneDraft(title="마일스톤1"), MilestoneDraft(title="마일스톤2")]
    assert first_plan_adapter.missing_milestone_titles(confirmed, plan) == []


def test_missing_milestones_notice_passes_banned_word_filter() -> None:
    """사용자 노출 문구는 금지어 필터(DevBaseline §4.2)를 통과해야 한다."""
    from reaction_backend.safety.banned_words import scan

    notice = first_plan_adapter.missing_milestones_notice(["A", "B", "C", "D"], confirmed=7)
    assert notice is not None
    assert "외 1개" in notice  # 4개 이상이면 앞 3개 + 'N개' 요약
    assert scan(notice) == ()


def test_window_coverage_tells_partial_vs_full_window() -> None:
    """구간 커버리지 변수 — 마감이 구간보다 멀면 '앞부분만' 을 명시해 여정 압축을 막는다 (#225)."""
    start = date(2026, 8, 12)

    # 마감 16주 뒤 → 구간(4주)은 앞부분만.
    far = _outcome_with("iv_wc_far", **{"goals.deadlines": {"type": "text", "raw": "2026-11-30"}})
    vars_far = first_plan_adapter.context_from_outcome(far, target_date=start)["prompt_vars"]
    assert "앞 4주만" in vars_far["window_coverage"]

    # 마감 3주 뒤 → 전부 덮는다.
    near = _outcome_with("iv_wc_near", **{"goals.deadlines": {"type": "text", "raw": "2026-08-30"}})
    vars_near = first_plan_adapter.context_from_outcome(near, target_date=start)["prompt_vars"]
    assert "전부를 덮는다" in vars_near["window_coverage"]

    # 마감 없음(습관형) → 계속되는 리듬의 첫 구간.
    endless = _outcome_with("iv_wc_none", **{"goals.deadlines": {"type": "text", "raw": ""}})
    vars_none = first_plan_adapter.context_from_outcome(endless, target_date=start)["prompt_vars"]
    assert "마감이 없다" in vars_none["window_coverage"]


def test_shape_action_plan_drops_branches_left_without_leaves() -> None:
    """절단으로 leaf 가 **전부** 사라진 branch 는 함께 버린다 — 빈 껍데기 섹션 방지.

    예전엔 비-leaf 를 무조건 살려서, 뒤쪽 마일스톤이 통째로 잘린 경우 자식 없는 branch 가
    남아 화면에 빈 섹션으로 떴다. 살아남은 leaf 의 **조상만** 남긴다.
    """
    outcome = _outcome_with("iv_prune")
    heaviest = next(g for g in outcome.core_goals if g.is_heaviest)
    heaviest.session_length_min = 90  # weekly 6시간 ÷ 90분 → target 4 세션

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
    # branch 2개 × leaf 4개 = 8 세션. target 4 → 앞 branch 만 살아남아야 한다.
    for b in range(2):
        nodes.append(
            GoalNodeDraft(
                node_id=f"branch{b}",
                parent_id="root",
                title=f"b{b}",
                node_type="branch",
                order_index=b,
                is_leaf=False,
            )
        )
        for i in range(4):
            leaf_id = f"leaf{b}_{i}"
            nodes.append(
                GoalNodeDraft(
                    node_id=leaf_id,
                    parent_id=f"branch{b}",
                    title=leaf_id,
                    node_type="leaf",
                    order_index=i,
                    is_leaf=True,
                )
            )
            actions.append(
                ActionItemDraft(
                    node_id=leaf_id,
                    title=leaf_id,
                    estimated_minutes=20,
                    category="study",
                    first_step="s",
                )
            )
    gp = GoalDecomposition(goal_nodes=nodes, action_items=actions, policy_violations=[])

    shaped = first_plan_adapter.shape_action_plan(outcome, "standard", gp)
    kept_ids = {n.node_id for n in shaped.goal_nodes}
    assert len(shaped.action_items) == 4
    assert "branch1" not in kept_ids  # leaf 가 하나도 안 남은 branch 는 제거
    assert "branch0" in kept_ids  # 살아남은 leaf 의 조상은 유지
    assert "root" in kept_ids  # root 도 조상으로 유지
    # 남은 모든 노드가 root 까지 연결된다(끊긴 노드 없음).
    for node in shaped.goal_nodes:
        assert node.parent_id is None or node.parent_id in kept_ids


def _decomposition(n: int, *, violations: list[PolicyViolation] | None = None) -> GoalDecomposition:
    """root + leaf n개짜리 최소 분해 결과."""
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
    items = []
    for i in range(n):
        nodes.append(
            GoalNodeDraft(
                node_id=f"l{i}",
                parent_id="root",
                title=f"{i}",
                node_type="leaf",
                order_index=i,
                is_leaf=True,
            )
        )
        items.append(
            ActionItemDraft(
                node_id=f"l{i}",
                title=f"{i}",
                estimated_minutes=30,
                category="study",
                first_step="s",
            )
        )
    return GoalDecomposition(
        goal_nodes=nodes, action_items=items, policy_violations=violations or []
    )


def test_plan_extends_to_horizon_when_llm_under_generates() -> None:
    """분해가 마감에 못 미치면 '이어가기' 회차로 채운다 — 두 달 목표에 일주일 계획 방지.

    실측 회귀: '매일 30분 알고리즘' 마감 9/30 인데 LLM 이 9세션(일주일치)만 만들어 계획이
    8/5 에서 끝났다. 규칙은 자르기만 하고 채우지 않아 그대로 나갔다.
    """
    outcome = _outcome_with(
        "iv_cov",
        **{
            "goals.frequency": {"type": "chip", "values": ["매일"]},
            "goals.session_length": {"type": "chip", "values": ["30분"]},
            "goals.deadlines": {"type": "text", "raw": "2026-09-30"},
        },
    )
    start = date(2026, 7, 28)
    # 마감까지 9주(상한 4주=한 달로 캡) × 주 7회 = 28 세션이 목표.
    extended = first_plan_adapter.extend_action_plan_to_horizon(
        outcome, "standard", _decomposition(9), target_date=start
    )
    assert len(extended.action_items) == 28
    assert all(a.estimated_minutes == 30 for a in extended.action_items[9:])  # 세션 길이 유지
    assert extended.action_items[9].title.endswith("10회차")  # 원본 뒤로 번호가 이어진다
    # 덧붙인 노드도 트리에 연결된다(끊긴 노드 없음).
    ids = {n.node_id for n in extended.goal_nodes}
    for n in extended.goal_nodes:
        assert n.parent_id is None or n.parent_id in ids

    # 이미 충분하면 건드리지 않는다.
    assert (
        len(
            first_plan_adapter.extend_action_plan_to_horizon(
                outcome, "standard", _decomposition(28), target_date=start
            ).action_items
        )
        == 28
    )


def test_plan_extension_respects_finite_goals_and_missing_cadence() -> None:
    """유한한 목표·빈도 미지정은 보충하지 않는다 — 의미 없는 회차를 붙이지 않으려고."""
    start = date(2026, 7, 28)
    cadence = _outcome_with(
        "iv_cov_finite",
        **{
            "goals.frequency": {"type": "chip", "values": ["매일"]},
            "goals.deadlines": {"type": "text", "raw": "2026-09-30"},
        },
    )
    # LLM 이 '이 목표는 유한해서 더 못 채운다' 고 스스로 밝히면 그 판단을 존중한다.
    flagged = _decomposition(
        9, violations=[PolicyViolation(node_id="root", reason="goal_volume_below_horizon")]
    )
    assert (
        len(
            first_plan_adapter.extend_action_plan_to_horizon(
                cadence, "standard", flagged, target_date=start
            ).action_items
        )
        == 9
    )

    # 빈도를 안 준 목표('몰아서')는 반복이 자연스럽지 않으므로 보충 대상이 아니다.
    no_cadence = _outcome_with(
        "iv_cov_nofreq", **{"goals.deadlines": {"type": "text", "raw": "2026-09-30"}}
    )
    assert (
        len(
            first_plan_adapter.extend_action_plan_to_horizon(
                no_cadence, "standard", _decomposition(9), target_date=start
            ).action_items
        )
        == 9
    )


def test_plan_extension_fills_deadline_less_habits_to_the_horizon() -> None:
    """마감 없는 습관형도 지평(4주) 끝까지 보충한다 — 마지막 8일이 조용히 비지 않게.

    회귀(라이브 실측): '매일 30분 달리기'(마감 없음)가 지평은 4주(v1.41)인데 세션은
    LLM 상한(20)에서 끊겨 20블록/20일로 끝났고 경고도 없었다. 성공 기준이 "한 달
    하루도 안 빼먹기"인 사용자에게 계획 자체가 8일 결번이었다. 마감 없음은 '기준 없음'이
    아니라 지평 전체(_horizon_weeks → _MAX_PLAN_WEEKS)가 기준이다.
    """
    start = date(2026, 7, 28)
    no_deadline = _outcome_with(
        "iv_cov_nodl",
        **{
            "goals.frequency": {"type": "chip", "values": ["매일"]},
            "goals.session_length": {"type": "chip", "values": ["30분"]},
            "goals.deadlines": {"type": "text", "raw": ""},
        },
    )
    extended = first_plan_adapter.extend_action_plan_to_horizon(
        no_deadline, "standard", _decomposition(20), target_date=start
    )
    # 매일 × 4주 지평 = 28 — LLM 20개 + 이어가기 8개.
    assert len(extended.action_items) == 28
    assert extended.action_items[20].title.endswith("21회차")

    # 보충 고지 문구 — 마감이 없으니 "마감까지" 라고 지어내지 않는다.
    warning = first_plan_adapter.coverage_extended_warning(8, None)
    assert warning is not None and "이번 계획 구간(4주)" in warning
    assert "마감까지" not in warning


def test_decompose_prompt_gets_precomputed_horizon_numbers() -> None:
    """프롬프트가 '남은 주 수'·'총 세션 수'를 **계산된 값**으로 받는다.

    예전엔 마감 날짜만 주고 "남은 주 수에 비례해 만들라" 고 시켰는데, 프롬프트에 오늘 날짜가
    없어 LLM 이 그 계산을 할 수 없었다 — 그래서 마감이 두 달 뒤여도 한 주치만 만들었다.
    """
    outcome = _outcome_with(
        "iv_hz",
        **{
            "goals.frequency": {"type": "chip", "values": ["매일"]},
            "goals.deadlines": {"type": "text", "raw": "2026-09-30"},
        },
    )
    pv = first_plan_adapter.context_from_outcome(outcome, target_date=date(2026, 7, 28))[
        "prompt_vars"
    ]
    assert pv["target_date"] == "2026-07-28"
    assert pv["horizon_weeks"] == "4"  # 9주지만 _MAX_PLAN_WEEKS(한 달)로 캡
    assert pv["sessions_per_week"] == "7"

    # 계획이 목표로 하는 총량은 28(7 × 4주)이지만, **한 호출에 요구하는 양**은 20 으로 묶는다.
    # 실측: 4주 캡에서도 28세션을 요구하면 20s 타임아웃 → 룰 폴백 → 전 구간 자리표시자가 된다.
    # 초과분(8개)은 extend_action_plan_to_horizon 이 '이어가기' 회차로 채운다.
    assert (
        first_plan_adapter.horizon_session_target(
            outcome, "standard", target_date=date(2026, 7, 28)
        )
        == 28
    )
    assert pv["total_sessions"] == "20"

    # 지평 목표가 상한보다 작으면 그대로 요구한다(불필요하게 줄이지 않는다).
    light = _outcome_with(
        "iv_hz_light",
        **{
            "goals.frequency": {"type": "chip", "values": ["주 3회"]},
            "goals.deadlines": {"type": "text", "raw": "2026-08-25"},
        },
    )
    lp = first_plan_adapter.context_from_outcome(light, target_date=date(2026, 7, 28))[
        "prompt_vars"
    ]
    assert lp["total_sessions"] == "12"  # 3 × 4주 — 상한(20) 미만이라 그대로

    # target_date 미지정이면 1주치(하위호환) — 계산 근거가 없으니 부풀리지 않는다.
    assert first_plan_adapter.context_from_outcome(outcome)["prompt_vars"]["horizon_weeks"] == "1"


def test_horizon_coverage_notice_explains_why_plan_ends_early() -> None:
    """계획이 마감 전에 끝나면 **이유를 말한다** — 말 안 하면 사용자가 버그로 읽는다.

    한 달 상한은 의도된 설계다(먼 미래를 자리표시자로 채우는 대신 매주 재계획이 이어감).
    의도된 동작일수록 침묵하면 안 된다.
    """
    far = _outcome_with("iv_hc_far", **{"goals.deadlines": {"type": "text", "raw": "2026-09-30"}})
    start = date(2026, 7, 28)

    # 상한에 걸린 경우 — 9주 필요한데 4주까지만.
    capped = first_plan_adapter.horizon_coverage_notice(
        far, last_planned_day=date(2026, 9, 21), target_date=start
    )
    assert capped is not None
    assert "4주" in capped and "2026-09-21" in capped
    assert "빠뜨린 게 아니에요" in capped  # 버그 아님을 분명히

    # 상한이 아니라 분량이 모자라 일찍 끝난 경우 — 다른 안내(분량을 올리라고).
    near = _outcome_with("iv_hc_near", **{"goals.deadlines": {"type": "text", "raw": "2026-08-25"}})
    short = first_plan_adapter.horizon_coverage_notice(
        near, last_planned_day=date(2026, 8, 5), target_date=start
    )
    assert short is not None
    assert "분량" in short
    assert "4주" not in short  # 상한 얘기를 꺼내면 안 된다(원인이 다름)

    # 마감까지 닿았으면 아무 말도 안 한다(잡음 방지) — 며칠 여유는 덮은 것으로 본다.
    assert (
        first_plan_adapter.horizon_coverage_notice(
            near, last_planned_day=date(2026, 8, 24), target_date=start
        )
        is None
    )
    # 마감 없는 습관형 목표는 비교 기준이 없다.
    no_dl = _outcome_with("iv_hc_nodl", **{"goals.deadlines": {"type": "text", "raw": ""}})
    assert (
        first_plan_adapter.horizon_coverage_notice(
            no_dl, last_planned_day=date(2026, 8, 5), target_date=start
        )
        is None
    )
    # 배치가 아예 없으면 할 말이 없다.
    assert (
        first_plan_adapter.horizon_coverage_notice(far, last_planned_day=None, target_date=start)
        is None
    )


# ─────────── 만다라 유래 목표 2주 지평(ADR-0008 §3) — max_weeks 파라미터 ───────────


def test_max_plan_weeks_for_mandala_vs_default() -> None:
    assert first_plan_adapter.max_plan_weeks_for(is_mandala_derived=True) == 2
    assert first_plan_adapter.max_plan_weeks_for(is_mandala_derived=False) == 4


def test_horizon_session_target_respects_custom_max_weeks() -> None:
    """max_weeks 를 안 넘기면 기존과 100% 동일(하위호환) — 넘기면 그 상한을 쓴다."""
    outcome = _outcome_with(
        "iv_mw",
        **{
            "goals.frequency": {"type": "chip", "values": ["매일"]},
            "goals.deadlines": {"type": "text", "raw": "2026-09-30"},  # 9주 뒤
        },
    )
    start = date(2026, 7, 28)

    assert (
        first_plan_adapter.horizon_session_target(outcome, "standard", target_date=start) == 28
    )  # 기본 4주 캡 — 회귀 없음
    assert (
        first_plan_adapter.horizon_session_target(
            outcome, "standard", target_date=start, max_weeks=2
        )
        == 14
    )  # 매일 × 2주


def test_context_from_outcome_prompt_vars_use_mandala_cap() -> None:
    """만다라 유래 목표는 프롬프트에도 2주치로 계산된 숫자가 실린다 — LLM 이 4주치를

    지어내지 않게(분해가 실제로 담을 양과 프롬프트가 말하는 양이 어긋나면 안 된다)."""
    outcome = _outcome_with(
        "iv_mw_ctx",
        **{
            "goals.frequency": {"type": "chip", "values": ["매일"]},
            "goals.deadlines": {"type": "text", "raw": "2026-09-30"},
        },
    )
    pv = first_plan_adapter.context_from_outcome(
        outcome, target_date=date(2026, 7, 28), max_weeks=2
    )["prompt_vars"]

    assert pv["horizon_weeks"] == "2"
    assert pv["total_sessions"] == "14"  # 7 × 2주 (상한 20 미만이라 그대로)
    assert "2주" in pv["window_coverage"]


def test_shape_and_extend_respect_mandala_cap() -> None:
    """shape_action_plan(자르기)·extend_action_plan_to_horizon(보충) 둘 다 2주 기준으로 움직인다."""
    outcome = _outcome_with(
        "iv_mw_shape",
        **{
            "goals.frequency": {"type": "chip", "values": ["매일"]},
            "goals.deadlines": {"type": "text", "raw": "2026-09-30"},
        },
    )
    start = date(2026, 7, 28)

    # LLM 이 20개를 냈어도 2주 캡(매일×2주=14)까지만 남긴다.
    shaped = first_plan_adapter.shape_action_plan(
        outcome, "standard", _decomposition(20), target_date=start, max_weeks=2
    )
    assert len(shaped.action_items) == 14

    # LLM 이 5개만 냈으면 2주치(14)까지 보충한다 — 4주치(28)까지 채우면 안 된다.
    extended = first_plan_adapter.extend_action_plan_to_horizon(
        outcome, "standard", _decomposition(5), target_date=start, max_weeks=2
    )
    assert len(extended.action_items) == 14


def test_horizon_coverage_notice_mentions_two_weeks_for_mandala_goal() -> None:
    far = _outcome_with("iv_mw_cov", **{"goals.deadlines": {"type": "text", "raw": "2026-09-30"}})
    start = date(2026, 7, 28)

    capped = first_plan_adapter.horizon_coverage_notice(
        far, last_planned_day=date(2026, 8, 10), target_date=start, max_weeks=2
    )

    assert capped is not None
    assert "2주" in capped
    assert "4주" not in capped


def test_coverage_extended_warning_mentions_two_weeks_for_mandala_goal() -> None:
    warning = first_plan_adapter.coverage_extended_warning(5, None, max_weeks=2)
    assert warning is not None
    assert "이번 계획 구간(2주)" in warning


def test_materials_link_only_is_treated_as_no_content() -> None:
    """참고 자료가 **링크뿐**이면 '(없음)' 으로 내려 LLM 이 내용을 지어내지 못하게 한다.

    실측 회귀: 강의 URL 하나만 붙여넣었더니 프롬프트는 '자료 있음' 으로 받아, LLM 이 존재
    여부도 모르는 '20강' 구성(1~5강/6~10강/11~15강/16~20강)을 만들어냈고 policy_violations 는
    비어 있었다. 우리는 링크를 열어볼 수 없으니 내용이 없는 것과 같다.
    """
    link_only = _outcome_with(
        "iv_link",
        **{
            "goals.materials": {
                "type": "text",
                "raw": "https://academy.lgresearch.ai/studyroom/f105273d2e",
            }
        },
    )
    assert first_plan_adapter.materials_is_link_only(
        "https://academy.lgresearch.ai/studyroom/f105273d2e"
    )
    ctx = first_plan_adapter.context_from_outcome(link_only)
    assert ctx["prompt_vars"]["materials"] == "(없음)"  # 프롬프트 미제공 flag 규칙이 걸리게
    # 결정적 경고로도 되묻는다 — LLM 순응에 기대지 않는다.
    warning = first_plan_adapter.materials_link_only_warning(link_only)
    assert warning is not None and "링크" in warning

    # #226 이후: 열어봤으면 그 본문이 실리고 되묻지 않는다.
    opened = first_plan_adapter.context_from_outcome(link_only, fetched_materials="1주차 OT")
    assert "1주차 OT" in opened["prompt_vars"]["materials"]  # 울타리 안에 실린다
    assert first_plan_adapter.materials_link_only_warning(link_only, fetched=True) is None
    # 못 열었으면 **왜** 못 열었는지를 말한다 — 사유가 없을 때만 기존 문구로 폴백.
    assert (
        first_plan_adapter.materials_link_only_warning(
            link_only, fetch_notice="로그인이 필요한 페이지라 제가 열 수 없었어요."
        )
        == "로그인이 필요한 페이지라 제가 열 수 없었어요."
    )
    # 링크 + 설명이면 설명이 실제 내용이므로 그대로 싣는다.
    mixed = _outcome_with(
        "iv_mixed",
        **{
            "goals.materials": {
                "type": "text",
                "raw": "https://ex.com/syllabus 1주차 스레드, 2주차 VM, 3주차 파일시스템",
            }
        },
    )
    assert not first_plan_adapter.materials_is_link_only(
        "https://ex.com/syllabus 1주차 스레드, 2주차 VM, 3주차 파일시스템"
    )
    assert (
        "1주차 스레드" in first_plan_adapter.context_from_outcome(mixed)["prompt_vars"]["materials"]
    )
    assert first_plan_adapter.materials_link_only_warning(mixed) is None
    # 자료가 애초에 링크뿐이 아니면 fetch 결과와 **무관하게** 조용하다.
    # (예전엔 이 주석이 link_only 를 넘기고 있어 위 단언의 중복이었다 — 실제로 검사된 적 없음.)
    assert first_plan_adapter.materials_link_only_warning(mixed, fetched=True) is None
    assert (
        first_plan_adapter.materials_link_only_warning(
            mixed, fetch_notice="로그인이 필요한 페이지라 제가 열 수 없었어요."
        )
        is None
    )

    # 원문만 붙여넣은 기존 경로는 그대로(회귀 방지).
    plain = _outcome_with("iv_plain")
    assert first_plan_adapter.materials_link_only_warning(plain) is None


async def test_validating_opens_a_link_only_material(monkeypatch: pytest.MonkeyPatch) -> None:
    """VALIDATING 노드가 링크를 열어 프롬프트 변수까지 채운다 (#226).

    I/O 는 이 노드가 하고 `context_from_outcome` 은 순수 함수로 남는다 — 그 분업이
    깨지면 어댑터가 네트워크를 타게 되어 파일 계약("순수 함수")이 거짓이 된다.
    """
    from reaction_backend.integrations.web_fetch import fetcher as _fetcher

    outcome = _outcome_with(
        "iv_fetch",
        **{"goals.materials": {"type": "text", "raw": "https://lecture.example/syllabus"}},
    )

    async def _ok(url: str) -> Any:
        return _fetcher.FetchResult("1주차 OT\n2주차 스레드", None)

    monkeypatch.setattr(first_plan.materials_resolver.fetcher, "fetch_text", _ok)
    cfg: Any = {"configurable": {}}
    state = first_plan.initial_state(user_id=uuid4(), outcome=outcome, target_date="2026-06-01")
    state = await first_plan.validate_inputs(state, cfg)

    assert state["materials_fetched"] is True
    assert state["materials_notice"] is None
    assert "2주차 스레드" in state["planning_context"]["prompt_vars"]["materials"]


async def test_validating_falls_back_when_the_link_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """못 열면 **예전 그대로** '(없음)' — 지어내기 방지 가드가 계속 작동한다."""
    from reaction_backend.integrations.web_fetch import fetcher as _fetcher

    outcome = _outcome_with(
        "iv_fetch_fail",
        **{"goals.materials": {"type": "text", "raw": "https://private.example/x"}},
    )

    async def _fail(url: str) -> Any:
        return _fetcher.FetchResult(None, _fetcher.REASON_LOGIN_REQUIRED)

    monkeypatch.setattr(first_plan.materials_resolver.fetcher, "fetch_text", _fail)
    cfg: Any = {"configurable": {}}
    state = first_plan.initial_state(user_id=uuid4(), outcome=outcome, target_date="2026-06-01")
    state = await first_plan.validate_inputs(state, cfg)

    assert state["materials_fetched"] is False
    assert state["materials_notice"] and "로그인" in state["materials_notice"]
    assert state["planning_context"]["prompt_vars"]["materials"] == "(없음)"


def test_peak_windows_for_plan_ranks_goal_time_then_global() -> None:
    """목표별 preferred_time 이 **1순위**, 전역 peak 이 **2순위 폴백** (순서 = 우선순위).

    예전엔 목표별이 있으면 전역을 버렸다. 그러면 목표별 창이 막혔을 때(이미 차거나 지나감)
    곧바로 활동창 전체 폴백으로 떨어져, 사용자가 답한 전역 집중 시간대가 배치에 한 번도
    안 쓰였다(실측: 오후 창이 지난 저녁에 22:15 배치). 두 시간대를 각각 묻는 이상 둘 다
    쓰여야 한다.
    """
    from datetime import time

    outcome = interview_adapter.build_outcome(
        session_id="iv_pt",
        slot_answers=SLOT_ANSWERS,  # 전역 peak=["오전","저녁"], 목표 preferred_time="오전"
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    wins = first_plan_adapter.peak_windows_for_plan(outcome)
    assert wins[0].start == time(6, 0) and wins[0].end == time(12, 0)  # 1순위 = 목표별(오전)
    # 목표 창과 겹치는 전역 '오전' 은 중복 제거되고, 나머지 전역 '저녁' 이 폴백으로 남는다.
    assert [(w.start, w.end) for w in wins[1:]] == [(time(18, 0), time(23, 0))]

    # 목표별 선호가 '저녁' 이면 저녁이 1순위 — 전역 '오전' 이 시각상 이르다고 이기면 안 된다.
    heaviest = next(g for g in outcome.core_goals if g.is_heaviest)
    heaviest.preferred_time = "저녁"
    evening_first = first_plan_adapter.peak_windows_for_plan(outcome)
    assert evening_first[0].start == time(18, 0)
    assert evening_first[1].start == time(6, 0)  # 전역 오전은 뒤로

    # preferred_time 없으면 전역 peak(오전+저녁 2창)만.
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
    # 창 밖 배치는 의도된 예외이므로 사용자에게 그 사실을 알린다.
    warning = first_plan_adapter.preferred_time_extension_warning(outcome)
    assert warning is not None and "오전" in warning


def test_preferred_time_overlapping_activity_does_not_extend() -> None:
    """선호 창이 활동창과 **겹치면** 활동창 밖을 열지 않는다 — 활동창이 이긴다.

    회귀: 선호 '오전'(06~12)을 무조건 가용에 합쳐, 활동창을 09:00~ 로 답한 사용자의
    계획 20블록이 전부 06:00 에 배치됐다(라이브 실측). 활동창 질문이 "이 시간 밖엔
    일정을 안 잡아요" 인 이상, 겹침이 있으면 free∩선호 교차(09~12)로 배치돼야 한다.
    창 밖 확장은 교집합이 0인 '아침 운동' 케이스(위 테스트)에만 열린다.
    """
    from datetime import date

    from reaction_backend.orchestrator.goal_structuring import (
        compute_free_blocks,
        time_policies_to_busy,
    )

    outcome = interview_adapter.build_outcome(
        session_id="iv_clip",
        slot_answers=SLOT_ANSWERS,  # 활동 09:00~23:00 + 선호 '오전' — 교집합 09~12
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    pols = first_plan_adapter.time_policies_from_outcome(outcome)
    day = date(2026, 7, 23)
    free = compute_free_blocks(day, time_policies_to_busy(day, pols))
    assert free, "가용 구간이 아예 없다"
    # 06:00 이 열려 있으면 안 된다 — 하루의 가용 시작은 활동창 시작(09:00)이다.
    earliest = min(f.start.time() for f in free)
    assert earliest.hour == 9, [(str(f.start.time()), str(f.end.time())) for f in free]
    # 겹침 케이스는 창 밖 배치가 없으므로 경고도 없어야 한다.
    assert first_plan_adapter.preferred_time_extension_warning(outcome) is None


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
    """요약 변수가 마감·성공 이미지·휴식 수용·다운스코프 단위까지 실어낸다 (P1-4)."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = dict(SLOT_ANSWERS)

    v = interview._summary_variables(state)
    assert v["deadlines"] == "2026-06-20"
    assert v["success_image"] == "데모 동작"
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
    from datetime import datetime

    from reaction_backend.schemas.common import KST

    # 고정 — 하베스팅되는 "2026-08-20" 마감이 `_is_past_deadline`(#231)에 안 걸리게 "오늘"을
    # 그 이전으로 얼린다. 얼리지 않으면 실제 시계가 그 날짜를 지나는 순간 이 슬롯이 "지난
    # 마감"으로 판정돼 프리필에서 조용히 빠지고(#231 의 의도된 동작), 이 테스트는 실제 날짜에
    # 따라 통과/실패가 갈리는 시한폭탄이 된다.
    monkeypatch.setattr(interview, "now_kst", lambda: datetime(2026, 8, 15, 9, 0, tzinfo=KST))

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


async def test_harvest_excludes_per_goal_slots_until_goal_is_attributed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """목표가 여러 개인데 heaviest 미확정이면 목표별 슬롯은 하베스팅 후보에서 빠진다.

    회귀(라이브 실측): "토익도, 캡스톤도, 운동도 주 3회는 하고 싶어요" 에서 **운동의**
    '주 3회'가 heaviest(캡스톤)의 goals.frequency 로 저장됐다. 빈도 질문이 스킵되고
    weekly_time 조건부 질문(v1.42)까지 증발해 사용자가 정정할 기회가 없었다.
    """
    seen: dict[str, str] = {}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        seen["open_slots"] = kwargs["variables"]["open_slots"]
        # LLM 이 규칙을 어기고 목표별 슬롯을 돌려줘도(순응 불확실) 저장되면 안 된다.
        return RunResult(
            value=SlotHarvest(
                slots=[
                    HarvestedSlot(
                        slot_key="goals.frequency", normalized_value="주 3회", confidence=0.9
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
        "goals.list": {
            "type": "text",
            "raw": "토익, 캡스톤, 운동",
            "normalized": ["토익 900점", "캡스톤 마무리", "운동 주 3회"],
        }
    }
    config: Any = {"configurable": {"session": None, "slot_meta": _HARVEST_META}}

    new_state = await interview.harvest_slots(
        state,
        config,
        answer_text="토익도 따야 하고, 캡스톤도 해야 하고, 운동도 주 3회는 하고 싶어요",
        answered_slot="goals.list",
    )

    # 후보 목록 자체에서 빠진다 — 프롬프트에 안 실리므로 LLM 이 채울 방법이 없다.
    assert "goals.frequency" not in seen["open_slots"]
    assert "goals.deadlines" not in seen["open_slots"]
    assert "identity.role" in seen["open_slots"]  # 목표 무관 슬롯은 그대로 후보
    # 돌려줘도 저장되지 않는다(open_set 필터) — 목표 무관 슬롯만 채워진다.
    assert new_state["harvested"] == ["identity.role"]
    assert "goals.frequency" not in new_state["slot_answers"]


async def test_harvest_allows_per_goal_slots_after_heaviest_is_chosen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """heaviest 가 정해진 뒤에는 목표별 슬롯도 다시 하베스팅된다 — 이후 답은 그 목표 얘기다."""

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        assert "goals.deadlines" in kwargs["variables"]["open_slots"]
        return RunResult(
            value=SlotHarvest(
                slots=[
                    HarvestedSlot(
                        slot_key="goals.deadlines", normalized_value="2026-11-30", confidence=0.9
                    )
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
        "goals.list": {
            "type": "text",
            "raw": "토익, 캡스톤",
            "normalized": ["토익 900점", "캡스톤 마무리"],
        },
        "goals.heaviest": {"type": "chip", "values": ["캡스톤 마무리"]},
    }
    config: Any = {"configurable": {"session": None, "slot_meta": _HARVEST_META}}

    new_state = await interview.harvest_slots(
        state,
        config,
        answer_text="캡스톤은 11월 말까지 발표까지 끝내야 해요. 백엔드부터 마무리할 거예요.",
        answered_slot="goals.current_level",
    )
    assert new_state["harvested"] == ["goals.deadlines"]
    assert new_state["slot_answers"]["goals.deadlines"] == {"type": "text", "raw": "2026-11-30"}


async def test_harvest_noop_when_no_open_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """채울 미충족 슬롯이 없으면 LLM 호출 없이 빈 결과 — 불필요한 호출/비용 방지."""
    called = {"n": 0}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:  # pragma: no cover - 호출되면 실패
        called["n"] += 1
        raise AssertionError("harvest should not call LLM when no open slots")

    monkeypatch.setattr(aiClient, "run", fake_run)

    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = {
        k: {"type": "text", "raw": "x"} for k in interview_catalog.PLAN_CATALOG.required_keys
    }
    config: Any = {"configurable": {"session": None, "slot_meta": {}}}

    new_state = await interview.harvest_slots(
        state, config, answer_text="뭐든", answered_slot="goals.list"
    )
    assert new_state["harvested"] == []
    assert called["n"] == 0


async def test_harvest_skips_short_answers_without_calling_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """짧은 답(20자 미만)에는 LLM 을 부르지 않는다 — 실측상 거기서 캘 게 없다.

    근거: 자유서술 답 173개 중 **78.6%가 20자 미만**이고 중앙 길이가 13자다. 인터뷰가 한
    번에 한 질문씩(추천 답변 카드까지) 묻기 때문에 사용자는 물어본 것에만 답한다. 그 결과
    하베스팅은 273회 호출해 9회(3.3%)만 수확했고, 나머지는 회당 약 1,010 토큰과 1초를 쓰고
    빈 배열을 돌려받았다.
    """
    called = {"n": 0}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:  # pragma: no cover - 호출되면 실패
        called["n"] += 1
        raise AssertionError("짧은 답에는 하베스팅 LLM 을 부르면 안 된다")

    monkeypatch.setattr(aiClient, "run", fake_run)

    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    config: Any = {"configurable": {"session": None, "slot_meta": _HARVEST_META}}

    # 실측 중앙값(13자)에 해당하는 전형적인 답.
    new_state = await interview.harvest_slots(
        state, config, answer_text="백준 브론즈 수준", answered_slot="goals.current_level"
    )

    assert new_state["harvested"] == []
    assert called["n"] == 0
    # 슬롯을 건드리지도 않았다 — 게이트는 '조용히 통과' 가 아니라 '아무 일도 안 함' 이다.
    assert new_state["slot_answers"] == state["slot_answers"]


async def test_harvest_still_runs_for_long_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """게이트를 넘는 길이면 종전대로 동작한다 — 기능을 끈 게 아니라 좁힌 것이다."""
    from datetime import datetime

    from reaction_backend.schemas.common import KST

    # 고정 — 위 test_harvest_prefills_confident_unfilled_slots 와 같은 이유(#231 지난 마감
    # 게이트가 실제 날짜에 따라 이 테스트를 갈랐다).
    monkeypatch.setattr(interview, "now_kst", lambda: datetime(2026, 8, 15, 9, 0, tzinfo=KST))

    called = {"n": 0}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        called["n"] += 1
        return RunResult(
            value=SlotHarvest(
                slots=[
                    HarvestedSlot(
                        slot_key="goals.deadlines", normalized_value="2026-08-20", confidence=0.9
                    )
                ]
            ),
            fell_back=False,
            reason=None,
            prompt_id="interview/slot_extraction",
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", fake_run)

    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    # 실제 흐름과 동일하게, validate 가 먼저 저장한 goals.list(단일 목표)를 깔아 둔다 —
    # 목표별 슬롯(goals.deadlines) 하베스팅은 귀속이 확정된 상태에서만 허용된다.
    state["slot_answers"] = {
        "goals.list": {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]}
    }
    config: Any = {"configurable": {"session": None, "slot_meta": _HARVEST_META}}

    long_answer = "캡스톤은 8월 20일 마감이고 난 3학년이고 오전에 집중이 잘돼"
    assert len(long_answer) >= interview.HARVEST_MIN_ANSWER_CHARS
    new_state = await interview.harvest_slots(
        state, config, answer_text=long_answer, answered_slot="goals.list"
    )

    assert called["n"] == 1
    assert new_state["harvested"] == ["goals.deadlines"]


async def test_harvest_gate_measures_by_stripped_length() -> None:
    """공백을 뺀 길이로 판정한다 — 공백으로 게이트를 넘기지 못하게."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    config: Any = {"configurable": {"session": None, "slot_meta": _HARVEST_META}}

    padded = "  짧은 답  " + " " * 40
    assert len(padded) >= interview.HARVEST_MIN_ANSWER_CHARS  # 원문은 길지만
    new_state = await interview.harvest_slots(
        state, config, answer_text=padded, answered_slot="goals.list"
    )
    assert new_state["harvested"] == []  # 실제 내용은 짧다 → 호출 없음


async def test_no_deadline_habit_gets_full_horizon_not_one_week() -> None:
    """마감 없는 습관형 목표도 지평 전체(4주)를 받는다.

    회귀 배경: `_horizon_weeks` 가 마감이 없으면 **1주**를 돌려줬다. '마감 없음' 은 *짧다* 가
    아니라 *끝이 없다* 는 뜻인데 정반대로 읽은 것이다. 그 결과 '주 3회 러닝' 같은 습관형이
    3세션 / 7일짜리 계획을 받고 끝났다 — 마감 있는 목표는 4주를 받는데 습관만 1주라,
    사용자에게는 계획이 안 만들어진 것으로 보인다(실측 지적).
    """
    habit = _outcome_with(
        "iv_habit_nodl",
        **{
            "goals.frequency": {"type": "chip", "values": ["주 3회"]},
            "goals.deadlines": {"type": "text", "raw": ""},  # 마감 미입력(스킵)
        },
    )
    assert habit.horizon is None  # 마감 미입력

    target = date(2026, 7, 28)
    pv = first_plan_adapter.context_from_outcome(habit, target_date=target)["prompt_vars"]
    assert pv["horizon_weeks"] == "4"
    # 주 3회 × 4주 = 12세션. 예전엔 3(= 1주치)이었다.
    assert first_plan_adapter.horizon_session_target(habit, "standard", target_date=target) == 12


async def test_no_deadline_still_bounded_by_max_plan_weeks() -> None:
    """마감이 없어도 무한히 뻗지 않는다 — 지평 상한이 바운드다.

    배치 창(`schedule_blocks`)이 세션 수에서 파생되므로, 세션 수가 캡되지 않으면 먼 미래까지
    블록이 깔린다. 상한이 실제로 걸리는지 고정한다.
    """
    daily = _outcome_with(
        "iv_habit_daily",
        **{
            "goals.frequency": {"type": "chip", "values": ["매일"]},
            "goals.deadlines": {"type": "text", "raw": ""},
        },
    )
    target = date(2026, 7, 28)
    # 매일(7) × 4주 = 28 — _MAX_PLAN_WEEKS 를 넘지 않는다.
    assert first_plan_adapter.horizon_session_target(daily, "standard", target_date=target) == 28
    # 한 호출에 LLM 에 요구하는 양은 여전히 _MAX_LLM_SESSIONS 로 묶인다.
    pv = first_plan_adapter.context_from_outcome(daily, target_date=target)["prompt_vars"]
    assert pv["total_sessions"] == "20"


async def test_missing_target_date_still_falls_back_to_one_week() -> None:
    """`target_date` 자체가 없으면 계산 기준이 없어 1주(하위호환) — 마감 없음과 구분한다."""
    habit = _outcome_with(
        "iv_habit_no_target",
        **{
            "goals.frequency": {"type": "chip", "values": ["주 3회"]},
            "goals.deadlines": {"type": "text", "raw": ""},
        },
    )
    assert first_plan_adapter.horizon_session_target(habit, "standard") == 3


# ── 주당 시간 유도 (묻지 않고 계산) ─────────────────────────────────────────


def test_weekly_hours_derived_from_length_and_frequency() -> None:
    """주당 시간 = 세션 길이 × 빈도. 셋 다 묻지 않는다.

    실측 근거: 세 답이 모두 있는 세션 8개 중 **4개가 1시간 이상 어긋났다**(최대 6시간).
    '주 8시간 · 한 번에 2시간 · 매일'(= 실제 14시간) 같은 답이 거짓이 아니라, 머릿속에서
    2×7 을 하지 않았을 뿐이다. 사람이 잘 답하는 것(체감 길이 · 달력 빈도)만 묻고 곱셈은
    코드가 한다.
    """
    sa = {
        "goals.session_length": {"type": "chip", "values": ["2시간"]},
        "goals.frequency": {"type": "chip", "values": ["매일"]},
    }
    assert interview_adapter.derived_weekly_hours(sa) == 14.0
    # 계산되면 주당 시간을 **묻지 않는다**.
    assert interview_adapter.is_slot_needed("goals.weekly_time", sa) is False


def test_weekly_time_still_asked_when_frequency_unknown() -> None:
    """빈도를 '상관없음/몰아서'로 답하면 계산이 안 되므로 주당 시간을 묻는다."""
    sa = {
        "goals.session_length": {"type": "chip", "values": ["1시간"]},
        "goals.frequency": {"type": "chip", "values": ["상관없음"]},
    }
    assert interview_adapter.derived_weekly_hours(sa) is None
    assert interview_adapter.is_slot_needed("goals.weekly_time", sa) is True


def test_explicit_weekly_time_wins_over_derived() -> None:
    """이미 답한 주당 시간이 유도값을 이긴다 — 저장된 세션의 해석을 바꾸지 않는다.

    유도를 우선하면 과거 데이터의 계획 분량이 말없이 바뀐다: 실측에 있는 '주 8시간 · 한 번
    2시간 · 매일' 세션이 14시간이 되어 부하가 두 배가 된다. 사용자가 이미 승인한 계획의
    전제를 뒤에서 바꾸는 셈이라 하위호환을 택한다. 새 인터뷰는 애초에 묻지 않으므로
    자연히 유도값을 쓴다.
    """
    legacy = _outcome_with(
        "iv_legacy_conflict",
        **{
            "goals.weekly_time": {"type": "chip", "values": ["8시간 이상"]},
            "goals.session_length": {"type": "chip", "values": ["2시간"]},
            "goals.frequency": {"type": "chip", "values": ["매일"]},
        },
    )
    heaviest = next(g for g in legacy.core_goals if g.is_heaviest)
    assert heaviest.weekly_hours == 8  # 유도했다면 14


def test_fsm_and_outcome_agree_on_skipping() -> None:
    """FSM 이 건너뛴 슬롯은 unresolved 에도 남지 않는다.

    한쪽만 건너뛰면 묻지도 않은 슬롯이 영영 미해결로 남아 인터뷰가 끝나지 않는다.
    """
    filled = {
        k: {"type": "text", "raw": "x"}
        for k in interview_adapter.REQUIRED_SLOT_KEYS
        if k != "goals.weekly_time"
    }
    filled["goals.session_length"] = {"type": "chip", "values": ["1시간"]}
    filled["goals.frequency"] = {"type": "chip", "values": ["주 3회"]}

    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = filled
    assert interview._next_required_slot(state) is None  # FSM 완료

    outcome = interview_adapter.build_outcome(
        session_id="iv_skip_agree",
        slot_answers=filled,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="rule",
    )
    assert "goals.weekly_time" not in outcome.unresolved_slots
    assert next(g for g in outcome.core_goals if g.is_heaviest).weekly_hours == 3  # 1h × 3


def _goal(title: str, *, heaviest: bool = False) -> GoalCandidate:
    return GoalCandidate(
        title=title, category="study", is_heaviest=heaviest, tentative_tier="focus", confidence=0.9
    )


def _outcome_with_goals(goals: list[GoalCandidate]) -> InterviewOutcome:
    from reaction_backend.schemas.common import now_kst

    return InterviewOutcome(
        session_id="multi",
        generated_at=now_kst(),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="방학"),
        core_goals=goals,
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["오전"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True, downscope_unit_min=10),
        horizon=None,
    )


def test_other_goals_deferred_notice_names_what_was_left_out() -> None:
    """목표가 여러 개면 **무엇에 집중했고 무엇이 빠졌는지** 말한다.

    실측(#187): 목표 3개를 넣으면 heaviest 것만 16세션 들어가고 나머지 둘은 세션 0·블록 0인데
    경고가 하나도 없었다. 승인하면 목표는 3개 다 저장되므로 사용자 눈엔 "3개 등록했는데
    계획엔 하나뿐" 이고 이유를 알 수 없다. 하나씩 굴리는 건 의도지만 침묵은 아니다.
    """
    notice = first_plan_adapter.other_goals_deferred_notice(
        _outcome_with_goals(
            [_goal("알고리즘 문제 풀기", heaviest=True), _goal("토익 900점"), _goal("운동 습관")]
        )
    )
    assert notice is not None
    assert "알고리즘 문제 풀기" in notice  # 무엇에 집중했는지
    assert "토익 900점" in notice and "운동 습관" in notice  # 무엇이 빠졌는지
    assert "다음 계획" in notice  # 버려진 게 아니라 다음 차례라는 것


def test_other_goals_deferred_notice_silent_for_single_goal() -> None:
    """목표가 하나면 할 말이 없다 — 잡음 방지."""
    assert (
        first_plan_adapter.other_goals_deferred_notice(
            _outcome_with_goals([_goal("알고리즘 문제 풀기", heaviest=True)])
        )
        is None
    )


def test_other_goals_deferred_notice_ignores_placeholder() -> None:
    """미입력 placeholder(#88)는 실제 목표가 아니므로 세지 않는다."""
    from reaction_backend.orchestrator.interview_adapter import PLACEHOLDER_GOAL_TITLE

    placeholder = GoalCandidate(
        title=PLACEHOLDER_GOAL_TITLE,
        category="other",
        is_heaviest=False,
        tentative_tier="maintain",
        confidence=0.0,
    )
    assert (
        first_plan_adapter.other_goals_deferred_notice(
            _outcome_with_goals([_goal("알고리즘", heaviest=True), placeholder])
        )
        is None
    )


def test_other_goals_deferred_notice_folds_long_lists() -> None:
    """목표가 많으면 앞 3개만 나열하고 나머지는 '외 N개' 로 접는다."""
    goals = [_goal("주력", heaviest=True)] + [_goal(f"목표{i}") for i in range(5)]
    notice = first_plan_adapter.other_goals_deferred_notice(_outcome_with_goals(goals))
    assert notice is not None
    assert "외 2개" in notice
    assert "목표4" not in notice  # 접힌 것은 이름이 안 나온다


def test_total_capacity_uses_full_horizon_not_the_four_week_cap() -> None:
    """마일스톤 크기 기준은 **마감까지 전체**다 — 4주 캡(`horizon_weeks`)이 아니다 (ADR-0007 §11).

    두 값을 헷갈리면 12주짜리 목표의 뼈대를 4주치 용량으로 재게 되어, 담을 수 있는 것보다
    훨씬 작게 끊긴다. `_horizon_weeks` 는 '이번 구간', `full_horizon_weeks` 는 '마감까지'.
    """
    start = date(2026, 8, 23)
    outcome = _outcome_with(
        "iv_capacity",
        **{
            "goals.deadlines": {"type": "text", "raw": "2026-11-15"},  # 12주
            "goals.weekly_time": {"type": "chip", "values": ["3시간"]},
            "goals.frequency": {"type": "chip", "values": ["몰아서 · 상관없음"]},
        },
    )
    v = first_plan_adapter.context_from_outcome(outcome, target_date=start)["prompt_vars"]
    assert first_plan_adapter.full_horizon_weeks(start, "2026-11-15") == 12
    assert "36시간" in v["total_capacity"]  # 3 × 12 — 4주 캡이면 12시간이 됐을 것
    assert "12주" in v["total_capacity"]
    # 같은 outcome 의 '이번 구간' 은 여전히 4주로 잘려 있다(두 값이 서로 다른 질문에 답한다).
    assert v["horizon_weeks"] == "4"


def test_total_capacity_says_rhythm_when_there_is_no_deadline() -> None:
    """마감이 없으면 총량 개념이 없다 — 리듬형이라는 사실을 그대로 알린다 (ADR-0007 §12).

    프롬프트가 이 문구를 보고 '억지로 단계를 지어내지 않는' 분기를 탄다. 숫자를 지어내
    넘기면(예: 4주 캡으로 12시간) 끝이 없는 목표에 가짜 완료 시점이 박힌다.
    """
    outcome = _outcome_with("iv_capacity_none", **{"goals.deadlines": {"type": "text", "raw": ""}})
    v = first_plan_adapter.context_from_outcome(outcome, target_date=date(2026, 8, 23))[
        "prompt_vars"
    ]
    assert "마감이 없어" in v["total_capacity"]
    assert first_plan_adapter.full_horizon_weeks(date(2026, 8, 23), None) is None


def test_identity_line_skips_unanswered_slots() -> None:
    """미응답(`미상`)은 사람이 읽는 줄에 싣지 않는다 — 없는 사실을 만들지 않는다."""
    filled = _outcome_with("iv_id_filled")
    assert first_plan_adapter.context_from_outcome(filled)["prompt_vars"]["identity"] == (
        "대3 · 학기중 · 컴퓨터공학"
    )
    bare = interview_adapter.build_outcome(
        session_id="iv_id_bare",
        slot_answers={},
        ambiguity_final=0.1,
        end_reason="early_user",
        analysis_source="rule",
    )
    assert first_plan_adapter.context_from_outcome(bare)["prompt_vars"]["identity"] == "(미입력)"
