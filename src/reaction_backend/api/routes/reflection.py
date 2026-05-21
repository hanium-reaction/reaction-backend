"""Reflection — 저녁 회고 (S17, S18). "회복 골든 타임".

핵심 결정 (DevBaseline §1.4 잠금):
- 21시 일괄 회고만, 실패 직후 X
- 최대 3일 누적 (오늘+어제+그제). 3일 초과는 system_failure_reason='reflection_skipped'로 자동 만료
- Idempotency 24h 강제 ([모두 완료] 중복 탭 방지)

실패 사유 13종 enum: TIME_SHORTAGE / LOW_ENERGY / HARD_TO_START / PRIORITY_SHIFT
/ PLAN_TOO_BIG / FATIGUE / AMBIGUITY / CONFLICT / OVERRUN / AVOIDANCE / DISTRACTION
/ EMERGENCY / CONTEXT_LOSS — 최대 2개 선택, memo는 at-rest 암호화.

DB: action_items, execution_events, habit_instances, execution_failure_tags,
    failure_reason_tags (마스터), idempotency_keys

예정 endpoint:
- GET  /reflection/pending                 — S17 진입 시 미체크 카드 조회 (3일 누적)
- POST /reflection/batch                   — [모두 완료] 일괄 처리 (Idempotency-Key 필수)
- GET  /reflection/failure-tags            — 13종 마스터 (활성만)
- POST /reflection/failure-tags/{exec_id}  — 실패 사유 태깅 (0~2개, memo 암호화)

구현 위치: domain/reflection/ + safety/ (메모 암호화)
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/reflection", tags=["reflection"])


@router.post("/batch", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def batch_reflect() -> None:
    """오늘+어제+그제 미체크 카드 일괄 처리. Idempotency-Key 헤더 필수."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §11 — to be implemented in a follow-up.",
    )
