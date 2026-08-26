"""Policy Snapshot 스키마 (api-contract §14) — 학습 루프 산출물.

4 영역(behavioral_profile / execution_constraints / interaction_style / recovery_policy)은
policy_snapshots JSONB 컬럼을 그대로 노출한다.

#168 로 `current` 외 4개(history/preview-update/apply/rollback)가 실제로 생겼다 — 그전에는
계약 문서에만 있고 라우트가 없었다(계약-구현 불일치).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, DraftMixin, KstDatetime


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


class PolicyHistoryItem(CamelModel):
    """GET /policy-snapshot/history 항목 — 4 영역 payload 는 빼고 메타만.

    이력 화면은 "언제 무엇 때문에 바뀌었나" 를 보는 자리라, 버전마다 JSONB 4개를 전부
    실어 보내면 응답만 커지고 화면은 안 쓴다. 특정 버전의 내용이 필요하면 롤백 미리보기
    (`preview-update` 는 다음 버전용이므로) 대신 `history` 로 고르고 `rollback` 한다.
    """

    version: int
    source: str  # rule | llm | user_manual
    is_active: bool
    reason_for_update: str | None
    valid_from: KstDatetime
    valid_to: KstDatetime | None


class PolicyHistoryResponse(CamelModel):
    items: list[PolicyHistoryItem]


class PolicyChangeItem(CamelModel):
    """승인 화면에 보여줄 변경 한 줄 — **근거를 숫자로** 담는다."""

    area: str
    """behavioralProfile | executionConstraints | interactionStyle | recoveryPolicy"""
    field: str
    before: Any
    after: Any
    why: str


class PolicyPreviewResponse(DraftMixin):
    """POST /policy-snapshot/preview-update — 다음 버전 후보 (Draft Layer).

    **아무것도 저장하지 않는다** (AGENTS §1 자동 적용 금지). `isDraft=true` 로 내려가고,
    사용자가 `POST /policy-snapshot/apply` 를 눌러야 INSERT 된다.

    `changes` 가 비면 이번 주엔 바꿀 게 없다는 뜻이다 — FE 는 그때 [적용] 을 비활성화하면
    된다(억지로 새 버전을 만들 이유가 없다).
    """

    base_version: int | None
    """현재 활성 버전. 스냅샷이 하나도 없으면 null (이 미리보기가 v1 후보다)."""
    next_version: int
    changes: list[PolicyChangeItem]
    reason_for_update: str | None
    behavioral_profile: dict[str, Any]
    execution_constraints: dict[str, Any]
    interaction_style: dict[str, Any]
    recovery_policy: dict[str, Any]


class PolicyApplyRequest(CamelModel):
    """POST /policy-snapshot/apply — 사용자가 승인(또는 수정)한 4 영역.

    `preview-update` 응답을 그대로 되보내면 룰 그대로 적용이고, 값을 고쳐 보내면 사용자
    수정본이 적용된다 — `source` 로 어느 쪽인지 FE 가 알려준다(`/recovery/decisions` 의
    accepted/edited 와 같은 관례: 사용자가 고쳤는지는 화면이 가장 잘 안다).
    """

    behavioral_profile: dict[str, Any]
    execution_constraints: dict[str, Any]
    interaction_style: dict[str, Any]
    recovery_policy: dict[str, Any]
    source: Literal["rule", "user_manual"] = "rule"
    reason_for_update: str | None = Field(default=None, max_length=200)
