"""Deep Interview Orchestrator (#6) — Cyclic StateGraph (ADR-0005 §2.3 canonical).

흐름 (베이스라인 §6 "슬롯 채우기 + 모호함 0 까지 cycle"):

    ask_next_slot → receive_answer → update_ambiguity ─┐
         ▲                                             │ should_continue
         └──────────────── continue ───────────────────┤
                                              finish ───┴→ finalize_outcome → END

- LLM 호출은 Node 안에서 `aiClient.run(...)` 만 (AGENTS.md §2, ADR-0005 §2 #3).
  Gemini SDK 직접 import 금지. 8s timeout / rate limit 시 룰 fallback.
- State 는 직렬화 가능해야 한다 → `AsyncSession` 은 넣지 않고
  `config["configurable"]["session"]` 채널로 전달 (ADR-0005 §7.1).
- 터미널에서 LLM 0회로 `InterviewOutcome`(경계 계약)을 빌드 → First Plan(#32) 시드.

종료 조건 4종 (ADR-0005 §2.5.2):
  ambiguity ≤ 0.2 / total_turns ≥ 15 / early_finish / 3턴 연속 정체.
"""

from __future__ import annotations

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
    NextQuestionSchema,
)

__all__ = [
    "InterviewState",
    "build_interview_graph",
    "initial_state",
    "should_continue",
]

# ── 종료 임계값 (ADR-0005 §2.5.2) ─────────────────────────────────────────────
AMBIGUITY_DONE_THRESHOLD = 0.2  # 모호함 ≤ 0.2 ("0 까지"는 보장 어려워 실용 임계값
MAX_TURNS = 15  # 베이스라인 §6 최대 15턴
STALL_LIMIT = 3  # 3턴 연속 모호함 감소 0 → 정체 종료


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
    last_answer: dict[str, Any] | None  # interview_slot_answers.value 형태
    next_question: NextQuestionSchema | None
    used_fallback: bool  # 어느 턴이든 룰 정규화면 True → outcome.analysis_source

    # 누적 슬롯 (DB slot_answers 의 in-memory 미러) {slot_key: value}
    slot_answers: dict[str, dict[str, Any] | None]

    # 터미널 산출물
    outcome: InterviewOutcome | None


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
        last_answer=None,
        next_question=None,
        used_fallback=False,
        slot_answers={},
        outcome=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 룰 fallback (8s timeout / rate limit / schema 실패 시) — 같은 schema 로 환원.
# ─────────────────────────────────────────────────────────────────────────────


def _rule_next_question(state: InterviewState) -> NextQuestionSchema:
    return NextQuestionSchema(
        question="조금만 더 구체적으로 알려주실 수 있을까요?",
        clarity_score=0.5,
        normalized_value=None,
        empathy_one_liner="천천히 알려주셔도 괜찮아요.",
    )


def _rule_ambiguity_update(state: InterviewState) -> AmbiguityUpdate:
    # 답이 있으면 모호함을 소폭 감소시키는 단순 휴리스틱.
    answered = state["last_answer"] is not None
    new_score = max(0.0, state["ambiguity_score"] - (0.15 if answered else 0.0))
    return AmbiguityUpdate(slot_key="", clarity_score=0.5, new_ambiguity=new_score)


def _session(config: RunnableConfig) -> Any:
    """config["configurable"]["session"] 안전 추출 (없으면 None → 예산/로깅 skip)."""
    return config.get("configurable", {}).get("session")


# ─────────────────────────────────────────────────────────────────────────────
# Nodes — async def node(state, config). config 두 번째 인자 (ADR-0005 §7.1).
# ─────────────────────────────────────────────────────────────────────────────


async def ask_next_slot(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ① — 모호함이 가장 큰 슬롯에서 다음 질문 1개 생성."""
    result = await aiClient.run(
        module="interview",
        schema=NextQuestionSchema,
        prompt_id="interview/next_question",
        fallback=lambda: _rule_next_question(state),
        timeout=8.0,
        variables={
            "goal_title": _heaviest_goal_hint(state),
            "turn_index": str(state["total_turns"]),
            "ambiguous_slot": "",
            "last_answer": _last_answer_text(state),
        },
        user_id=state["user_id"],
        session=_session(config),
    )
    return {
        **state,
        "next_question": result.value,
        "total_turns": state["total_turns"] + 1,
        "used_fallback": state["used_fallback"] or result.fell_back,
    }


async def receive_answer(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """사용자 답 수신 노드 — 외부 트리거(POST .../answers)로 진입. DB UPSERT 는 라우터."""
    return state


async def update_ambiguity(state: InterviewState, config: RunnableConfig) -> InterviewState:
    """LLM ② — 직전 답 정규화 + clarity 채점 → 모호함 지표 갱신."""
    prev = state["ambiguity_score"]
    result = await aiClient.run(
        module="interview",
        schema=AmbiguityUpdate,
        prompt_id="interview/ambiguity_score",
        fallback=lambda: _rule_ambiguity_update(state),
        timeout=8.0,
        variables={"answer": _last_answer_text(state)},
        user_id=state["user_id"],
        session=_session(config),
    )
    new_score = result.value.new_ambiguity
    # 정체 감지: 모호함이 줄지 않으면 stall_count 증가, 줄면 리셋.
    stall = state["stall_count"] + 1 if new_score >= prev else 0
    return {
        **state,
        "ambiguity_score": new_score,
        "stall_count": stall,
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
    """종료 조건 4종 평가. 종료면 DB enum 사유, 아니면 None.

    정체(stall)는 현재 슬롯으로 마감 가능하므로 `completed` 로 환원한다
    (interview_end_reason enum 에 stall 없음).
    """
    if state["early_finish"]:
        return "early_user"
    if state["ambiguity_score"] <= AMBIGUITY_DONE_THRESHOLD:
        return "completed"
    if state["total_turns"] >= MAX_TURNS:
        return "turn_limit"
    if state["stall_count"] >= STALL_LIMIT:
        return "completed"
    return None


def should_continue(state: InterviewState) -> Literal["continue", "finish"]:
    """Cycle 종료 조건. 종료면 finalize_outcome, 아니면 ask_next_slot 재진입."""
    return "finish" if _terminal_reason(state) is not None else "continue"


# ─────────────────────────────────────────────────────────────────────────────
# 프롬프트 변수 보조
# ─────────────────────────────────────────────────────────────────────────────


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
    goals = state["slot_answers"].get("goals.heaviest")
    if goals and goals.get("type") == "text":
        return str(goals.get("raw", "")) or "당신의 목표"
    return "당신의 목표"


def build_interview_graph() -> CompiledStateGraph[
    InterviewState, Any, InterviewState, InterviewState
]:
    """Cyclic StateGraph 컴파일. 라우터는 `await graph.ainvoke(initial, config=...)`."""
    graph = StateGraph(InterviewState)
    graph.add_node("ask_next_slot", ask_next_slot)
    graph.add_node("receive_answer", receive_answer)
    graph.add_node("update_ambiguity", update_ambiguity)
    graph.add_node("finalize_outcome", finalize_outcome)

    graph.set_entry_point("ask_next_slot")
    graph.add_edge("ask_next_slot", "receive_answer")
    graph.add_edge("receive_answer", "update_ambiguity")
    graph.add_conditional_edges(
        "update_ambiguity",
        should_continue,
        {"continue": "ask_next_slot", "finish": "finalize_outcome"},
    )
    graph.add_edge("finalize_outcome", END)
    return graph.compile()
