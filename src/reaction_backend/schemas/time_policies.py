"""Time Policies 도메인 스키마 (api-contract §5) — S07.

#3-C 단계는 mock 스텁. 실제 정책 제약 적용은 후속.
"""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue

from reaction_backend.schemas.common import CamelModel

# policy_type 6종 (DevBaseline / api-contract §5)
PolicyType = Literal["sleep", "lunch", "break_min", "no_touch", "late_night_block", "custom"]


class TimePolicy(CamelModel):
    """시간 정책 — GET/POST/PATCH 응답. payload 는 policy_type 별로 형태가 다름."""

    policy_id: str
    policy_type: str
    payload: dict[str, JsonValue]
    is_active: bool


class TimePolicyCreateRequest(CamelModel):
    """POST /time-policies 요청."""

    policy_type: PolicyType
    payload: dict[str, JsonValue]


class TimePolicyUpdateRequest(CamelModel):
    """PATCH /time-policies/{id} 요청 — 부분 수정."""

    payload: dict[str, JsonValue] | None = None
    is_active: bool | None = None
