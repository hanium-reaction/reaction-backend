"""Onboarding — 상태 머신 라우팅 (S01–S08, api-contract §3).

users.onboarding_state 진실의 근원:
  WELCOME → ONBOARDING_INTERVIEW → ONBOARDING_CONFIRM
  → ONBOARDING_CALENDAR ⇄ ONBOARDING_MANUAL_SCHEDULE
  → ONBOARDING_POLICIES → ONBOARDING_FIRST_PLAN
  → ONBOARDING_NOTIFICATIONS → ACTIVE

#3-B 단계는 **mock 스텁** — demo user 의 상태를 그대로 반환한다.
실제 상태 전이는 각 도메인 라우터(interview/time_policies/...)가 자기 단계 완료 시 수행 (#16~).
"""

from fastapi import APIRouter

from reaction_backend.api.mock.demo import DEMO_USER
from reaction_backend.schemas.onboarding import OnboardingStatus

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# onboarding_state → 진입 화면 (DevBaseline §5 화면 흐름).
_STATE_TO_SCREEN: dict[str, str] = {
    "WELCOME": "S01",
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
async def get_onboarding_status() -> OnboardingStatus:
    """[stub] 현재 사용자의 onboarding 상태 + 다음 가야 할 화면 hint."""
    state = DEMO_USER.onboarding_state
    return OnboardingStatus(
        current_state=state,
        suggested_next_screen=_STATE_TO_SCREEN.get(state, "S01"),
    )
