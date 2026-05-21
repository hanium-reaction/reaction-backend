"""Onboarding — 상태 머신 라우팅.

users.onboarding_state 진실의 근원:
  WELCOME → ONBOARDING_INTERVIEW → ONBOARDING_CONFIRM
  → ONBOARDING_CALENDAR ⇄ ONBOARDING_MANUAL_SCHEDULE
  → ONBOARDING_POLICIES → ONBOARDING_FIRST_PLAN
  → ONBOARDING_NOTIFICATIONS → ACTIVE

예정 endpoint:
- GET /onboarding/status     — 현재 상태 + 다음 화면 hint
- POST /onboarding/transition — 명시적 전이 (디버그/관리자)

구현 위치: 각 단계의 도메인 라우터(interview/time_policies/...)가
자기 단계 완료 시 onboarding_state를 전이시킴. 이 라우터는 read 위주.
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.get("/status", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_onboarding_status() -> None:
    """현재 사용자의 onboarding 상태와 다음 가야 할 화면 hint."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §3 — to be implemented in a follow-up.",
    )
