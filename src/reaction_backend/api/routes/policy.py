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

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/policy-snapshot", tags=["policy"])


@router.get("/current", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_current_policy() -> None:
    """현재 활성 PolicySnapshot."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §14 — to be implemented in a follow-up.",
    )
