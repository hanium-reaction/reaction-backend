"""Deep Interview Orchestrator (#6) — Rule-based Slot FSM + LLM Nodes.

흐름 (베이스라인 §6 "필수 슬롯 채우기 → 모호함 0 까지 cycle"):

    ask_question → receive_answer → validate_answer ─┐
         ▲                                           │ should_continue
         └──────────────── continue ─────────────────┤
                            finish → summarize_interview → finalize_outcome → END

설계 원칙 (요청 규칙 엄수):
- **Rule-based Slot FSM**: 다음에 물을 슬롯 선택·종료 판단은 LLM 0회의 순수 규칙
  (`_next_required_slot` / `_terminal_reason`). 룰이 흐름을 운전하고 LLM 은 문장 생성·
  채점에만 쓴다 — 8s timeout/rate limit 이 와도 인터뷰가 끊기지 않는다.
- **모든 LLM 호출은 `aiClient.run(...)` 단일 게이트만** (AGENTS.md §2). Gemini SDK 직접
  import 금지. 각 노드는 timeout=8.0 + 같은 schema 로 환원하는 룰 `fallback=` 을 넘긴다.
- **Envelope-less**: 터미널은 껍데기 없이 도메인 객체 `InterviewOutcome` 를 빌드(LLM 0회).
  요약 확인 카드(`InterviewSummary`)는 표현 계층으로 state 에만 싣는다.
- State 는 직렬화 가능해야 한다 → `AsyncSession` 은 넣지 않고
  `config["configurable"]["session"]` 채널로 전달 (ADR-0005 §7.1).

종료 조건:
  필수 슬롯 전부 충족 / early_finish.

  `ambiguity_score` float 는 LLM 채점 보조값일 뿐 API 의 `ambiguityScore`
  (남은 필수 슬롯 수) 완료 조건을 대체하지 않는다.

라우터는 보통 그래프를 한 번에 `ainvoke` 하지 않고 `interview_runner` 로 턴 단위 구동한다
(사용자 답이 HTTP 요청으로 외부에서 들어오기 때문 — `receive_answer` 가 no-op 인 이유).
"""

from __future__ import annotations

import re
from typing import Any, Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import interview_adapter
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    InterviewEndReason,
    InterviewOutcome,
    InterviewSummary,
    NextQuestionSchema,
)

__all__ = [
    "InterviewState",
    "ask_question",
    "build_interview_graph",
    "finalize_outcome",
    "initial_state",
    "receive_answer",
    "should_continue",
    "summarize_interview",
    "validate_answer",
]

STORE_CLARITY_MIN = 0.4  # clarity 가 이 미만이면 답을 채우지 않고 같은 슬롯 재질문

# Rule-based FSM 이 순서대로 채워가는 필수 슬롯 (interview_adapter 와 동일 진실 소스).
# 핵심 목표(goals.*) / 가용 시간(time.*) / 선호 방식(recovery.*) 그룹을 모두 포함.
REQUIRED_SLOT_SEQUENCE: tuple[str, ...] = interview_adapter.REQUIRED_SLOT_KEYS


class InterviewState(TypedDict):
    """LangGraph 가 Node 간 전달하는 상태. DB(`interview_sessions`)와 별도 short-lived.

    직렬화 가능해야 하므로 비직렬화 객체(AsyncSession 등)는 넣지 않는다(ADR-0005 §7.1).
    """

    # 식별/진행
    session_id: UUID
    user_id: UUID
    ambiguity_score: float  # 0..1, 낮을수록 명확 (DB ambiguity_final 과 동일 척도)
    total_turns: int
    stall_count: int
    early_finish: bool  # [충분해요] 탭
    end_reason: InterviewEndReason | None

    # 턴 단위
    next_slot_key: str | None  # FSM 이 이번 턴에 물은 필수 슬롯
    last_slot_key: str | None  # 직전 답이 속한 슬롯 (라우터가 주입)
    last_answer: dict[str, Any] | None  # interview_slot_answers.value 형태
    next_question: NextQuestionSchema | None
    used_fallback: bool  # 어느 턴이든 룰 정규화면 True → outcome.analysis_source

    # 누적 슬롯 (DB slot_answers 의 in-memory 미러) {slot_key: value}
    slot_answers: dict[str, dict[str, Any] | None]

    # 터미널 산출물
    summary: InterviewSummary | None  # 요약 확인 카드 (표현 계층)
    outcome: InterviewOutcome | None  # 경계 계약 (First Plan 시드)


