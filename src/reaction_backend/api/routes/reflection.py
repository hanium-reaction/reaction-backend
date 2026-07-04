"""Reflection — 저녁 회고 (S17, S18). "회복 골든 타임".

핵심 결정 (DevBaseline §1.4 잠금):
- 21시 일괄 회고만, 실패 직후 X
- 최대 3일 누적 (오늘+어제+그제). 3일 초과는 system_failure_reason='reflection_skipped'로 자동 만료
- Idempotency 24h 강제 ([모두 완료] 중복 탭 방지)

실패 사유 13종 enum: TIME_SHORTAGE / LOW_ENERGY / HARD_TO_START / PRIORITY_SHIFT
/ PLAN_TOO_BIG / FATIGUE / AMBIGUITY / CONFLICT / OVERRUN / AVOIDANCE / DISTRACTION
/ EMERGENCY / CONTEXT_LOSS — 최대 2개 선택, memo는 at-rest 암호화.

#19-B 구현:
- GET  /reflection/failure-tags            — 13종 마스터 (is_active=true)
- POST /reflection/failure-tags/{exec_id}  — 실패 사유 태깅 (0~2개, memo 암호화).
  failed/partial_done 실행만 허용, 재태깅은 409 (hard delete 회피 — AGENTS.md §2).
  태깅 후 Recovery 카드 생성(§12 `/recovery/proposals/generate`)으로 이어진다.

후속:
- GET  /reflection/pending — S17 진입 시 미체크 카드 조회 (3일 누적)
- POST /reflection/batch   — [모두 완료] 일괄 처리 (Idempotency-Key 필수)
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.session import get_db
from reaction_backend.repositories.action_item_repo import ActionItemRepo, get_action_item_repo
from reaction_backend.repositories.execution_repo import ExecutionRepo, get_execution_repo
from reaction_backend.safety.encryption import encrypt_memo
from reaction_backend.schemas.common import KST, now_kst, to_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.reflection import (
    FailureTagMaster,
    FailureTagRequest,
    FailureTagResponse,
    ReflectionPendingItem,
)

router = APIRouter(prefix="/reflection", tags=["reflection"])

_EXEC_PREFIX = "exec_"
_ACTION_PREFIX = "action_"

# 회고 누적 창 — 오늘+어제+그제 (DevBaseline §1.4). 초과분은 cron 이 reflection_skipped 로 만료.
_PENDING_WINDOW_DAYS = 3

# 태깅 대상 — 실패/부분완료만 (S18 은 미완료 카드에서만 진입)
_TAGGABLE_STATUSES = ("failed", "partial_done")

ExecutionRepoDep = Annotated[ExecutionRepo, Depends(get_execution_repo)]
ActionRepoDep = Annotated[ActionItemRepo, Depends(get_action_item_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


def _execution_not_found() -> ApiError:
    return ApiError(
        ErrorCode.TODAY_EXECUTION_NOT_FOUND,
        "해당 실행 기록을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _parse_execution_id(execution_id: str) -> UUID:
    if not execution_id.startswith(_EXEC_PREFIX):
        raise _execution_not_found()
    try:
        return UUID(execution_id[len(_EXEC_PREFIX) :])
    except ValueError as e:
        raise _execution_not_found() from e


@router.get("/pending")
async def list_pending_reflections(
    user: CurrentUser,
    repo: ExecutionRepoDep,
    action_repo: ActionRepoDep,
) -> list[ReflectionPendingItem]:
    """S17 저녁 회고 — 최근 3일(오늘+어제+그제) 미체크(in_progress) 실행 목록 (#83).

    시작만 하고 체크인하지 않은 실행을 소급 회고(POST /reflection/batch)하도록 모은다.
    아직 결과 미정이라 completionStatus 는 null.
    """
    today = now_kst().date()
    since = datetime.combine(today - timedelta(days=_PENDING_WINDOW_DAYS - 1), time.min, tzinfo=KST)
    executions = await repo.list_pending_reflection(user.id, since=since)

    items: list[ReflectionPendingItem] = []
    for execution in executions:
        action = await action_repo.get_by_id(user.id, execution.action_item_id)
        start = to_kst(execution.plan_start_at)
        items.append(
            ReflectionPendingItem(
                execution_id=f"{_EXEC_PREFIX}{execution.id}",
                action_item_id=f"{_ACTION_PREFIX}{execution.action_item_id}",
                title=action.title if action is not None else "(삭제된 카드)",
                scheduled_date=start.date(),
                scheduled_time=start.strftime("%H:%M"),
                completion_status=None,
            )
        )
    return items


@router.get("/failure-tags")
async def list_failure_tags(
    user: CurrentUser,
    repo: ExecutionRepoDep,
) -> list[FailureTagMaster]:
    """S18 칩 마스터 — 13종 (is_active=true, sort_order 순)."""
    tags = await repo.list_active_failure_tags()
    return [
        FailureTagMaster(
            tag_code=t.tag_code,
            label_ko=t.label_ko,
            description=t.description,
            sort_order=t.sort_order,
        )
        for t in tags
    ]


@router.post("/failure-tags/{execution_id}", status_code=status.HTTP_201_CREATED)
async def tag_failure_reasons(
    execution_id: str,
    body: FailureTagRequest,
    user: CurrentUser,
    repo: ExecutionRepoDep,
    session: SessionDep,
) -> FailureTagResponse:
    """실패 사유 태깅 (0~2개) + memo at-rest 암호화 (#19-B).

    이 태그가 Recovery 룰 엔진(§12)의 `primary_trigger_tags` 매칭 입력이 된다.
    """
    execution = await repo.get_by_id(user.id, _parse_execution_id(execution_id))
    if execution is None:
        raise _execution_not_found()
    if execution.completion_status not in _TAGGABLE_STATUSES:
        raise ApiError(
            ErrorCode.REFLECT_NOT_FAILED,
            "실패/부분완료 실행에만 실패 사유를 남길 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    if await repo.has_failure_tags(execution.id):
        raise ApiError(
            ErrorCode.REFLECT_ALREADY_TAGGED,
            "이 실행에는 이미 실패 사유가 기록되어 있어요.",
            http_status=HTTPStatus.CONFLICT,
        )

    # 13종 마스터 검증 (is_active 만) — 중복 코드 제거
    codes = list(dict.fromkeys(body.tag_codes))
    valid = {t.tag_code for t in await repo.list_active_failure_tags()}
    invalid = [code for code in codes if code not in valid]
    if invalid:
        raise ApiError(
            ErrorCode.REFLECT_INVALID_TAG,
            f"알 수 없는 실패 사유예요: {', '.join(invalid)}",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="tagCodes",
        )

    memo_encrypted = encrypt_memo(body.memo) if body.memo else None
    await repo.add_failure_tags(
        execution_id=execution.id,
        tag_codes=codes,
        memo_encrypted=memo_encrypted,
    )
    await session.commit()

    return FailureTagResponse(
        execution_id=execution_id,
        tag_codes=codes,
        has_memo=memo_encrypted is not None,
    )


@router.post("/batch", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def batch_reflect() -> None:
    """오늘+어제+그제 미체크 카드 일괄 처리. Idempotency-Key 헤더 필수."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §11 — to be implemented in a follow-up.",
    )
