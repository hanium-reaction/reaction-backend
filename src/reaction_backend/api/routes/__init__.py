"""도메인별 라우터. 각 파일이 한 도메인을 담당.

re:action API는 16개 도메인으로 묶인다 (docs/api-contract.md §1 참고):
  health · auth · onboarding · interview · time_policies · goals · habits
  · planning · calendar · today · reflection · recovery · review · policy
  · notifications · settings

각 라우터는 placeholder 단계이며, 실제 구현은 후속 이슈에서 채운다.
"""

from reaction_backend.api.routes import (
    auth,
    calendar,
    fixed_schedules,
    goals,
    habits,
    health,
    inbox,
    interview,
    notifications,
    onboarding,
    planning,
    policy,
    recovery,
    reflection,
    review,
    settings,
    time_policies,
    today,
)

__all__ = [
    "auth",
    "calendar",
    "fixed_schedules",
    "goals",
    "habits",
    "health",
    "inbox",
    "interview",
    "notifications",
    "onboarding",
    "planning",
    "policy",
    "recovery",
    "reflection",
    "review",
    "settings",
    "time_policies",
    "today",
]