def initial_state(*, session_id: UUID, user_id: UUID) -> InterviewState:
    """라우터/테스트에서 그래프 진입 시 쓰는 초기 상태."""
    return InterviewState(
        session_id=session_id,
        user_id=user_id,
        ambiguity_score=1.0,
        total_turns=0,
        stall_count=0,
        early_finish=False,
        end_reason=None,
        next_slot_key=None,
        last_slot_key=None,
        last_answer=None,
        next_question=None,
        used_fallback=False,
        slot_answers={},
        summary=None,
        outcome=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based FSM helpers — 순수 함수 (LLM 호출 X). 흐름은 룰이 운전한다.
# ─────────────────────────────────────────────────────────────────────────────


def _is_filled(value: dict[str, Any] | None) -> bool:
    """슬롯 값이 실질적으로 채워졌는지 (빈 dict/None 제외)."""
    return bool(value)


def _next_required_slot(state: InterviewState) -> str | None:
    """아직 안 채운 첫 필수 슬롯 키. 모두 채웠으면 None (FSM 완료 신호)."""
    answers = state["slot_answers"]
    return next((k for k in REQUIRED_SLOT_SEQUENCE if not _is_filled(answers.get(k))), None)


def _all_required_filled(state: InterviewState) -> bool:
    return _next_required_slot(state) is None


# ─────────────────────────────────────────────────────────────────────────────
# 룰 fallback (8s timeout / rate limit / schema 실패 시) — 같은 schema 로 환원.
# ─────────────────────────────────────────────────────────────────────────────


def _rule_next_question(state: InterviewState, slot_key: str) -> NextQuestionSchema:
    """카탈로그 기본 질문으로 회귀 — LLM 죽어도 인터뷰가 끊기지 않는다."""
    return NextQuestionSchema(
        question=_DEFAULT_SLOT_QUESTIONS.get(
            slot_key, "조금만 더 구체적으로 알려주실 수 있을까요?"
        ),
        clarity_score=0.5,
        normalized_value=None,
        empathy_one_liner="천천히 알려주셔도 괜찮아요.",
    )


def _rule_ambiguity_update(state: InterviewState, slot_key: str) -> AmbiguityUpdate:
    """답이 있으면 모호함을 소폭 감소시키는 단순 휴리스틱."""
    answered = _has_answer_text(state)
    new_score = max(0.0, state["ambiguity_score"] - (0.15 if answered else 0.0))
    return AmbiguityUpdate(
        slot_key=slot_key,
        clarity_score=0.5 if answered else 0.0,
        new_ambiguity=new_score,
    )


def _rule_summary(state: InterviewState) -> InterviewSummary:
    """슬롯에서 결정적으로 빌드한 룰 요약 — LLM 실패 시 그대로 노출."""
    v = _summary_variables(state)
    goals = v["goals"] or "아직 정하지 않음"
    return InterviewSummary(
        headline=f"{v['identity']} · 핵심 목표 {goals}",
        goal_summary=f"가장 무겁게 느끼는 일은 '{v['heaviest']}' 이고, 정리한 목표는 {goals} 예요.",
        time_summary=f"활동 시간대는 {v['time_window']}, 집중은 {v['peak_window']} 가 좋다고 하셨어요.",
        preference_summary=f"못 한 날엔 '{v['tone']}' 톤을 선호하세요.",
        confirm_question="이대로 계획을 세워볼까요?",
    )


def _session(config: RunnableConfig) -> Any:
    """config["configurable"]["session"] 안전 추출 (없으면 None → 예산/로깅 skip)."""
    return config.get("configurable", {}).get("session")


def _tone_mode(config: RunnableConfig) -> str | None:
    """config["configurable"]["tone_mode"] 안전 추출 (#23-D). 없으면 None = 톤 prefix 없음."""
    raw = config.get("configurable", {}).get("tone_mode")
    return raw if isinstance(raw, str) else None


# ─────────────────────────────────────────────────────────────────────────────
# Nodes — async def node(state, config). config 두 번째 인자 (ADR-0005 §7.1).
# ─────────────────────────────────────────────────────────────────────────────


async def ask_question(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ① — FSM 이 고른 다음 필수 슬롯에 대한 질문 1개 생성.

    슬롯 선택은 룰(`_next_required_slot`), 문장만 LLM. timeout 시 카탈로그 기본 질문.
    """
    slot_key = _next_required_slot(state) or ""
    result = await aiClient.run(
        module="interview",
        schema=NextQuestionSchema,
        prompt_id="interview/next_question",
        fallback=lambda: _rule_next_question(state, slot_key),
        timeout=8.0,
        variables={
            "goal_title": _heaviest_goal_hint(state),
            "turn_index": str(state["total_turns"]),
            "ambiguous_slot": slot_key,
            "last_answer": _last_answer_text(state),
        },
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    return {
        **state,
        "next_question": result.value,
        "next_slot_key": slot_key,
        "total_turns": state["total_turns"] + 1,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def receive_answer(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """사용자 답 수신 노드 — 외부 트리거(POST .../answers)로 진입.

    실제 답 주입·DB UPSERT 는 라우터(`interview_runner.submit_and_advance`)가 한다.
    그래프 자체를 batch `ainvoke` 할 때는 답이 없으므로 no-op (state passthrough).
    """
    return state


async def validate_answer(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ② — 직전 답을 채점·정규화하고, 충분하면 슬롯에 채운 뒤 모호함 갱신.

    - clarity ≥ STORE_CLARITY_MIN → 정규화해 `slot_answers[slot]` 에 저장(슬롯 충족).
    - clarity 미달 → 저장 안 함 → FSM 이 같은 슬롯을 한 번 더 묻는다 (재질문).
    - 새 슬롯이 안 채워진 턴이면 stall_count 증가(진척 없음), 채워지면 리셋 (정체 감지).
    """
    slot_key = state.get("last_slot_key") or state.get("next_slot_key") or ""

    result = await aiClient.run(
        module="interview",
        schema=AmbiguityUpdate,
        prompt_id="interview/ambiguity_score",
        fallback=lambda: _rule_ambiguity_update(state, slot_key),
        timeout=8.0,
        variables={"slot_key": slot_key, "answer": _last_answer_text(state)},
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    update = result.value

    slot_answers = dict(state["slot_answers"])
    last_answer = state["last_answer"]
    filled_now = (
        bool(slot_key) and last_answer is not None and update.clarity_score >= STORE_CLARITY_MIN
    )
    if filled_now and last_answer is not None:  # 2번째 조건은 mypy 내로잉용 (filled_now 에 포함)
        slot_answers[slot_key] = _normalize_for_store(slot_key, last_answer)

    new_score = update.new_ambiguity
    stall = 0 if filled_now else state["stall_count"] + 1
    return {
        **state,
        "slot_answers": slot_answers,
        "ambiguity_score": new_score,
        "stall_count": stall,
        "last_answer": None,  # 소비 완료 — 다음 턴 답과 섞이지 않게
        "last_slot_key": None,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def summarize_interview(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ③ — 모은 슬롯을 요약 확인 카드로. timeout 시 슬롯에서 룰 요약."""
    v = _summary_variables(state)
    result = await aiClient.run(
        module="interview",
        schema=InterviewSummary,
        prompt_id="interview/summary",
        fallback=lambda: _rule_summary(state),
        timeout=8.0,
        variables=v,
        user_id=state["user_id"],
        session=_session(config),
        tone_mode=_tone_mode(config),
    )
    return {
        **state,
        "summary": result.value,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def finalize_outcome(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """터미널 — LLM 0회로 경계 계약(InterviewOutcome) 빌드. First Plan 시드."""
    reason: InterviewEndReason = _terminal_reason(state) or "completed"
    outcome = interview_adapter.build_outcome(
        session_id=str(state["session_id"]),
        slot_answers=state["slot_answers"],
        ambiguity_final=state["ambiguity_score"],
        end_reason=reason,
        analysis_source="rule" if state["used_fallback"] else "llm",
    )
    return {**state, "outcome": outcome, "end_reason": reason}


# ─────────────────────────────────────────────────────────────────────────────
# Conditional edge — 순수 함수 (LLM 호출 X, ADR-0005 §2.4 패턴).
# ─────────────────────────────────────────────────────────────────────────────


def _terminal_reason(state: InterviewState) -> InterviewEndReason | None:
    """종료 조건 평가. 종료면 DB enum 사유, 아니면 None.

    일반 진행은 필수 슬롯 완료(FSM)일 때만 `completed` 로 마감한다.
    LLM 의 float `ambiguity_score` 가 낮아도 미해결 필수 슬롯이 남아 있으면 계속 묻는다.
    """
    if state["early_finish"]:
        return "early_user"
    if _all_required_filled(state):
        return "completed"
    return None


def should_continue(state: InterviewState) -> Literal["continue", "finish"]:
    """Cycle 종료 조건. 종료면 summarize_interview, 아니면 ask_question 재진입."""
    return "finish" if _terminal_reason(state) is not None else "continue"


# ─────────────────────────────────────────────────────────────────────────────
# 답 정규화 / 프롬프트 변수 보조
# ─────────────────────────────────────────────────────────────────────────────

_TEXT_SPLIT_RE = re.compile(r"[,、，\n]")


def _normalize_for_store(slot_key: str, answer: dict[str, Any]) -> dict[str, Any]:
    """저장 직전 룰 정규화 — text 답은 항목 리스트(`normalized`)를 채워 어댑터가 쓰기 쉽게.

    chip/range 는 그대로 둔다. 이미 normalized 가 있으면 보존.
    """
    if answer.get("type") == "text" and "normalized" not in answer:
        raw = str(answer.get("raw", ""))
        parts = [p.strip() for p in _TEXT_SPLIT_RE.split(raw) if p.strip()]
        if parts:
            return {**answer, "normalized": parts}
    return answer


def _has_answer_text(state: InterviewState) -> bool:
    return bool(_last_answer_text(state))


def _last_answer_text(state: InterviewState) -> str:
    answer = state["last_answer"]
    if not answer:
        return ""
    if answer.get("type") == "text":
        return str(answer.get("raw", ""))
    if answer.get("type") == "chip":
        values = answer.get("values") or []
        return ", ".join(str(v) for v in values)
    if answer.get("type") == "range":
        return f"{answer.get('start', '')}~{answer.get('end', '')}"
    return ""


def _heaviest_goal_hint(state: InterviewState) -> str:
    heaviest = state["slot_answers"].get("goals.heaviest")
    text = _slot_text(heaviest) or _slot_first_chip(heaviest)
    if text:
        return text
    goals = state["slot_answers"].get("goals.list")
    items = _slot_items(goals)
    return items[0] if items else "당신의 목표"


def _summary_variables(state: InterviewState) -> dict[str, str]:
    """요약 프롬프트 변수 — 슬롯에서 사람이 읽을 문자열로 추출 (룰)."""
    answers = state["slot_answers"]
    role = _slot_first_chip(answers.get("identity.role")) or "미상"
    season = _slot_first_chip(answers.get("identity.season")) or ""
    goals = ", ".join(_slot_items(answers.get("goals.list"))) or "아직 정하지 않음"
    heaviest = (
        _slot_text(answers.get("goals.heaviest"))
        or _slot_first_chip(answers.get("goals.heaviest"))
        or "아직 정하지 않음"
    )
    window = answers.get("time.activity_window")
    time_window = (
        f"{window.get('start')}~{window.get('end')}"
        if window and window.get("type") == "range"
        else "아직 정하지 않음"
    )
    peak = ", ".join(_slot_chips(answers.get("time.peak_window"))) or "아직 정하지 않음"
    tone = _slot_first_chip(answers.get("recovery.tone")) or "담백"
    identity = f"{role} {season}".strip()
    return {
        "identity": identity,
        "goals": goals,
        "heaviest": heaviest,
        "time_window": time_window,
        "peak_window": peak,
        "tone": tone,
    }


def _slot_chips(value: dict[str, Any] | None) -> list[str]:
    if not value or value.get("type") != "chip":
        return []
    raw = value.get("values") or []
    return [str(v) for v in raw] if isinstance(raw, list) else []


def _slot_first_chip(value: dict[str, Any] | None) -> str | None:
    chips = _slot_chips(value)
    return chips[0] if chips else None


def _slot_text(value: dict[str, Any] | None) -> str | None:
    if not value or value.get("type") != "text":
        return None
    raw = value.get("raw")
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def _slot_items(value: dict[str, Any] | None) -> list[str]:
    if not value or value.get("type") != "text":
        return []
    norm = value.get("normalized")
    if isinstance(norm, list):
        return [str(v) for v in norm if str(v).strip()]
    raw = value.get("raw")
    return [str(raw)] if isinstance(raw, str) and raw.strip() else []


# 카탈로그 기본 질문 (LLM 죽었을 때 회귀) — mock.interview.SLOT_CATALOG 라벨 기반.
_DEFAULT_SLOT_QUESTIONS: dict[str, str] = {
    "identity.role": "어떤 학년/시기예요?",
    "identity.season": "지금 학기 중이에요, 방학이에요?",
    "goals.list": "지금 머릿속에 있는 일들을 편하게 알려주세요.",
    "goals.heaviest": "그중 가장 무겁게 느끼는 건 어떤 거예요?",
    "goals.deadlines": "마감일이 정해진 게 있어요?",
    "goals.success_image": "이번 주 끝에 어떤 모습이면 좋을까요?",
    "time.activity_window": "보통 몇 시부터 몇 시까지 활동해요?",
    "time.fixed_blocks": "매주 고정으로 비워야 하는 시간 있어요?",
    "time.peak_window": "가장 잘 집중되는 시간대는요?",
    "time.no_touch": "절대 일정 잡으면 안 되는 시간은요?",
    "recovery.tone": "못 한 날 어떤 톤이 좋아요?",
    "recovery.rest_ok": "쉬는 게 어때요 하는 제안을 받을 의향 있어요?",
    "recovery.downscope_unit": "5분짜리로 줄어든 일도 의미 있게 느껴지나요?",
}


def build_interview_graph() -> CompiledStateGraph[
    InterviewState, Any, InterviewState, InterviewState
]:
    """Cyclic StateGraph 컴파일. 라우터는 보통 `interview_runner` 로 턴 단위 구동하고,
    batch 시뮬레이션/테스트는 `await graph.ainvoke(initial, config=...)`."""
    graph = StateGraph(InterviewState)
    graph.add_node("ask_question", ask_question)
    graph.add_node("receive_answer", receive_answer)
    graph.add_node("validate_answer", validate_answer)
    graph.add_node("summarize_interview", summarize_interview)
    graph.add_node("finalize_outcome", finalize_outcome)

    graph.set_entry_point("ask_question")
    graph.add_edge("ask_question", "receive_answer")
    graph.add_edge("receive_answer", "validate_answer")
    graph.add_conditional_edges(
        "validate_answer",
        should_continue,
        {"continue": "ask_question", "finish": "summarize_interview"},
    )
    graph.add_edge("summarize_interview", "finalize_outcome")
    graph.add_edge("finalize_outcome", END)
    return graph.compile()
