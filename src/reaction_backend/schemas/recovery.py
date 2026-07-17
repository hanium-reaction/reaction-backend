"""Recovery 도메인 스키마 (api-contract §12) — S19 Recovery Coach / S20 Replan.

UX 4 그룹 (DOWNSCOPE / RESCHEDULE / CARRY_OVER / PARK) 카드를 Draft Layer 로 반환하고,
사용자 결정(`/recovery/decisions`)에서만 `is_draft=False` 가 된다 (ADR-0005 §7.2).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, DraftMixin, KstDatetime

RecoveryOptionGroup = Literal["DOWNSCOPE", "RESCHEDULE", "CARRY_OVER", "PARK"]

RecoveryDecision = Literal["accepted", "skipped"]


class RecoveryProposalLLM(CamelModel):
    """LLM Structured Output — `aiClient.run("recovery/if_then_proposal")` 응답 schema.

    프롬프트(`prompts/recovery/if_then_proposal.v*.md` — registry 가 latest 자동 선택)의
    JSON 형식과 1:1. 새 버전을 올릴 땐 이 schema 와 출력 형식을 맞출 것
    (`tests/prompts/test_recovery_prompts.py` 가 변수 계약을 강제한다).
    fallback 룰도 같은 schema 로 반환 (Tool Executor 가 강제 검증).
    """

    strategy_code: str
    if_clause: str
    then_clause: str
    rationale: str
    estimated_workload_change_minutes: int = 0


class RecoveryCard(CamelModel):
    """회복 옵션 카드 1장 — recovery_attempts 1행과 대응 (user_decision='pending')."""

    attempt_id: str
    option_group: RecoveryOptionGroup
    strategy_type: str
    label_ko: str
    suggested_action_text: str
    min_recovery_unit_minutes: int
    allow_rest_mode: bool
    trigger_tag: str | None


class RecoveryGenerateRequest(CamelModel):
    """POST /recovery/proposals/generate 요청."""

    execution_id: str


class RecoveryProposalsResponse(DraftMixin):
    """후보 2~4장 — Draft Layer (`is_draft=True` 강제, 라우터 책임)."""

    execution_id: str
    cards: list[RecoveryCard]


class RecoveryDecisionRequest(CamelModel):
    """POST /recovery/decisions 요청 (Idempotency-Key 필수, §1.7).

    - `decision="accepted"` → `accepted_attempt_id` 필수, 나머지 pending 카드는 rejected.
    - `decision="skipped"` → 모든 pending 카드 skipped ("오늘은 쉬기").
    """

    execution_id: str
    decision: RecoveryDecision
    accepted_attempt_id: str | None = None
    decision_reason: str | None = Field(default=None, max_length=200)


class RecoveryDecisionResponse(CamelModel):
    """결정 결과 — 명시 승인 endpoint 이므로 `is_draft=False` (ADR-0005 §7.2)."""

    execution_id: str
    accepted_attempt_id: str | None
    rejected_attempt_ids: list[str]
    skipped_attempt_ids: list[str]
    resulting_action_item_id: str | None
    is_draft: bool = False


class ReplanBlock(CamelModel):
    """Replan diff 의 한 면(before/after) — 카드 1장 + 시간 배치 1건.

    `start_at`/`end_at` 은 응답 시 KST(+09:00). before 는 원본 실패 카드의 계획 시각,
    after 는 회복 카드의 제안 시각(원본 시간대를 회복 target_date 로 그대로 이동).
    """

    action_item_id: str
    title: str
    target_date: date
    start_at: KstDatetime
    end_at: KstDatetime
    estimated_minutes: int


class ReplanDiffResponse(DraftMixin):
    """GET /replan/{executionId} — S20 before/after 프리뷰 (Draft Layer).

    승인 전까지 `is_draft=True`. `already_approved=True` 면 이미 approve 로 블록이
    배치된 상태(멱등 재조회).
    """

    execution_id: str
    option_group: RecoveryOptionGroup
    before: ReplanBlock
    after: ReplanBlock
    already_approved: bool = False


class ReplanApproveResponse(CamelModel):
    """POST /replan/{executionId}/approve — 최종 적용 (명시 승인 → `is_draft=False`).

    회복 ActionItem 을 `scheduled_block`(source=`recovery`) 으로 배치한다. 멱등:
    이미 배치돼 있으면 같은 block 을 반환한다. 원본 `action_item.status` 불변.
    """

    execution_id: str
    scheduled_block_id: str
    action_item_id: str
    start_at: KstDatetime
    end_at: KstDatetime
    is_draft: bool = False
