"""Recovery — S19, S20. if-then 코핑 플랜 (Issue #20-A 수직 슬라이스).

흐름 (Orchestrator 2 — Recovery):
  DETECTING → DIAGNOSING → COACHING → HITL → UPDATING → SAVING

핵심 결정 (AGENTS.md §1):
- UX 4 그룹 (DOWNSCOPE / RESCHEDULE / CARRY_OVER / PARK) — 같은 그룹 동시 노출 1카드.
- 내부 9 전략은 `recovery_strategy_catalog` 기준, 통계/감사용 보존.
- 8초 안에 LLM 응답 못 받으면 heuristic fallback (PRD §9) — 룰 선택 + 카탈로그 템플릿.
- 원본 `action_item.status` (FAILED 등)는 절대 변경 X — Resilience 지표 전제.
- AI 출력 = Draft Layer (`is_draft=True`) → `/recovery/decisions` 에서만 확정.

#20-A 구현 범위:
- POST /recovery/proposals/generate — 룰 선택(orchestrator.recovery) + LLM personalize
  (`recovery/if_then_proposal`, fallback 시 템플릿) → recovery_attempts(pending) INSERT
- POST /recovery/decisions — 수락/스킵 저장 (Idempotency 미들웨어 §1.7).
  수락 그룹이 DOWNSCOPE/CARRY_OVER 면 새 ActionItem(source=recovery_*) 생성.

#20-B 구현 범위 (S20 replan):
- GET /replan/{executionId} — 수락한 회복의 before/after diff (Draft Layer 프리뷰).
- POST /replan/{executionId}/approve — 회복 ActionItem 을 scheduled_block(source=recovery)
  으로 배치 (Idempotency §1.7, 멱등 재배치 방지). 원본 action_item.status 불변.
  재배치 대상은 새 ActionItem 을 만든 DOWNSCOPE/CARRY_OVER 뿐 (그 외 RECOVERY_NO_REPLAN).
"""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.recovery_attempt import (
    ADOPTED_DECISION_VALUES,
    RecoveryAttempt,
)
from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator.recovery import (
    first_matching_tag,
    recovery_target_date,
    recovery_unit_minutes,
    render_template,
    select_strategies,
    shift_to_recovery_day,
)
from reaction_backend.repositories.action_item_repo import (
    ActionItemRepo,
    get_action_item_repo,
)
from reaction_backend.repositories.recovery_repo import RecoveryRepo, get_recovery_repo
from reaction_backend.repositories.scheduled_block_repo import (
    ScheduledBlockRepo,
    get_scheduled_block_repo,
)
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.recovery import (
    RecoveryCard,
    RecoveryDecisionRequest,
    RecoveryDecisionResponse,
    RecoveryGenerateRequest,
    RecoveryProposalLLM,
    RecoveryProposalsResponse,
    ReplanApproveResponse,
    ReplanBlock,
    ReplanDiffResponse,
)

router = APIRouter(tags=["recovery"])

# 응답의 aiSource 리터럴 (DraftMixin 과 같은 집합 — 계약 동결).
AiSource = Literal["llm", "rule"]

_EXEC_PREFIX = "exec_"
_ATTEMPT_PREFIX = "rec_"
_ACTION_PREFIX = "action_"
_BLOCK_PREFIX = "block_"

# 회복 대상 completion_status — 실패/부분완료만 (DevBaseline: 21시 회고 흐름에서 호출)
_ELIGIBLE_STATUSES = ("failed", "partial_done")

# 수락 시 새 ActionItem 을 만드는 그룹 (없는 그룹: RESCHEDULE / PARK — §5.16)
_GROUP_TO_SOURCE = {
    "DOWNSCOPE": "recovery_downscope",
    "CARRY_OVER": "recovery_carryover",
}

