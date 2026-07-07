"""Policy Snapshot 스키마 (api-contract §14) — 학습 루프 산출물의 현재 활성 정책.

GET /policy-snapshot/current 응답. 4 영역(behavioral_profile / execution_constraints
/ interaction_style / recovery_policy)은 policy_snapshots JSONB 컬럼을 그대로 노출한다.
"""

from __future__ import annotations

from typing import Any

from reaction_backend.schemas.common import CamelModel, KstDatetime


class PolicySnapshotResponse(CamelModel):
    """GET /policy-snapshot/current — 현재 활성 PolicySnapshot (#83).

    활성 스냅샷이 없으면 라우트가 404(POLICY_NOT_FOUND) 를 낸다 — FE 는 카운트-only
    폴백을 유지한다.
    """

    version: int
    source: str  # rule | llm | user_manual
    behavioral_profile: dict[str, Any]
    execution_constraints: dict[str, Any]
    interaction_style: dict[str, Any]
    recovery_policy: dict[str, Any]
    reason_for_update: str | None
    valid_from: KstDatetime
