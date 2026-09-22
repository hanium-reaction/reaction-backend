"""인터뷰가 **사용자가 말한 적 없는 값**으로 계획을 세우지 않는다 (r2 실측 회귀).

배포 미러에서 한 세션을 끝까지 돌려 확인된 것을 못 박는다.

1. 활동 시간대(`time.activity_window`)를 **한 번도 묻지 않았는데** 답이 있었다 —
   "새벽에만 집중이 돼서 밤에 작업해요" 에서 00:00~06:00 이 수확돼 프로필까지 저장됐고,
   계획은 22:00 에 일정을 잡아 놓고 "활동 가능 시간(00:00~06:00)과 겹치지 않아서" 라고
   스스로를 반박했다.
2. 같은 사용자에게 주당 시간이 두 개였다 — 확인 카드는 "주 3.5시간", 목표 기록은 2시간.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.llm import RunResult, aiClient
from reaction_backend.orchestrator import (
    interview,
    interview_adapter,
    interview_runner,
)
from reaction_backend.orchestrator.interview import InterviewState
from reaction_backend.orchestrator.interview_catalog import PLAN_CATALOG
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    AnswerIntake,
    HarvestedSlot,
    InterviewSummary,
    NextQuestionSchema,
)

_SLOT_META: dict[str, dict[str, Any]] = {
    s.slot_key: {"label": s.label, "answer_type": s.answer_type, "options": list(s.options)}
    for s in PLAN_CATALOG.slots
}


def _filled(slot_key: str) -> dict[str, Any]:
    """그 슬롯 형식에 맞는 '이미 답했다' 값 — 테스트 대상 슬롯만 비워 두려고 쓴다."""
    slot = PLAN_CATALOG.by_key[slot_key]
    if slot.answer_type == "time_range":
        return {"type": "range", "start": "09:00", "end": "23:00"}
    if slot.answer_type == "date_picker":
        return {"type": "text", "raw": "2026-12-31"}
    if slot.options:
        return {"type": "chip", "values": [slot.options[0]]}
    if slot_key == "goals.list":
        return {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]}
    if slot_key == "goals.heaviest":
        return {"type": "chip", "values": ["캡스톤"]}
    return {"type": "text", "raw": "답변"}


def _state_missing(*open_keys: str) -> InterviewState:
    """`open_keys` 만 비어 있는 인터뷰 상태 — FSM 이 곧바로 그 슬롯을 묻는다."""
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = {
        key: _filled(key) for key in PLAN_CATALOG.required_keys if key not in open_keys
    }
    return state


def _stub(
    *,
    normalized: Any = None,
    clarity: float = 0.9,
    fell_back: bool = False,
    harvested: list[HarvestedSlot] | None = None,
):
    """aiClient.run stub — 채점 LLM 이 낸 정규화 값/수확 결과를 테스트가 정한다."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(question="다음 질문", empathy_one_liner="좋아요")
        elif schema in (AmbiguityUpdate, AnswerIntake):
            value = schema(
                slot_key=kwargs["variables"]["slot_key"],
                clarity_score=clarity,
                new_ambiguity=0.1,
                normalized_value=normalized,
                **({"slots": harvested} if schema is AnswerIntake and harvested else {}),
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


async def _answer(state: InterviewState, slot_key: str, raw: Any) -> interview_runner.TurnResult:
    """라우터와 **같은 방식**으로 답 1개를 넣는다 — 슬롯 메타(형식·보기)까지 실어서."""
    slot = PLAN_CATALOG.by_key[slot_key]
    return await interview_runner.submit_and_advance(
        state=state,
        slot_key=slot_key,
        answer_value=raw,
        answer_type=slot.answer_type,
        options=list(slot.options),
        slot_meta=_SLOT_META,
    )


# ─────────────────────────────────────────────────────────────────────────────
# P1 — 활동 시간대는 묻지 않고 채우지 않는다
# ─────────────────────────────────────────────────────────────────────────────


async def test_activity_window_is_not_harvested_from_a_free_text_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시간대를 언급한 자유서술이 활동 시간대를 채우지 못한다 — 그리고 슬롯은 계속 열려 있다.

    회귀(실측): "새벽에만 집중이 돼서 밤에 작업해요" 한 마디로 00:00~06:00 이 수확돼,
    한 번도 묻지 않은 활동창이 그 사용자의 **유일한 배치 가능 시간**이 됐다. 질문 자체가
    "이 시간 밖엔 일정을 안 잡아요" 라고 약속하는 경계라, 확인받지 않은 값이 정의하면 안 된다.
    """
    monkeypatch.setattr(
        aiClient,
        "run",
        _stub(
            normalized="처음이에요",
            harvested=[
                HarvestedSlot(
                    slot_key="time.activity_window",
                    normalized_value={"start": "00:00", "end": "06:00"},
                    confidence=0.95,
                )
            ],
        ),
    )
    state = _state_missing("goals.current_level", "time.activity_window")

    result = await _answer(
        state,
        "goals.current_level",
        "새벽에만 집중이 돼서 밤에 작업해요. 아직 시작은 못 했어요.",
    )

    assert "time.activity_window" not in result.harvested
    assert result.state["slot_answers"].get("time.activity_window") is None
    # 슬롯이 열린 채 남아 **정식으로 묻는다** — 다음에 물을 슬롯이 바로 그것이다.
    assert result.done is False
    assert result.state["next_slot_key"] == "time.activity_window"


def test_harvest_excludes_whole_plan_boundaries_but_keeps_goal_content() -> None:
    """수확 제외 목록의 기준을 못 박는다 — 계획 전체를 가두는 값은 물어서만 받는다."""
    assert "time.activity_window" in PLAN_CATALOG.harvest_exclude
    assert "goals.weekly_time" in PLAN_CATALOG.harvest_exclude
    # 목표 내용은 그대로 수확 대상 — 하베스팅을 끈 게 아니라 좁힌 것이다.
    for key in ("goals.deadlines", "goals.success_image", "identity.role"):
        assert key not in PLAN_CATALOG.harvest_exclude


# ─────────────────────────────────────────────────────────────────────────────
# P4 — 확인 카드의 주당 시간과 계획의 주당 시간이 같다
# ─────────────────────────────────────────────────────────────────────────────


def _card_and_record(answers: dict[str, Any]) -> tuple[str, int | None]:
    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    state["slot_answers"] = answers
    outcome = interview_adapter.build_outcome(
        session_id=str(state["session_id"]),
        slot_answers=answers,
        ambiguity_final=0.1,
        end_reason="completed",
        analysis_source="llm",
    )
    return interview._summary_variables(state)["weekly_load"], outcome.core_goals[0].weekly_hours


_GOAL_BASE: dict[str, Any] = {
    "goals.list": {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]},
    "goals.heaviest": {"type": "chip", "values": ["캡스톤"]},
    "goals.session_length": {"type": "chip", "values": ["30분"]},
    "goals.frequency": {"type": "chip", "values": ["매일"]},
}


def test_confirm_card_quotes_the_weekly_hours_the_plan_will_use() -> None:
    """주당 시간을 직접 답했으면 카드도 그 값을 말한다.

    회귀(실측): 카드는 "약 주 3.5시간 (한 번 30분 × 매일)" 이라 확인받고, 목표 기록은
    2시간이었다. 사용자가 [이대로 진행] 을 누른 숫자와 계획이 쓰는 숫자가 다르면 그
    확인 카드는 확인이 아니다.
    """
    card, record = _card_and_record(
        {**_GOAL_BASE, "goals.weekly_time": {"type": "chip", "values": ["2시간"]}}
    )
    assert record == 2
    assert card == "2시간"
    assert "3.5" not in card


def test_confirm_card_shows_the_multiplication_when_weekly_hours_were_not_asked() -> None:
    """주당 시간을 묻지 않은 새 흐름에서는 곱셈 결과를 그대로 되돌려준다 — 같은 값의 두 표기."""
    card, record = _card_and_record(dict(_GOAL_BASE))
    assert card == "약 주 3.5시간 (한 번 30분 × 매일)"
    assert record == interview_adapter.weekly_hours_for_plan(_GOAL_BASE) == 4  # 3.5 의 정수 반올림


def test_weekly_hours_has_one_source_of_truth() -> None:
    """목표 기록은 `weekly_hours_for_plan` 하나만 읽는다 — 두 번째 진실을 만들지 않는다."""
    for answers in (
        dict(_GOAL_BASE),
        {**_GOAL_BASE, "goals.weekly_time": {"type": "chip", "values": ["2시간"]}},
        {"goals.list": {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]}},
    ):
        outcome = interview_adapter.build_outcome(
            session_id="s",
            slot_answers=answers,
            ambiguity_final=0.1,
            end_reason="completed",
            analysis_source="llm",
        )
        assert outcome.core_goals[0].weekly_hours == interview_adapter.weekly_hours_for_plan(
            answers
        )
