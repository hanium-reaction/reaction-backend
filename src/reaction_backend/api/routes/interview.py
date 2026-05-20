"""Interview — 딥 인터뷰 (S02). re:action의 정확도 씨앗.

핵심 메커니즘 (DevBaseline §6):
- 19 슬롯(필수 11 + 선택 8) — identity/goals/time/energy/recovery/constraints
- 모호함 지표 = 필수 슬롯 중 미입력 또는 clarity_score < 0.5 개수
- 모호함 0 또는 15턴 또는 [충분해요] 탭 → 종료
- 매 턴 LLM 호출 → llm_runs 로깅, 금지어 후처리 필터

DB: interview_sessions, interview_slot_answers ((session_id, slot_key) UNIQUE), llm_runs

예정 endpoint:
- POST /interview/sessions                      — 세션 시작
- GET  /interview/sessions/{id}                 — 진행 상태 (모호함 지표 포함)
- POST /interview/sessions/{id}/answers         — 슬롯 답 제출 (UPSERT)
- POST /interview/sessions/{id}/next-question   — 다음 질문 요청 (LLM 호출)
- POST /interview/sessions/{id}/finish          — 조기 종료 [충분해요]
- GET  /interview/sessions/{id}/slot-catalog    — 슬롯 정의 (클라가 라벨 렌더링)

구현 위치: agents/interview_agent.py + orchestrator/interview_orchestrator.py (Issue #5)
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/interview", tags=["interview"])


@router.post("/sessions", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def start_session() -> None:
    """딥 인터뷰 세션 시작 (사용자당 진행 중 1개)."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §4 — to be implemented in a follow-up.",
    )
