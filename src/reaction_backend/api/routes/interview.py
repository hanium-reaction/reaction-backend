"""Interview — 딥 인터뷰 (S02, api-contract §4).

#3-B 단계는 **정적 mock 스텁**: 고정 세션·고정 질문 시퀀스를 반환한다.
적응형 질문 선택·모호함 채점·LLM 호출·세션 상태머신은 #6 에서 구현.
스텁은 DEMO_SESSION_ID 한 세션만 유효한 것으로 취급한다.
"""

from fastapi import APIRouter, status

from reaction_backend.api.mock.interview import (
    DEMO_QUESTIONS,
    DEMO_SESSION_ID,
    REQUIRED_SLOT_COUNT,
    SLOT_CATALOG,
    DemoQuestion,
)
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.interview import (
    InterviewSession,
    Question,
    SlotAnswerRequest,
    SlotCatalogEntry,
)

router = APIRouter(prefix="/interview", tags=["interview"])


def _question(demo: DemoQuestion) -> Question:
    return Question(
        slot_key=demo.slot_key,
        text=demo.text,
        answer_type=demo.answer_type,
        options=list(demo.options),
    )


def _ensure_demo_session(session_id: str) -> None:
    """스텁은 DEMO_SESSION_ID 만 유효한 세션으로 취급 — 그 외는 404."""
    if session_id != DEMO_SESSION_ID:
        raise ApiError(
            ErrorCode.INTERVIEW_SESSION_NOT_FOUND,
            "해당 인터뷰 세션을 찾을 수 없어요.",
            http_status=status.HTTP_404_NOT_FOUND,
        )


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def start_session() -> InterviewSession:
    """[stub] 딥 인터뷰 세션 시작 — 첫 질문 포함."""
    return InterviewSession(
        session_id=DEMO_SESSION_ID,
        ambiguity_score=REQUIRED_SLOT_COUNT,
        total_turns=0,
        end_reason=None,
        current_question=_question(DEMO_QUESTIONS[0]),
    )


@router.get("/slot-catalog")
async def get_slot_catalog() -> list[SlotCatalogEntry]:
    """[stub] 슬롯 카탈로그 — 클라이언트가 라벨·입력형식 렌더링에 사용."""
    return [
        SlotCatalogEntry(
            slot_key=slot.slot_key,
            label=slot.label,
            answer_type=slot.answer_type,
            is_required=slot.is_required,
            category=slot.category,
        )
        for slot in SLOT_CATALOG
    ]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> InterviewSession:
    """[stub] 인터뷰 진행 상태 (모호함 지표 포함)."""
    _ensure_demo_session(session_id)
    return InterviewSession(
        session_id=DEMO_SESSION_ID,
        ambiguity_score=9,
        total_turns=4,
        end_reason=None,
        current_question=_question(DEMO_QUESTIONS[1]),
    )


@router.post("/sessions/{session_id}/answers")
async def submit_answer(session_id: str, body: SlotAnswerRequest) -> InterviewSession:
    """[stub] 슬롯 답 UPSERT. 응답은 답 반영 후 세션 상태(정적값)."""
    _ensure_demo_session(session_id)
    return InterviewSession(
        session_id=DEMO_SESSION_ID,
        ambiguity_score=8,
        total_turns=5,
        end_reason=None,
        current_question=_question(DEMO_QUESTIONS[1]),
    )


@router.post("/sessions/{session_id}/next-question")
async def next_question(session_id: str) -> InterviewSession:
    """[stub] 다음 질문 요청. 적응형 선택·LLM 호출은 #6."""
    _ensure_demo_session(session_id)
    return InterviewSession(
        session_id=DEMO_SESSION_ID,
        ambiguity_score=8,
        total_turns=5,
        end_reason=None,
        current_question=_question(DEMO_QUESTIONS[2]),
    )


@router.post("/sessions/{session_id}/finish")
async def finish_session(session_id: str) -> InterviewSession:
    """[stub] 조기 종료 [충분해요] — endReason=early_user."""
    _ensure_demo_session(session_id)
    return InterviewSession(
        session_id=DEMO_SESSION_ID,
        ambiguity_score=8,
        total_turns=5,
        end_reason="early_user",
        current_question=None,
    )
