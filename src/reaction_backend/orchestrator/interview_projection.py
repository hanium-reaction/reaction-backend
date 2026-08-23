"""종료된 인터뷰 세션 → `InterviewOutcome` 투영 (DB 읽기 + 결정적 변환).

`interview_adapter.build_outcome` 은 **순수 함수(DB 무관)** 로 선언돼 있어(그 모듈 docstring)
세션 행을 읽는 책임을 거기 둘 수 없다. 그렇다고 라우터마다 "슬롯을 읽어 build_outcome 에
넘긴다" 를 반복하면 end_reason·analysis_source 를 어떻게 유도하는지가 **두 번째 진실**로
갈라진다 — 실제로 계획 생성과 자료 확정이 서로 다른 outcome 을 보게 된다.

그래서 그 조립만 여기 한 곳에 둔다. LLM 0회.
"""

from __future__ import annotations

from typing import cast

from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.orchestrator import interview_adapter
from reaction_backend.repositories.interview_repo import InterviewRepo
from reaction_backend.schemas.interview import InterviewEndReason, InterviewOutcome

__all__ = ["project_session_outcome"]


async def project_session_outcome(row: InterviewSession, repo: InterviewRepo) -> InterviewOutcome:
    """세션의 slot_answers 를 outcome 으로 결정적 투영 (LLM 0회)."""
    slot_rows = await repo.list_slot_answers(row.id)
    slot_answers = {r.slot_key: r.value for r in slot_rows if r.value is not None}
    return interview_adapter.build_outcome(
        session_id=str(row.id),
        slot_answers=slot_answers,
        ambiguity_final=(float(row.ambiguity_final) if row.ambiguity_final is not None else 0.0),
        end_reason=cast(InterviewEndReason, row.end_reason or "completed"),
        # 인터뷰 정규화가 LLM 이었는지 룰 fallback 이었는지 (세션에 영속된 플래그).
        analysis_source="rule" if row.used_fallback else "llm",
    )
