"""Interview turn driver — FastAPI 라우터 ↔ Interview FSM 브리지 (ADR-0005 §7.3).

딥 인터뷰는 사용자 답이 매 HTTP 요청으로 외부에서 들어오는 **턴 단위** 흐름이라,
그래프 전체를 한 번에 `ainvoke` 하지 않는다(그러면 답을 기다릴 수 없다). 대신 라우터는
이 모듈의 함수를 호출하고, 각 함수는 `interview.py` 의 노드(일반 async 함수)를 직접 엮어
"질문 1개 응답" 또는 "요약 + InterviewOutcome" 을 돌려준다.

상태(`InterviewState`)는 직렬화 가능하므로 라우터가 요청 사이에 보관한다
(권장: `interview_sessions` 스칼라 + `interview_slot_answers` 행으로 영속, 매 요청 복원).
세션(AsyncSession)은 state 가 아니라 `config["configurable"]["session"]` 로만 전달한다.

Envelope-less: 반환은 도메인 객체(`NextQuestionSchema` / `InterviewOutcome`) 그대로.
8s timeout / rate limit 은 각 노드의 룰 fallback 이 흡수하므로 이 레이어는 실패하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.orchestrator import interview
from reaction_backend.orchestrator.interview import InterviewState
from reaction_backend.schemas.interview import (
    InterviewOutcome,
    InterviewSummary,
    NextQuestionSchema,
)

__all__ = ["TurnResult", "finish_early", "start_interview", "submit_and_advance"]


@dataclass(slots=True)
class TurnResult:
    """한 턴의 결과. `done=False` 면 `question` 으로 계속, `done=True` 면 `outcome` 확정.

    `state` 는 라우터가 다음 턴까지 보관(영속)할 직렬화 가능한 인터뷰 상태다.
    """

    state: InterviewState
    done: bool
    question: NextQuestionSchema | None = None
    summary: InterviewSummary | None = None  # 요약 확인 카드 (done=True 일 때)
    outcome: InterviewOutcome | None = None  # 경계 계약 (done=True 일 때)
    end_reason: str | None = None


def _config(session: AsyncSession | None) -> RunnableConfig:
    """노드가 예산 가드·llm_runs 기록에 쓰는 세션 채널 (ADR-0005 §7.1)."""
    return {"configurable": {"session": session}}


def _coerce_answer(value: Any) -> dict[str, Any]:
    """라우터가 받은 JsonValue 를 slot_answers value 형식으로 환원.

    이미 `{"type": ...}` 형태면 그대로 신뢰한다(클라이언트가 카탈로그 answerType 대로 보냄).
    """
    if isinstance(value, dict) and "type" in value:
        return value
    if isinstance(value, dict) and "start" in value and "end" in value:
        return {"type": "range", "start": value["start"], "end": value["end"]}
    if isinstance(value, list):
        return {"type": "chip", "values": value}
    return {"type": "text", "raw": str(value)}


async def start_interview(
    *,
    session_id: UUID,
    user_id: UUID,
    session: AsyncSession | None = None,
) -> TurnResult:
    """세션 시작 → FSM 이 고른 첫 필수 슬롯 질문 1개를 만들어 반환."""
    config = _config(session)
    state = interview.initial_state(session_id=session_id, user_id=user_id)
    state = await interview.ask_question(state, config)
    return TurnResult(state=state, done=False, question=state["next_question"])


async def submit_and_advance(
    *,
    state: InterviewState,
    slot_key: str,
    answer_value: Any,
    session: AsyncSession | None = None,
) -> TurnResult:
    """답 1개 주입 → 채점/정규화/저장 → 종료면 요약+outcome, 아니면 다음 질문.

    이게 `POST /interview/sessions/{id}/answers` 가 호출하는 핵심 진입점이다.
    """
    config = _config(session)
    state = {**state, "last_answer": _coerce_answer(answer_value), "last_slot_key": slot_key}

    state = await interview.receive_answer(state, config)
    state = await interview.validate_answer(state, config)

    if interview.should_continue(state) == "finish":
        return await _finalize(state, config)

    state = await interview.ask_question(state, config)
    return TurnResult(state=state, done=False, question=state["next_question"])


async def finish_early(
    *,
    state: InterviewState,
    session: AsyncSession | None = None,
) -> TurnResult:
    """[충분해요] — 남은 슬롯이 있어도 즉시 마감(end_reason=early_user).

    빈 필수 슬롯은 `interview_adapter` 가 안전 default 로 채우고 unresolved_slots 에 남긴다.
    """
    config = _config(session)
    state = {**state, "early_finish": True}
    return await _finalize(state, config)


async def _finalize(state: InterviewState, config: RunnableConfig) -> TurnResult:
    state = await interview.summarize_interview(state, config)
    state = await interview.finalize_outcome(state, config)
    return TurnResult(
        state=state,
        done=True,
        summary=state["summary"],
        outcome=state["outcome"],
        end_reason=state["end_reason"],
    )
