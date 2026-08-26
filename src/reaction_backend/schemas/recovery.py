"""Recovery 도메인 스키마 (api-contract §12) — S19 Recovery Coach / S20 Replan.

UX 4 그룹 (DOWNSCOPE / RESCHEDULE / CARRY_OVER / PARK) 카드를 Draft Layer 로 반환하고,
사용자 결정(`/recovery/decisions`)에서만 `is_draft=False` 가 된다 (ADR-0005 §7.2).

`RecoveryProposalsResponse.recovery_mode`(#328) — 동일 목표 반복 실패/거절이면
`"goal_renegotiation"`, 그 외엔 `"standard"`. 카드 자체의 구조(그룹·필드)는 그대로다 —
재협상도 회복 카드 3장일 뿐이라 별도 스키마를 안 만든다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, DraftMixin, KstDatetime

RecoveryOptionGroup = Literal["DOWNSCOPE", "RESCHEDULE", "CARRY_OVER", "PARK"]

RecoveryDecision = Literal["accepted", "edited", "skipped"]

RecoveryMode = Literal["standard", "goal_renegotiation"]
"""일반 회복(4그룹 중 태그 매칭) vs 목표 재협상(#328, 근거 대장 §5.2 L3) 구분 — FE 가
카드 개수·제목으로 모드를 추론하지 않도록 명시 필드로 낸다. `goal_renegotiation` 이면
`cards` 는 항상 DOWNSCOPE/RESCHEDULE/PARK 각 1장(카탈로그가 세 그룹 모두 활성이면)."""


class RecoveryProposalLLM(CamelModel):
    """LLM Structured Output — `aiClient.run("recovery/if_then_proposal")` 응답 schema.

    프롬프트(`prompts/recovery/if_then_proposal.v1.md`/`v2.md`)의 JSON 형식과 1:1.
    새 버전을 올릴 땐 이 schema 와 출력 형식을 맞출 것
    (`tests/prompts/test_recovery_prompts.py` 가 변수 계약을 강제한다).
    fallback 룰도 같은 schema 로 반환 (Tool Executor 가 강제 검증).

    v3(`obstacle`/`coping_clause`/`acknowledgment` 포함)는 `RecoveryProposalLLMv3` 를
    따로 쓴다 — 한때 이 필드들을 여기 직접 얹었더니(#272), `aiClient.run(schema=...)`
    가 v1/v2 호출에도 **같은 확장 스키마**를 Gemini 에 넘겨 프롬프트가 요청하지도 않은
    필드를 Gemini 가 알아서 채워버렸다(L1-1 실 dispatch 중 실측 — v2 호출인데
    `coping_clause`/`acknowledgment` 에 실제 문장이 채워져 나옴). v1/v2 는 그 필드
    자체를 "구조적으로 안 가진 것"이 루브릭(rubric-v1.md §1 축③/§5)의 전제라, 스키마가
    새어 들어가면 그 전제가 깨진다. 프로덕션(`routes/recovery.py`, 당시 `_PROMPT_ID`=v2
    고정)도 같은 스키마를 쓰고 있었으므로 이건 실험만이 아니라 **불필요한 필드를 매
    호출마다 생성하던 프로덕션 낭비이기도 했다.** (이후 v3 가 AVOIDANCE 태그 조건부로
    승격되면서 `RecoveryProposalLLMv3` 가 그 경로의 실제 스키마가 됐다 — 아래 참고.)
    """

    strategy_code: str
    if_clause: str
    then_clause: str
    rationale: str
    estimated_workload_change_minutes: int = 0


class RecoveryProposalLLMv3(RecoveryProposalLLM):
    """v3 전용 — `obstacle`/`coping_clause`/`acknowledgment` 추가(근거 대장 §4 S5/S1).

    v3 프롬프트(`if_then_proposal.v3.md`)를 호출할 때만 이 schema 를 쓴다 —
    `scripts/l1_1_generate.py`(오프라인 A/B) 와, 프로덕션 `routes/recovery.py`
    (`_PROMPT_ID_V3`, **AVOIDANCE 태그가 있을 때만**) 둘 다. 그 외 태그는 여전히
    `_PROMPT_ID_V2` + `RecoveryProposalLLM`(이 클래스 아님) — L1-1 승률(1.000)이
    judge–human κ=0.482 로 보조 지표 강등(#278)된 데다 실 도그푸딩 검증도 없어,
    노출 범위를 이 태그 하나로 좁혀 승격했다.
    """

    obstacle: str = ""
    coping_clause: str = ""
    acknowledgment: str = ""


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
    # v3(AVOIDANCE 전용) personalize 로 채워진 배치의 **선두 카드에만** 값이 있다 — 나머지
    # 셋 다 null 이면 v2 로 만들어졌거나 룰 폴백된 카드(routes/recovery.py 참고).
    obstacle: str | None = None
    coping_clause: str | None = None
    acknowledgment: str | None = None


class RecoveryGenerateRequest(CamelModel):
    """POST /recovery/proposals/generate 요청."""

    execution_id: str


class RecoveryProposalsResponse(DraftMixin):
    """후보 2~4장 — Draft Layer (`is_draft=True` 강제, 라우터 책임)."""

    execution_id: str
    cards: list[RecoveryCard]
    recovery_mode: RecoveryMode = "standard"


class RecoveryDecisionRequest(CamelModel):
    """POST /recovery/decisions 요청 (Idempotency-Key 필수, §1.7).

    - `decision="accepted"` → `accepted_attempt_id` 필수, 나머지 pending 카드는 rejected.
    - `decision="edited"` → `accepted_attempt_id` + `edited_action_text` 필수. 부수효과는
      accepted 와 같고(형제 rejected, 새 카드 생성) **새 카드 title 만 사용자 문구**가 된다.
      AI 원문(`suggested_action_text`)은 보존한다 — "얼마나 고쳐 썼나"가 AI 품질 지표다.
      새 카드를 만들지 않는 그룹(RESCHEDULE/PARK)은 문구를 담을 곳이 없어 422.
    - `decision="skipped"` → 모든 pending 카드 skipped ("오늘은 쉬기").

    `re_engagement_anchor_at`(#327, FE #221): PARK/CARRY_OVER 수락에만 유효(그 외 그룹에
    보내면 422 — 조용히 버리면 사용자가 지정한 시점이 사라진 걸 못 알아챈다). 생략하면
    서버가 전략별 기본값(`orchestrator.recovery.re_engagement_anchor_at`)을 계산한다.
    시간대 정보(예: `+09:00`)를 포함한 ISO 8601 이어야 한다.
    """

    execution_id: str
    decision: RecoveryDecision
    accepted_attempt_id: str | None = None
    edited_action_text: str | None = Field(default=None, max_length=300)
    decision_reason: str | None = Field(default=None, max_length=200)
    re_engagement_anchor_at: datetime | None = None


class RecoveryDecisionResponse(CamelModel):
    """결정 결과 — 명시 승인 endpoint 이므로 `is_draft=False` (ADR-0005 §7.2)."""

    execution_id: str
    accepted_attempt_id: str | None
    rejected_attempt_ids: list[str]
    skipped_attempt_ids: list[str]
    resulting_action_item_id: str | None
    # PARK/CARRY_OVER 수락일 때만 값 있음(명시값 또는 서버 기본값 확정 결과) — #327.
    re_engagement_anchor_at: KstDatetime | None = None
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
