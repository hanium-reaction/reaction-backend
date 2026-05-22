"""Time Policies — 시간 정책 (S07, api-contract §5).

#3-C 단계는 **mock 스텁**: demo user 의 고정 정책 집합을 반환한다.
실제 계획 제약 적용·인터뷰 기반 prefill 로직은 후속 도메인 이슈.
"""

from fastapi import APIRouter, status

from reaction_backend.api.mock.time_policies import DEMO_POLICIES, DemoTimePolicy
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.time_policies import (
    TimePolicy,
    TimePolicyCreateRequest,
    TimePolicyUpdateRequest,
)

router = APIRouter(prefix="/time-policies", tags=["time-policies"])


def _to_schema(demo: DemoTimePolicy) -> TimePolicy:
    return TimePolicy(
        policy_id=demo.policy_id,
        policy_type=demo.policy_type,
        payload=demo.payload,
        is_active=demo.is_active,
    )


def _find(policy_id: str) -> DemoTimePolicy:
    """스텁은 DEMO_POLICIES 의 id 만 유효 — 그 외는 404."""
    for policy in DEMO_POLICIES:
        if policy.policy_id == policy_id:
            return policy
    raise ApiError(
        ErrorCode.POLICY_NOT_FOUND,
        "해당 시간 정책을 찾을 수 없어요.",
        http_status=status.HTTP_404_NOT_FOUND,
    )


@router.get("")
async def list_policies() -> list[TimePolicy]:
    """[stub] 내 활성 시간 정책 전체."""
    return [_to_schema(policy) for policy in DEMO_POLICIES]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_policy(body: TimePolicyCreateRequest) -> TimePolicy:
    """[stub] 신규 시간 정책 추가."""
    return TimePolicy(
        policy_id="policy_new_stub",
        policy_type=body.policy_type,
        payload=body.payload,
        is_active=True,
    )


@router.post("/prefill-from-interview")
async def prefill_from_interview() -> list[TimePolicy]:
    """[stub] 인터뷰 답 기반 정책 prefill 제안 (S07 진입 시)."""
    return [_to_schema(policy) for policy in DEMO_POLICIES]


@router.patch("/{policy_id}")
async def update_policy(policy_id: str, body: TimePolicyUpdateRequest) -> TimePolicy:
    """[stub] 시간 정책 부분 수정."""
    demo = _find(policy_id)
    return TimePolicy(
        policy_id=demo.policy_id,
        policy_type=demo.policy_type,
        payload=body.payload if body.payload is not None else demo.payload,
        is_active=body.is_active if body.is_active is not None else demo.is_active,
    )


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(policy_id: str) -> None:
    """[stub] 시간 정책 soft delete (is_active=false)."""
    _find(policy_id)
    return None
