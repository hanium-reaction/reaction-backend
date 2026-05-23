"""Onboarding — 상태 머신 라우팅 (S01–S08, api-contract §3).

users.onboarding_state 진실의 근원:
  WELCOME → ONBOARDING_INTERVIEW → ONBOARDING_CONFIRM
  → ONBOARDING_CALENDAR ⇄ ONBOARDING_MANUAL_SCHEDULE
  → ONBOARDING_POLICIES → ONBOARDING_FIRST_PLAN
  → ONBOARDING_NOTIFICATIONS → ACTIVE

Issue #16 실구현: 인증 필수, `CurrentUser.onboarding_state` 기반 다음 화면 안내.
WELCOME 사용자는 S02(interview)부터, ACTIVE 사용자는 S10(메인) 으로.
실제 상태 전이는 각 도메인 라우터(interview/time_policies/...)가 자기 단계 완료 시 수행 (#17~).
"""

from fastapi import APIRouter

from reaction_backend.api.deps import CurrentUser
from reaction_backend.schemas.onboarding import OnboardingStatus

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# onboarding_state → 다음 진입 화면 (DevBaseline §5 화면 흐름).
# WELCOME 은 로그인 직후 상태 — 다음으로 가야 할 화면은 S02(interview).
_STATE_TO_SCREEN: dict[str, str] = {
    "WELCOME": "S02",
    "ONBOARDING_INTERVIEW": "S02",
    "ONBOARDING_CONFIRM": "S03",
    "ONBOARDING_CALENDAR": "S04",
    "ONBOARDING_MANUAL_SCHEDULE": "S05",
    "ONBOARDING_POLICIES": "S07",
    "ONBOARDING_FIRST_PLAN": "S06",
    "ONBOARDING_NOTIFICATIONS": "S08",
    "ACTIVE": "S10",
}


@router.get("/status")
async def get_onboarding_status(user: CurrentUser) -> OnboardingStatus:
    """현재 사용자의 onboarding 상태 + 다음 가야 할 화면 hint."""
    state = user.onboarding_state
    return OnboardingStatus(
        current_state=state,
        suggested_next_screen=_STATE_TO_SCREEN.get(state, "S02"),
    )
