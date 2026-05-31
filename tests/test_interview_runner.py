"""Interview turn driver (#6) — Rule-based Slot FSM end-to-end.

ADR-0005 §7.3 패턴: aiClient.run 만 stub, 노드/러너는 일반 async 함수라 직접 호출.
- 필수 슬롯 수집 → Analysis Confirm(summary) → InterviewOutcome 까지 끊김 없이 연결
- clarity 미달 시 같은 슬롯 재질문 (저장 안 함)
- [충분해요] 조기 종료 → 빈 슬롯 default + unresolved_slots 기록
- LLM fallback 이면 outcome.analysis_source='rule'
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.llm import RunResult, aiClient
from reaction_backend.orchestrator import interview, interview_runner
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    InterviewOutcome,
    InterviewSummary,
    NextQuestionSchema,
)

# 슬롯 타입별 대표 답 (slot answerType 대로 클라이언트가 보낼 법한 raw 값)
_RANGE_SLOTS = {"time.activity_window"}
_CHIP_SLOTS = {
    "identity.role",
    "identity.season",
    "time.peak_window",
    "time.no_touch",
    "recovery.tone",
    "recovery.rest_ok",
    "recovery.downscope_unit",
}


def _answer_for(slot_key: str) -> Any:
    if slot_key in _RANGE_SLOTS:
        return {"start": "09:00", "end": "23:00"}
    if slot_key in _CHIP_SLOTS:
        return ["오전"] if slot_key == "time.peak_window" else ["네"]
    if slot_key == "goals.list":
        return "캡스톤, 토익"
    if slot_key == "goals.heaviest":
        return "캡스톤"
    if slot_key == "goals.deadlines":
        return "2026-06-20"
    return "테스트 답변"


def _stub(*, clarity: float = 0.9, new_ambiguity: float = 0.9, fell_back: bool = False):
    """aiClient.run stub — clarity 만 조절해 저장 여부를 제어. ambiguity 는 높게 두고
    종료를 FSM(필수 슬롯 완료)이 운전하게 한다."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(
                question="다음 질문",
                clarity_score=clarity,
                normalized_value=None,
                empathy_one_liner="좋아요",
            )
        elif schema is AmbiguityUpdate:
            value = AmbiguityUpdate(
                slot_key=kwargs["variables"]["slot_key"],
                clarity_score=clarity,
                new_ambiguity=new_ambiguity,
            )
        elif schema is InterviewSummary:
            value = InterviewSummary(
                headline="요약",
                goal_summary="목표 요약",
                time_summary="시간 요약",
                preference_summary="선호 요약",
                confirm_question="이대로 계획을 세워볼까요?",
            )
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=fell_back,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    return stub_run


async def test_fsm_collects_required_slots_then_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    """필수 슬롯을 순서대로 채우면 Analysis Confirm + InterviewOutcome 으로 마감."""
    monkeypatch.setattr(aiClient, "run", _stub(clarity=0.9))

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    assert result.question is not None
    assert result.state["next_slot_key"] == "identity.role"  # FSM 첫 필수 슬롯

    guard = 0
    while not result.done and guard < 30:
        slot = result.state["next_slot_key"]
        assert slot is not None
        result = await interview_runner.submit_and_advance(
            state=result.state, slot_key=slot, answer_value=_answer_for(slot)
        )
        guard += 1

    assert result.done is True
    assert result.end_reason == "completed"
    assert isinstance(result.summary, InterviewSummary)
    assert isinstance(result.outcome, InterviewOutcome)
    assert result.outcome.unresolved_slots == []  # 필수 슬롯 모두 충족
    assert result.outcome.analysis_source == "llm"
    # 핵심 목표/가용 시간/선호 방식이 실제로 채워졌는지
    assert {g.title for g in result.outcome.core_goals} == {"캡스톤", "토익"}
    assert result.outcome.availability.activity_window.start == "09:00"
    assert result.outcome.preferences.recovery_tone == "네"


async def test_low_clarity_reasks_same_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """clarity 가 임계 미만이면 답을 저장하지 않고 같은 슬롯을 다시 묻는다."""
    monkeypatch.setattr(aiClient, "run", _stub(clarity=0.1))

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    first_slot = result.state["next_slot_key"]

    result = await interview_runner.submit_and_advance(
        state=result.state, slot_key=first_slot, answer_value="음..."
    )
    assert result.done is False
    assert result.state["next_slot_key"] == first_slot  # 저장 안 됨 → 같은 슬롯 재질문
    assert first_slot not in result.state["slot_answers"]


async def test_finish_early_defaults_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """[충분해요] — 빈 필수 슬롯은 default + unresolved_slots, end_reason=early_user."""
    monkeypatch.setattr(aiClient, "run", _stub())

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    result = await interview_runner.finish_early(state=result.state)

    assert result.done is True
    assert result.end_reason == "early_user"
    assert result.outcome is not None
    assert "goals.list" in result.outcome.unresolved_slots
    assert len(result.outcome.core_goals) >= 1  # min_length 계약 유지


async def test_fallback_marks_rule_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """노드가 룰 fallback 되면 outcome.analysis_source='rule' 로 표기."""
    monkeypatch.setattr(aiClient, "run", _stub(fell_back=True))

    result = await interview_runner.start_interview(session_id=uuid4(), user_id=uuid4())
    result = await interview_runner.finish_early(state=result.state)

    assert result.state["used_fallback"] is True
    assert result.outcome is not None
    assert result.outcome.analysis_source == "rule"


def test_required_sequence_covers_three_pillars() -> None:
    """FSM 시퀀스가 핵심 목표·가용 시간·선호 방식 세 기둥을 모두 포함하는지."""
    seq = interview.REQUIRED_SLOT_SEQUENCE
    assert any(k.startswith("goals.") for k in seq)
    assert any(k.startswith("time.") for k in seq)
    assert any(k.startswith("recovery.") for k in seq)
