"""Recovery — S19, S20. if-then 코핑 플랜.

흐름 (Orchestrator 2 — Recovery):
  DETECTING → DIAGNOSING → COACHING → HITL → UPDATING → SAVING

핵심 결정:
- UX 4 그룹 (DOWNSCOPE / RESCHEDULE / CARRY_OVER / PARK)
- 내부 9 전략 (NANO_STEP / DOWNSCOPE_DEFAULT / ENVIRONMENT_SHIFT / CONTEXT_REWARMING
  / RESCHEDULE_DEFAULT / ACTIVE_RECOVERY / CARRYOVER_DEFAULT / FREEZE_SLOT / PARK_DEFAULT)
- 같은 그룹에서 동시 노출 카드 최대 1개. 9 전략은 통계/감사용 보존.
- 8초 안에 LLM 응답 못 받으면 heuristic fallback (PRD §9)
- 원본 action_item.status (FAILED 등)는 절대 변경 X — Resilience 지표 전제

DB: recovery_attempts, recovery_strategy_catalog (v0.7 9전략), action_items (new),
    scheduled_blocks (new), idempotency_keys, llm_runs

예정 endpoint:
- POST /recovery/proposals/generate          — Recovery Coach 호출 → 후보 2~4개
- POST /recovery/decisions                   — 사용자 선택 저장 (Idempotency)
- GET  /replan/{execution_id}                — S20 before/after 비교
- POST /replan/{execution_id}/approve        — 최종 적용 (Idempotency)

구현 위치: agents/{failure_diagnosis,recovery_coach}_agent.py + orchestrator/recovery.py
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/recovery", tags=["recovery"])


@router.post("/proposals/generate", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def generate_recovery_proposals() -> None:
    """실패 컨텍스트 기반 회복 옵션 2~4개 생성 (LLM + heuristic fallback)."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §12 — to be implemented in a follow-up.",
    )
