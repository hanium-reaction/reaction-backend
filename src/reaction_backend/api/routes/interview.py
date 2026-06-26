"""Interview — 딥 인터뷰 (S02, api-contract §4) — #6 실배선.

mock 스텁을 걷어내고 LangGraph 인터뷰 엔진(`orchestrator/interview*`)에 연결한다.

배선 두 축:
1. **route → 엔진** — 각 핸들러가 `interview_runner` 의 턴 함수를 호출한다
   (start / submit_and_advance / finish_early). 반환은 envelope 없이 도메인 객체.
2. **영속화(상태 재조립)** — `interview_sessions` 는 상태 통짜 저장(JSON) 칸이 없으므로
   매 요청마다 스칼라(total_turns·ambiguity_final) + `interview_slot_answers` 행을 읽어
   `InterviewState` 로 재조립(`_state_from_db`)하고, 턴 후 다시 영속(`_persist_turn`)한다.

엔진 ↔ FE 스키마 번역:
- `ambiguityScore`(int) = 남은 미해결 필수 슬롯 수 (진행될수록 감소).
- `Question` = 엔진 질문 텍스트 + 슬롯 카탈로그(answer_type·options). `goals.heaviest` 보기는
  `goals.list` 응답에서 런타임 동적 생성.
- 종료 턴에는 `summary`(S03 확인 카드) + `outcome`(First Plan 시드)을 함께 싣는다.

동시성/세션 가드:
- 단일 활성 세션 enforce — 진행 중 세션이 있으면 새 세션 생성 시 409 `INTERVIEW_SESSION_EXISTS`.
- 동시성 lock(ADR-0005 §7.6) — mutating 진입점은 `user_agent_lock` 으로 보호, 다중 디바이스
  동시 진입 시 409 `AGENT_CONCURRENT_ACCESS`.

⚠️ 본 PR 의 한계(후속): 재조립 시 transient 상태(stall_count·used_fallback)는 리셋된다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, status
from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.api.mock.interview import SLOT_CATALOG, InterviewSlot
from reaction_backend.config import get_settings
from reaction_backend.db.models.interview_session import InterviewSession as InterviewSessionRow
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator import interview, interview_adapter, interview_runner
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.orchestrator.interview import InterviewState
from reaction_backend.repositories.interview_repo import InterviewRepo, get_interview_repo
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.interview import (
    InterviewEndReason,
    InterviewSession,
    Question,
    SlotAnswerRequest,
    SlotCatalogEntry,
)

router = APIRouter(prefix="/interview", tags=["interview"])

# ADR-0005 §7.6 — Interview 동시성 lock 의 agent 식별자.
_LOCK_AGENT = "interview"

_CATALOG_BY_KEY: dict[str, InterviewSlot] = {s.slot_key: s for s in SLOT_CATALOG}
_REQUIRED_KEYS = interview_adapter.REQUIRED_SLOT_KEYS

RepoDep = Annotated[InterviewRepo, Depends(get_interview_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


# ─────────────────────────────────────────────────────────────────────────────
# helpers — 에러 / config / 재조립 / 매핑 / 영속화
# ─────────────────────────────────────────────────────────────────────────────


def _not_found() -> ApiError:
    return ApiError(
        ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
        "해당 인터뷰 세션을 찾을 수 없어요.",
        http_status=status.HTTP_404_NOT_FOUND,
    )


def _session_exists() -> ApiError:
    return ApiError(
        ErrorCode.INTERVIEW_SESSION_EXISTS,
        "이미 진행 중인 인터뷰가 있어요. 기존 세션을 이어서 진행해 주세요.",
        http_status=status.HTTP_409_CONFLICT,
    )


def _parse_session_id(session_id: str) -> UUID:
    try:
        return UUID(session_id)
    except ValueError as e:
        raise _not_found() from e


def _config(session: AsyncSession) -> RunnableConfig:
    """노드가 예산 가드·llm_runs 기록에 쓰는 세션 채널 (ADR-0005 §7.1)."""
    return {"configurable": {"session": session}}


async def _load(repo: InterviewRepo, user_id: UUID, session_id: str) -> InterviewSessionRow:
    row = await repo.get_active(user_id, _parse_session_id(session_id))
    if row is None:
        raise _not_found()
    return row


def _state_from_db(
    row: InterviewSessionRow, slot_rows: list[InterviewSlotAnswer]
) -> InterviewState:
    """interview_sessions 스칼라 + slot_answers 행 → InterviewState 재조립.

    transient(stall_count·used_fallback·next_*)은 initial_state default(0/False/None)로
    시작 — 재조립 한계(PR 본문). 영속 대상은 slot_answers·ambiguity·total_turns.
    """
    state = interview.initial_state(session_id=row.id, user_id=row.user_id)
    state["slot_answers"] = {r.slot_key: r.value for r in slot_rows if r.value is not None}
    if row.ambiguity_final is not None:
        state["ambiguity_score"] = float(row.ambiguity_final)
    state["total_turns"] = row.total_turns
    return state


def _remaining_required(slot_answers: Mapping[str, dict[str, Any] | None]) -> int:
    """남은 미해결 필수 슬롯 수 → FE ambiguityScore(int)."""
    return sum(1 for k in _REQUIRED_KEYS if not slot_answers.get(k))


def _question_options(
    slot_key: str, slot_answers: Mapping[str, dict[str, Any] | None]
) -> list[str]:
    """chip/select 보기. `goals.heaviest` 는 사용자가 나열한 goals.list 에서 동적 생성."""
    if slot_key == "goals.heaviest":
        goals = slot_answers.get("goals.list")
        if isinstance(goals, dict) and goals.get("type") == "text":
            norm = goals.get("normalized")
            if isinstance(norm, list):
                return [str(x) for x in norm if str(x).strip()]
            raw = goals.get("raw")
            if isinstance(raw, str) and raw.strip():
                return [raw.strip()]
        return []
    slot = _CATALOG_BY_KEY.get(slot_key)
    return list(slot.options) if slot else []


def _to_question(state: InterviewState) -> Question | None:
    """엔진 질문(NextQuestionSchema) + 슬롯 카탈로그 → FE Question."""
    nq = state["next_question"]
    slot_key = state["next_slot_key"]
    if nq is None or not slot_key:
        return None
    slot = _CATALOG_BY_KEY.get(slot_key)
    return Question(
        slot_key=slot_key,
        text=nq.question,
        answer_type=slot.answer_type if slot else "text",
        options=_question_options(slot_key, state["slot_answers"]),
    )


def _response(
    session_id: UUID,
    state: InterviewState,
    *,
    end_reason: str | None = None,
    summary: Any = None,
    outcome: Any = None,
) -> InterviewSession:
    return InterviewSession(
        session_id=str(session_id),
        ambiguity_score=_remaining_required(state["slot_answers"]),
        total_turns=state["total_turns"],
        end_reason=end_reason,
        current_question=None if end_reason is not None else _to_question(state),
        summary=summary,
        outcome=outcome,
    )


def _ended_response(
    row: InterviewSessionRow, slot_rows: list[InterviewSlotAnswer]
) -> InterviewSession:
    """이미 종료된 세션 재조회 — outcome 은 slot_answers 에서 결정적 재빌드(LLM 0회)."""
    slot_answers = {r.slot_key: r.value for r in slot_rows if r.value is not None}
    outcome = interview_adapter.build_outcome(
        session_id=str(row.id),
        slot_answers=slot_answers,
        ambiguity_final=float(row.ambiguity_final) if row.ambiguity_final is not None else 0.0,
        end_reason=cast(InterviewEndReason, row.end_reason or "completed"),
        analysis_source="rule",
    )
    return InterviewSession(
        session_id=str(row.id),
        ambiguity_score=_remaining_required(slot_answers),
        total_turns=row.total_turns,
        end_reason=row.end_reason,
        current_question=None,
        summary=None,
        outcome=outcome,
    )


async def _persist_turn(
    repo: InterviewRepo, row: InterviewSessionRow, state: InterviewState
) -> None:
    """턴 결과 영속: slot_answers UPSERT + 진행 스칼라 저장."""
    for slot_key, value in state["slot_answers"].items():
        await repo.upsert_slot_answer(
            row.id,
            slot_key,
            value,
            is_required=slot_key in _REQUIRED_KEYS,
        )
    await repo.save_progress(
        row, total_turns=state["total_turns"], ambiguity_final=state["ambiguity_score"]
    )


# ─────────────────────────────────────────────────────────────────────────────
# endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def start_session(user: CurrentUser, repo: RepoDep, session: SessionDep) -> InterviewSession:
    """딥 인터뷰 세션 시작 — FSM 이 고른 첫 필수 슬롯 질문 1개 생성.

    단일 활성 세션 enforce: 이미 진행 중(end_reason IS NULL)인 세션이 있으면 409.
    동시성 lock(ADR-0005 §7.6) 안에서 검사+생성해 다중 디바이스 race 를 막는다.
    """
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        if await repo.get_active_session(user.id) is not None:
            raise _session_exists()
        row = await repo.create_session(user.id, get_settings().llm_model)
        result = await interview_runner.start_interview(
            session_id=row.id, user_id=user.id, session=session, tone_mode=user.tone_mode
        )
        await _persist_turn(repo, row, result.state)
        await session.commit()
        return _response(row.id, result.state)


@router.get("/slot-catalog")
async def get_slot_catalog() -> list[SlotCatalogEntry]:
    """슬롯 카탈로그 — 클라이언트가 라벨·입력형식·보기(options) 렌더링에 사용."""
    return [
        SlotCatalogEntry(
            slot_key=s.slot_key,
            label=s.label,
            answer_type=s.answer_type,
            is_required=s.is_required,
            category=s.category,
            options=list(s.options),
        )
        for s in SLOT_CATALOG
    ]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user: CurrentUser, repo: RepoDep) -> InterviewSession:
    """인터뷰 진행 상태(모호함 지표). 종료 세션이면 outcome 동봉, 진행 중이면 질문 없음."""
    row = await _load(repo, user.id, session_id)
    slot_rows = await repo.list_slot_answers(row.id)
    if row.end_reason is not None:
        return _ended_response(row, slot_rows)
    slot_answers = {r.slot_key: r.value for r in slot_rows if r.value is not None}
    return InterviewSession(
        session_id=str(row.id),
        ambiguity_score=_remaining_required(slot_answers),
        total_turns=row.total_turns,
        end_reason=None,
        current_question=None,
        summary=None,
        outcome=None,
    )


@router.post("/sessions/{session_id}/answers")
async def submit_answer(
    session_id: str,
    body: SlotAnswerRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> InterviewSession:
    """슬롯 답 1개 주입 → 채점/정규화/저장 → 종료면 요약+outcome, 아니면 다음 질문.

    동시성 lock(ADR-0005 §7.6): 다중 디바이스 동시 답 제출로 인한 state race 방지.
    """
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        row = await _load(repo, user.id, session_id)
        if row.end_reason is not None:
            return _ended_response(row, await repo.list_slot_answers(row.id))

        slot_rows = await repo.list_slot_answers(row.id)
        state = _state_from_db(row, slot_rows)
        result = await interview_runner.submit_and_advance(
            state=state,
            slot_key=body.slot_key,
            answer_value=body.value,
            session=session,
            tone_mode=user.tone_mode,
        )
        await _persist_turn(repo, row, result.state)

        if result.done:
            reason = result.end_reason or "completed"
            await repo.finalize(
                row,
                end_reason=reason,
                total_turns=result.state["total_turns"],
                ambiguity_final=result.state["ambiguity_score"],
            )
            await session.commit()
            return _response(
                row.id,
                result.state,
                end_reason=reason,
                summary=result.summary,
                outcome=result.outcome,
            )

        await session.commit()
        return _response(row.id, result.state)


@router.post("/sessions/{session_id}/next-question")
async def next_question(
    session_id: str, user: CurrentUser, repo: RepoDep, session: SessionDep
) -> InterviewSession:
    """현재 미해결 슬롯의 질문 1개 재생성 — 중단된 세션 재개(resume)용.

    동시성 lock(ADR-0005 §7.6): 동시 재개 진입으로 인한 state race 방지.
    """
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        row = await _load(repo, user.id, session_id)
        if row.end_reason is not None:
            return _ended_response(row, await repo.list_slot_answers(row.id))
        slot_rows = await repo.list_slot_answers(row.id)
        state = _state_from_db(row, slot_rows)
        state = await interview.ask_question(state, _config(session))
        await _persist_turn(repo, row, state)
        await session.commit()
        return _response(row.id, state)


@router.post("/sessions/{session_id}/finish")
async def finish_session(
    session_id: str, user: CurrentUser, repo: RepoDep, session: SessionDep
) -> InterviewSession:
    """[충분해요] 조기 종료 — 남은 슬롯은 안전 default 로 채우고 outcome 빌드.

    동시성 lock(ADR-0005 §7.6): 동시 종료/답 제출로 인한 state race 방지.
    """
    async with user_agent_lock(session, user.id, _LOCK_AGENT):
        row = await _load(repo, user.id, session_id)
        if row.end_reason is not None:
            return _ended_response(row, await repo.list_slot_answers(row.id))
        slot_rows = await repo.list_slot_answers(row.id)
        state = _state_from_db(row, slot_rows)
        result = await interview_runner.finish_early(
            state=state, session=session, tone_mode=user.tone_mode
        )
        await _persist_turn(repo, row, result.state)
        reason = result.end_reason or "early_user"
        await repo.finalize(
            row,
            end_reason=reason,
            total_turns=result.state["total_turns"],
            ambiguity_final=result.state["ambiguity_score"],
        )
        await session.commit()
        return _response(
            row.id, result.state, end_reason=reason, summary=result.summary, outcome=result.outcome
        )
