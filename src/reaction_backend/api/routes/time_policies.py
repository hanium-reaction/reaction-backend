"""Time Policies — S07. 계획 생성의 핵심 제약.

policy_type별 payload (discriminated union):
- sleep       — {start_time, end_time}  (최소 1개 활성 필수)
- lunch       — {start_time, end_time}
- break_min   — {min_minutes}            (카드 간 최소 휴식)
- no_touch    — {days_of_week, start_time, end_time}
- late_night_block — {start_time, blocked_categories}
- custom      — 자유

규칙: 계획 생성 시 이 정책을 침범하는 scheduled_block은 트랜잭션 롤백 후 재시도.

DB: time_policies, behavioral_profiles (interview prefill 기준)

예정 endpoint:
- GET    /time-policies               — 내 정책 전체
- POST   /time-policies               — 신규 정책 추가
- POST   /time-policies/prefill-from-interview  — 인터뷰 답 → 초기 정책 prefill (S07 진입 시)
- PATCH  /time-policies/{id}          — 부분 수정
- DELETE /time-policies/{id}          — soft delete (is_active=false)

구현 위치: Issue #2(DB) → Issue #1 follow-up.
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/time-policies", tags=["time-policies"])


@router.get("", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def list_my_policies() -> None:
    """내 활성 시간 정책 전체."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §5 — to be implemented in a follow-up.",
    )
