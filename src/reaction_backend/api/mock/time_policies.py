"""Time Policies mock fixture — #3-C 스텁용 (S07).

데모 시간 정책. 실제 계획 제약 적용·인터뷰 prefill 로직은 후속 도메인 이슈.
policy_type: sleep | lunch | break_min | no_touch | late_night_block | custom
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class DemoTimePolicy:
    """시간 정책 한 건 (api-contract §5). payload 는 policy_type 별로 형태가 다름."""

    policy_id: str
    policy_type: str
    payload: dict[str, JsonValue]
    is_active: bool


# demo user 의 활성 시간 정책 — GET /time-policies / prefill 응답 공통 사용.
DEMO_POLICIES: tuple[DemoTimePolicy, ...] = (
    DemoTimePolicy("policy_demo_sleep", "sleep", {"startTime": "23:00", "endTime": "07:00"}, True),
    DemoTimePolicy("policy_demo_lunch", "lunch", {"startTime": "12:00", "endTime": "13:00"}, True),
    DemoTimePolicy("policy_demo_break", "break_min", {"minMinutes": 10}, True),
)