RecoveryRepoDep = Annotated[RecoveryRepo, Depends(get_recovery_repo)]
ActionRepoDep = Annotated[ActionItemRepo, Depends(get_action_item_repo)]
BlockRepoDep = Annotated[ScheduledBlockRepo, Depends(get_scheduled_block_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]

# replan 으로 만드는 블록의 출처 (DB 설계서 §5.10 block_source)
_REPLAN_BLOCK_SOURCE = "recovery"


def _parse_id(raw: str, prefix: str, error: ApiError) -> UUID:
    if not raw.startswith(prefix):
        raise error
    try:
        return UUID(raw[len(prefix) :])
    except ValueError as e:
        raise error from e


def _execution_not_found() -> ApiError:
    return ApiError(
        ErrorCode.RECOVERY_EXECUTION_NOT_FOUND,
        "해당 실행 기록을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _attempt_not_found() -> ApiError:
    return ApiError(
        ErrorCode.RECOVERY_ATTEMPT_NOT_FOUND,
        "해당 회복 카드를 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _ai_source(fell_back: bool) -> AiSource:
    """응답의 `aiSource` — 룰 폴백이었으면 'rule', LLM 문구가 반영됐으면 'llm'.

    generate 응답(신규/pending 재사용)과 replan diff 세 곳이 같은 판정을 해야 한다.
    리터럴 값은 계약 동결(api-contract §12)이라 문자열 자체는 바꾸지 않는다.
    """
    return "rule" if fell_back else "llm"


def _to_card(attempt: RecoveryAttempt, strategy: RecoveryStrategyCatalog | None) -> RecoveryCard:
    return RecoveryCard(
        attempt_id=f"{_ATTEMPT_PREFIX}{attempt.id}",
        option_group=attempt.recovery_option_group,  # type: ignore[arg-type]
        strategy_type=attempt.recovery_strategy_type,
        label_ko=strategy.label_ko if strategy is not None else attempt.recovery_strategy_type,
        suggested_action_text=attempt.suggested_action_text or "",
        min_recovery_unit_minutes=(
            strategy.min_recovery_unit_minutes if strategy is not None else 5
        ),
        allow_rest_mode=strategy.allow_rest_mode if strategy is not None else False,
        trigger_tag=attempt.trigger_tag,
    )


async def _get_execution_or_404(
    user_id: UUID, raw_execution_id: str, repo: RecoveryRepo
) -> ExecutionEvent:
    execution_id = _parse_id(raw_execution_id, _EXEC_PREFIX, _execution_not_found())
    execution = await repo.get_execution(user_id, execution_id)
    if execution is None:
        raise _execution_not_found()
    return execution


@router.post("/recovery/proposals/generate", status_code=status.HTTP_201_CREATED)
async def generate_recovery_proposals(
    body: RecoveryGenerateRequest,
    user: CurrentUser,
    repo: RecoveryRepoDep,
    action_repo: ActionRepoDep,
    session: SessionDep,
) -> RecoveryProposalsResponse:
    """실패 컨텍스트 기반 회복 옵션 2~4개 생성 (LLM thinking 0 + ≤ 12s, 룰 fallback — ADR-0003 addendum).

    이미 pending 카드가 있으면 재생성하지 않고 그대로 반환한다 (중복 INSERT 방지).
    """
    execution = await _get_execution_or_404(user.id, body.execution_id, repo)
    if execution.completion_status not in _ELIGIBLE_STATUSES:
        raise ApiError(
            ErrorCode.RECOVERY_NOT_ELIGIBLE,
            "완료되지 않은(실패/부분완료) 실행에만 회복 카드를 만들 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="executionId",
        )

    strategies = await repo.list_active_strategies()
    catalog = {s.strategy_type: s for s in strategies}

    # 멱등 — pending 카드가 이미 있으면 그대로 반환 (재호출/새로고침 안전)
    existing = await repo.list_attempts(user.id, execution.id)
    pending = [a for a in existing if a.user_decision == "pending"]
    if pending:
        return RecoveryProposalsResponse(
            execution_id=body.execution_id,
            cards=[_to_card(a, catalog.get(a.recovery_strategy_type)) for a in pending],
            ai_source=_ai_source(all(a.llm_fallback_used for a in pending)),
        )
    if existing:
        # 결정이 끝난 실행 — pending 이 0건이라 위 가드를 그냥 통과해 **두 번째 카드 세트**가
        # INSERT 되던 구멍. 그 결과 `/recovery/decisions` 의 409 가 무력화되고(같은 실패에
        # 회복 ActionItem 2개), `_accepted_replan_attempt` 는 created_at 오름차순에서 처음
        # 만난 채택 카드를 잡으므로 replan 은 **옛 결정**에 고정된다 — 사용자가 다시 고른
        # 최신 회복은 화면에도 안 뜨고 블록도 영영 안 생긴다.
        # decisions 와 같은 에러 코드로 상태 머신을 한 곳에서 닫는다 (api-contract §12).
        raise ApiError(
            ErrorCode.RECOVERY_ALREADY_DECIDED,
            "이 실행의 회복 카드는 이미 결정됐어요.",
            http_status=HTTPStatus.CONFLICT,
        )

    failure_tags = await repo.list_failure_tag_codes(execution.id)
    selected = select_strategies(failure_tags, strategies)
    if not selected:
        raise ApiError(
            ErrorCode.RECOVERY_NO_PROPOSAL,
            "지금 제안할 수 있는 회복 카드가 없어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    # 템플릿 변수 — 원본 카드 제목 기반 (없어도 동작)
    action = await action_repo.get_by_id(user.id, execution.action_item_id)
    action_title = action.title if action is not None else ""
    variables = {
        "first_step": (action.first_step or action_title) if action is not None else "",
        "suspended_step": action_title,
    }
    texts = {s.strategy_type: render_template(s.if_then_template, variables) for s in selected}

    # LLM personalize — 룰이 고른 **선두 카드**의 if-then 문구를 이 사용자 맥락에 맞게 다듬는다.
    # 전략 선택은 룰이 이미 끝냈으므로, LLM 에 선두 전략(label/group/template)을 넘겨 "그 전략을
    # personalize"하게 한다(새 전략을 고르지 않음). 실패 시 카탈로그 템플릿 그대로 (PRD §9).
    top = selected[0]
    result = await aiClient.run(
        module="recovery",
        schema=RecoveryProposalLLM,
        prompt_id="recovery/if_then_proposal",
        fallback=lambda: RecoveryProposalLLM(
            strategy_code=top.strategy_type,
            if_clause="",
            then_clause=texts[top.strategy_type],
            rationale="",
        ),
        # 회복 personalize 는 템플릿 한 문장을 맥락에 맞게 다듬는 가벼운 작업이라 thinking 불필요.
        # thinking 을 끄지 않으면 상위 모델(flash-latest)이 SDK 기본 추론으로 8s 를 넘겨 매번
        # timeout→룰 폴백됐다(회복 카드가 항상 템플릿). thinking 0 으로 빠르게 + timeout 여유.
        thinking_budget=0,
        timeout=12.0,
        variables={
            "failure_type": ", ".join(failure_tags) if failure_tags else "UNKNOWN",
            "confidence": "n/a",
            "interruption_summary": "없음",
            "context_summary": f"실행 카드: {action_title} / 결과: {execution.completion_status}",
            "strategy_label": top.label_ko,
            "strategy_group": top.option_group,
            "base_template": texts[top.strategy_type],
        },
        user_id=user.id,
        session=session,
        tone_mode=user.tone_mode,
    )
    # 선두 카드에 personalize 적용. LLM 이 '선두 전략을 다듬어라'는 지시를 받으므로
    # strategy_code 일치 여부로 게이트하지 않는다 — 과거엔 LLM 이 generic code("downscope")를
    # 반환해 선택 전략키(NANO_STEP 등)와 항상 불일치 → Gemini 문구가 통째로 폐기되던 버그.
    if not result.fell_back:
        personalized = " ".join(
            part for part in (result.value.if_clause, result.value.then_clause) if part
        ).strip()
        if personalized:
            texts[top.strategy_type] = personalized

    attempts = [
        await repo.create_attempt(
            user_id=user.id,
            execution_id=execution.id,
            option_group=s.option_group,
            strategy_type=s.strategy_type,
            suggested_action_text=texts[s.strategy_type],
            trigger_tag=first_matching_tag(failure_tags, s),
            llm_fallback_used=result.fell_back,
        )
        for s in selected
    ]
    await session.commit()

    return RecoveryProposalsResponse(
        execution_id=body.execution_id,
        cards=[_to_card(a, catalog.get(a.recovery_strategy_type)) for a in attempts],
        ai_source=_ai_source(result.fell_back),
    )


def _validated_target(
    body: RecoveryDecisionRequest, pending: list[RecoveryAttempt], edited_text: str
) -> RecoveryAttempt:
    """수락 대상 카드를 찾고 채택 관련 검증을 **전부** 끝낸다 (부수효과 0).

    호출자가 이 함수를 첫 대입보다 앞에서 부르는 한, 422/404 가 나도 세션에 부분 변경이
    남지 않는다 — 그게 이 함수가 순수한 이유다.
    """
    if body.accepted_attempt_id is None:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "수락할 카드(acceptedAttemptId)를 알려주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="acceptedAttemptId",
        )
    attempt_id = _parse_id(body.accepted_attempt_id, _ATTEMPT_PREFIX, _attempt_not_found())
    target = next((a for a in pending if a.id == attempt_id), None)
    if target is None:
        raise _attempt_not_found()

    if body.decision == "edited":
        if not edited_text:
            raise ApiError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                "고친 문구(editedActionText)를 알려주세요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="editedActionText",
            )
        if target.recovery_option_group not in _GROUP_TO_SOURCE:
            # 이 그룹은 새 카드를 만들지 않아 문구를 담을 곳이 없다. 계약상 이들의 조정은
            # 문구가 아니라 시간이고, 그 경로는 S15 주간 편집기(PATCH blocks)다.
            raise ApiError(
                ErrorCode.RECOVERY_EDIT_NOT_SUPPORTED,
                "이 회복은 문구 대신 시간을 조정해요. 주간 편집기에서 옮겨 주세요.",
                http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
                field="editedActionText",
            )
    return target


def _adopt(
    target: RecoveryAttempt, decision: str, decision_reason: str | None, decided_at: datetime
) -> str:
    """채택 스탬프. `recovery_started_at` = 결정 시각 (average_recovery_minutes 의 기점)."""
    target.user_decision = decision
    target.recovery_decided_at = decided_at
    target.recovery_started_at = decided_at
    target.decision_reason = decision_reason
    return f"{_ATTEMPT_PREFIX}{target.id}"


def _reject_siblings(
    pending: list[RecoveryAttempt], target: RecoveryAttempt, decided_at: datetime
) -> list[str]:
    """수락 카드를 뺀 나머지 pending 을 rejected 로 — 같은 그룹 동시 노출 1카드 규칙의 뒷면."""
    rejected: list[str] = []
    for sibling in pending:
        if sibling.id == target.id:
            continue
        sibling.user_decision = "rejected"
        sibling.recovery_decided_at = decided_at
        rejected.append(f"{_ATTEMPT_PREFIX}{sibling.id}")
    return rejected


def _skip_all(
    pending: list[RecoveryAttempt], decision_reason: str | None, decided_at: datetime
) -> list[str]:
    """'오늘은 쉬기' — 모든 pending 카드를 skipped 로."""
    skipped: list[str] = []
    for sibling in pending:
        sibling.user_decision = "skipped"
        sibling.recovery_decided_at = decided_at
        sibling.decision_reason = decision_reason
        skipped.append(f"{_ATTEMPT_PREFIX}{sibling.id}")
    return skipped


async def _create_recovery_action(
    *,
    user_id: UUID,
    execution: ExecutionEvent,
    target: RecoveryAttempt,
    source: str,
    edited_text: str,
    decided_at: datetime,
    repo: RecoveryRepo,
    action_repo: ActionItemRepo,
) -> UUID:
    """DOWNSCOPE/CARRY_OVER 수락 → 회복 ActionItem 생성. 반환: 새 카드 ID.

    원본 카드는 **읽기만** 한다 (category 상속). `action_item.status` 는 건드리지 않는다
    — AGENTS.md §2, Resilience 지표의 전제.
    """
    original = await action_repo.get_by_id(user_id, execution.action_item_id)
    strategy = await repo.get_strategy(target.recovery_strategy_type)
    new_action = await action_repo.create_from_recovery(
        user_id=user_id,
        parent_action_item_id=execution.action_item_id,
        # 편집 수락이면 사용자 문구가 카드 제목이 된다. `target.suggested_action_text`
        # (AI 원문)는 그대로 둔다 — 덮어쓰면 "얼마나 고쳐 썼나"를 영영 못 잰다.
        title=edited_text or (target.suggested_action_text or "회복 액션")[:300],
        category=original.category if original is not None else "other",
        source=source,
        target_date=recovery_target_date(decided_at.date(), target.recovery_option_group),
        estimated_minutes=recovery_unit_minutes(
            strategy.min_recovery_unit_minutes if strategy is not None else None
        ),
    )
    return new_action.id


@router.post("/recovery/decisions")
async def decide_recovery(
    body: RecoveryDecisionRequest,
    user: CurrentUser,
    repo: RecoveryRepoDep,
    action_repo: ActionRepoDep,
    session: SessionDep,
) -> RecoveryDecisionResponse:
    """사용자 선택 저장 (Idempotency-Key 필수 — §1.7 미들웨어 enforce).

    - `accepted` → 선택 카드 accepted, 나머지 pending 은 rejected.
      그룹이 DOWNSCOPE/CARRY_OVER 면 새 ActionItem(source=recovery_*) 생성 — 원본
      카드 status 는 변경하지 않는다 (혈통: parent_action_item_id).
    - `skipped` → 모든 pending 카드 skipped ("오늘은 쉬기").
    """
    execution = await _get_execution_or_404(user.id, body.execution_id, repo)
    attempts = await repo.list_attempts(user.id, execution.id)
    pending = [a for a in attempts if a.user_decision == "pending"]
    if not pending:
        raise ApiError(
            ErrorCode.RECOVERY_ALREADY_DECIDED,
            "이 실행의 회복 카드는 이미 결정됐어요.",
            http_status=HTTPStatus.CONFLICT,
        )

    decided_at = now_kst()
    accepted_id: str | None = None
    rejected_ids: list[str] = []
    skipped_ids: list[str] = []
    resulting_action_id: str | None = None

    edited_text = (body.edited_action_text or "").strip()
    if body.decision != "edited" and edited_text:
        # 조용히 무시하면 "고쳤는데 반영이 안 됨"을 숨긴다.
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "문구를 고쳐서 수락하려면 decision 을 'edited' 로 보내주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="editedActionText",
        )

    if body.decision in ADOPTED_DECISION_VALUES:
        # ⚠️ 검증이 **전부** 첫 대입보다 앞에 온다 — 그래서 422 가 나도 부분 변경이 없다.
        # 이 순서가 뒤집히면 "실패했는데 일부만 반영된" 상태가 커밋될 수 있다.
        target = _validated_target(body, pending, edited_text)

        accepted_id = _adopt(target, body.decision, body.decision_reason, decided_at)
        rejected_ids = _reject_siblings(pending, target, decided_at)

        source = _GROUP_TO_SOURCE.get(target.recovery_option_group)
        if source is not None:
            new_action_id = await _create_recovery_action(
                user_id=user.id,
                execution=execution,
                target=target,
                source=source,
                edited_text=edited_text,
                decided_at=decided_at,
                repo=repo,
                action_repo=action_repo,
            )
            target.resulting_action_item_id = new_action_id
            resulting_action_id = f"{_ACTION_PREFIX}{new_action_id}"
    else:  # skipped
        skipped_ids = _skip_all(pending, body.decision_reason, decided_at)

    await session.commit()

    return RecoveryDecisionResponse(
        execution_id=body.execution_id,
        accepted_attempt_id=accepted_id,
        rejected_attempt_ids=rejected_ids,
        skipped_attempt_ids=skipped_ids,
        resulting_action_item_id=resulting_action_id,
    )


def _no_replan() -> ApiError:
    return ApiError(
        ErrorCode.RECOVERY_NO_REPLAN,
        "재배치할 회복 일정이 없어요. 일정을 만드는 회복(범위 축소·내일로 이어가기)을 먼저 수락해 주세요.",
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


def _accepted_replan_attempt(attempts: list[RecoveryAttempt]) -> RecoveryAttempt:
    """일정 재배치 대상인 수락 카드 — 새 ActionItem(resulting_action_item_id) 이 있는 1건.

    수락이 없거나(skipped) 새 카드를 만들지 않는 그룹(RESCHEDULE/PARK)이면 422.
    """
    for attempt in attempts:
        if (
            attempt.user_decision in ADOPTED_DECISION_VALUES
            and attempt.resulting_action_item_id is not None
        ):
            return attempt
    raise _no_replan()


def _after_block_time(
    execution: ExecutionEvent,
    original: ActionItem,
    recovery_action: ActionItem,
    *,
    now: datetime,
) -> tuple[datetime, datetime]:
    """회복 카드 제안 시각 — ORM 객체에서 원시값을 꺼내 orchestrator 규칙에 넘긴다.

    계산(일 단위 시프트 + 과거 배치 보정)은 `orchestrator.recovery.shift_to_recovery_day` 에
    있다 — DB/요청 객체가 필요 없는 순수 규칙이라 그쪽에서 단위 테스트로 고정된다.
    """
    return shift_to_recovery_day(
        execution.plan_start_at,
        original_target_date=original.target_date,
        recovery_target_date=recovery_action.target_date,
        estimated_minutes=recovery_action.estimated_minutes,
        now=now,
    )


async def _existing_replan_block(
    user_id: UUID, recovery_action_id: UUID, block_repo: ScheduledBlockRepo
) -> ScheduledBlock | None:
    """회복 카드에 이미 배치된 블록 — GET 의 `alreadyApproved` 와 approve 멱등의 단일 소스.

    **소스로 거르지 않는다.** 예전엔 `source == 'recovery'` 인 블록만 기존 배치로 인정했는데,
    같은 회복 카드의 블록 소스는 다른 경로에서 바뀌거나 다른 값으로 생긴다:
    S15 블록 이동(`PATCH /plans/{planId}/blocks/{blockId}`)이 `source='user_edit'` 로
    덮어쓰고, 주간 forward 재계획은 `source='ai_plan'` 으로 만든다. 둘 다 소스 필터에
    안 걸려 `alreadyApproved=false` 가 되고(FE 에 '배치하기' CTA 재노출), approve 가
    블록을 하나 더 INSERT 했다 — `create_block` 은 겹침 검사도 하지 않는다.

    형제 경로가 이미 같은 판정을 소스 무관으로 한다 (`planning.py` 주간 승인: "백로그인데
    그새 활성 블록이 생겼으면 중복 방지로 skip"). `list_by_action_item` 은 cancelled 를
    제외하고 start_at 오름차순이라, 첫 건을 고르는 것이 결정적이다.
    """
    blocks = await block_repo.list_by_action_item(user_id, recovery_action_id)
    return blocks[0] if blocks else None


async def _load_replan_context(
    user_id: UUID,
    raw_execution_id: str,
    repo: RecoveryRepo,
    action_repo: ActionItemRepo,
) -> tuple[ExecutionEvent, RecoveryAttempt, ActionItem, ActionItem]:
    """(execution, 수락카드, 원본 ActionItem, 회복 ActionItem) 를 한 번에 로드."""
    execution = await _get_execution_or_404(user_id, raw_execution_id, repo)
    attempts = await repo.list_attempts(user_id, execution.id)
    attempt = _accepted_replan_attempt(attempts)

    original = await action_repo.get_by_id(user_id, execution.action_item_id)
    if original is None:
        raise _execution_not_found()
    assert attempt.resulting_action_item_id is not None  # _accepted_replan_attempt 보장
    recovery_action = await action_repo.get_by_id(user_id, attempt.resulting_action_item_id)
    if recovery_action is None:
        raise _no_replan()
    return execution, attempt, original, recovery_action


@router.get("/replan/{execution_id}")
async def get_replan_diff(
    execution_id: str,
    user: CurrentUser,
    repo: RecoveryRepoDep,
    action_repo: ActionRepoDep,
    block_repo: BlockRepoDep,
) -> ReplanDiffResponse:
    """S20 before/after diff (Draft Layer) — 수락한 회복의 일정 변화 프리뷰 (#20-B).

    before = 원본 실패 카드의 계획 시각, after = 회복 카드의 제안 시각.
    이미 approve 로 블록이 배치됐으면 `alreadyApproved=true`.

    **승인 후에는 실제 배치된 블록이 단일 진실**이다 — 과거 배치 보정(#174)이 `now` 에
    의존하므로, 승인 뒤에도 매번 재계산하면 프리뷰가 실제 블록과 어긋난다. 미승인 프리뷰는
    정의상 "지금 승인하면 여기"라 조회 시각에 따라 달라진다.
    """
    execution, attempt, original, recovery_action = await _load_replan_context(
        user.id, execution_id, repo, action_repo
    )
    block = await _existing_replan_block(user.id, recovery_action.id, block_repo)
    already_approved = block is not None
    if block is not None:
        start_at, end_at = block.start_at, block.end_at
    else:
        start_at, end_at = _after_block_time(execution, original, recovery_action, now=now_kst())

    return ReplanDiffResponse(
        execution_id=execution_id,
        option_group=attempt.recovery_option_group,  # type: ignore[arg-type]
        before=ReplanBlock(
            action_item_id=f"{_ACTION_PREFIX}{original.id}",
            title=original.title,
            target_date=original.target_date,
            start_at=execution.plan_start_at,
            end_at=execution.plan_end_at,
            estimated_minutes=original.estimated_minutes,
        ),
        after=ReplanBlock(
            action_item_id=f"{_ACTION_PREFIX}{recovery_action.id}",
            title=recovery_action.title,
            target_date=recovery_action.target_date,
            start_at=start_at,
            end_at=end_at,
            estimated_minutes=recovery_action.estimated_minutes,
        ),
        ai_source=_ai_source(attempt.llm_fallback_used),
        already_approved=already_approved,
    )


@router.post("/replan/{execution_id}/approve")
async def approve_replan(
    execution_id: str,
    user: CurrentUser,
    repo: RecoveryRepoDep,
    action_repo: ActionRepoDep,
    block_repo: BlockRepoDep,
    session: SessionDep,
) -> ReplanApproveResponse:
    """S20 최종 적용 (Idempotency-Key 필수 — §1.7 미들웨어 enforce).

    회복 ActionItem 을 `scheduled_block`(source='recovery') 으로 배치한다. 멱등:
    이미 배치돼 있으면 같은 block 을 반환(중복 INSERT 방지). 원본 `action_item.status`
    는 변경하지 않는다 (AGENTS.md §2 — Resilience 지표 전제).
    """
    execution, _attempt, original, recovery_action = await _load_replan_context(
        user.id, execution_id, repo, action_repo
    )

    block = await _existing_replan_block(user.id, recovery_action.id, block_repo)
    if block is None:
        start_at, end_at = _after_block_time(execution, original, recovery_action, now=now_kst())
        block = await block_repo.create_block(
            user_id=user.id,
            action_item_id=recovery_action.id,
            start_at=start_at,
            end_at=end_at,
            source=_REPLAN_BLOCK_SOURCE,
        )
        await session.commit()

    return ReplanApproveResponse(
        execution_id=execution_id,
        scheduled_block_id=f"{_BLOCK_PREFIX}{block.id}",
        action_item_id=f"{_ACTION_PREFIX}{recovery_action.id}",
        start_at=block.start_at,
        end_at=block.end_at,
    )
