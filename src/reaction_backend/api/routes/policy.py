"""Policy Snapshot — 학습 루프의 산출물.

구성 (PolicySnapshot 4 영역):
- behavioral_profile     — attention_span, energy_cycle, time_chunk_preference, success_buffer
- execution_constraints  — daily_max_load, buffer_ratio, no_touch_zones
- interaction_style      — suggestion_style, recovery_tone, explanation_depth, reminder_frequency
- recovery_policy        — default_strategy_per_tag, min_recovery_step_minutes

규칙:
- 버전 보존(valid_from/valid_to). 롤백 가능
- 주간 KPI가 전주 대비 10%↓이면 자동 롤백 후보
- 새 버전 생성은 사용자 명시 [적용] (Verifier diff 표시) 후

DB: policy_snapshots (버전 이력), behavioral_profiles, interaction_styles

예정 endpoint:
- GET  /policy-snapshot/current             — 현재 활성 정책
- GET  /policy-snapshot/history             — 버전 이력
- POST /policy-snapshot/preview-update      — 다음 버전 diff 미리보기
- POST /policy-snapshot/apply               — 사용자 승인 후 적용
- POST /policy-snapshot/rollback/{version}  — 이전 버전으로 롤백

구현 위치: agents/policy_update_agent.py
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends

from reaction_backend.api.deps import CurrentUser
from reaction_backend.repositories.policy_snapshot_repo import (
    PolicySnapshotRepo,
    get_policy_snapshot_repo,
)
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.policy import PolicySnapshotResponse

router = APIRouter(prefix="/policy-snapshot", tags=["policy"])

PolicySnapshotRepoDep = Annotated[PolicySnapshotRepo, Depends(get_policy_snapshot_repo)]


@router.get("/current")
async def get_current_policy(
    user: CurrentUser,
    repo: PolicySnapshotRepoDep,
) -> PolicySnapshotResponse:
    """현재 활성 PolicySnapshot (#83) — 없으면 404 (FE 는 카운트-only 폴백 유지)."""
    snapshot = await repo.get_active(user.id)
    if snapshot is None:
        raise ApiError(
            ErrorCode.POLICY_NOT_FOUND,
            "아직 활성 정책 스냅샷이 없어요.",
            http_status=HTTPStatus.NOT_FOUND,
        )
    return PolicySnapshotResponse(
        version=snapshot.version,
        source=snapshot.source,
        behavioral_profile=snapshot.behavioral_profile,
        execution_constraints=snapshot.execution_constraints,
        interaction_style=snapshot.interaction_style,
        recovery_policy=snapshot.recovery_policy,
        reason_for_update=snapshot.reason_for_update,
        valid_from=snapshot.valid_from,
    )
