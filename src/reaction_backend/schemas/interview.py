"""Interview 도메인 스키마 (api-contract §4) — S02 딥 인터뷰.

#3-B 단계는 정적 mock 스텁. 적응형 질문 선택·모호함 채점·LLM 호출은 #6.
"""

from __future__ import annotations

from pydantic import Field, JsonValue

from reaction_backend.schemas.common import CamelModel


class SlotCatalogEntry(CamelModel):
    """슬롯 카탈로그 한 항목 — GET /interview/slot-catalog."""

    slot_key: str
    label: str
    answer_type: str
    is_required: bool
    category: str


class Question(CamelModel):
    """인터뷰 질문 — 세션의 currentQuestion."""

    slot_key: str
    text: str
    answer_type: str
    options: list[str]


class InterviewSession(CamelModel):
    """인터뷰 세션 상태 — sessions·answers·next-question·finish 공통 응답."""

    session_id: str
    ambiguity_score: int
    total_turns: int
    end_reason: str | None
    current_question: Question | None


class SlotAnswerRequest(CamelModel):
    """POST /interview/sessions/{id}/answers 요청 — 슬롯 답 UPSERT."""

    slot_key: str = Field(min_length=1)
    value: JsonValue
    client_turn: int = Field(ge=0)
