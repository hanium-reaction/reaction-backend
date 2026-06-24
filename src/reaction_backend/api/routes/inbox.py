"""Inbox — Life Inbox (S24) + Triage (S25) (api-contract §18).

Issue #22-B 실구현:
- `POST /inbox` — `aiClient.run("inbox/classify")` Sequential Parser + 룰 fallback + 암호화 저장
- `GET /inbox?status=` — list (raw_text 복호화 응답)
- `PATCH /inbox/{id}` — user_category override / status 변경
- `POST /inbox/{id}/convert-to-goal` — Goal 생성 + Maintain 한도 enforce + inbox.status=promoted
- `POST /inbox/{id}/convert-to-action` — ActionItem(`source=inbox`) 생성 + inbox.status=promoted
- `POST /inbox/{id}/archive` — soft delete (status=archived)

raw_text 는 `safety.encryption` AES-256-GCM (`raw_text_encrypted` 컬럼). 응답엔 복호화 평문.
LLM 호출은 `llm.aiClient` Tool Executor 경유 (AGENTS.md §2, ADR-0003 / ADR-0005 §4 단계 5 Sequential).
"""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.inbox_item import InboxItem as InboxItemModel
from reaction_backend.db.session import get_db
from reaction_backend.llm import aiClient
from reaction_backend.repositories.action_item_repo import (
    ActionItemRepo,
    get_action_item_repo,
)
from reaction_backend.repositories.goal_repo import GoalRepo, get_goal_repo
from reaction_backend.repositories.inbox_repo import InboxRepo, get_inbox_repo
from reaction_backend.safety.encryption import decrypt_inbox_text, encrypt_inbox_text
from reaction_backend.schemas.common import KST
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.inbox import (
    InboxCategory,
    InboxClassification,
    InboxCreateRequest,
    InboxItem,
    InboxUpdateRequest,
)

router = APIRouter(prefix="/inbox", tags=["inbox"])

_ID_PREFIX = "inbox_"
_MAINTAIN_LIMIT = 5  # convert-to-goal 기본 tier=maintain 한도 (DevBaseline §1.4)

# 룰 fallback 키워드 매칭 — LLM 실패 시 사용 (Tool Executor 가 자동 분기).
_KEYWORD_MAP: tuple[tuple[str, InboxCategory], ...] = (
    ("공부", "study"),
    ("강의", "study"),
    ("토익", "study"),
    ("시험", "study"),
    ("학습", "study"),
    ("프로젝트", "project"),
    ("캡스톤", "project"),
    ("과제", "project"),
    ("코딩", "project"),
    ("운동", "health"),
    ("헬스", "health"),
    ("달리기", "health"),
    ("다이어트", "health"),
    ("약속", "schedule"),
    ("미팅", "schedule"),
    ("회의", "schedule"),
    ("습관", "routine"),
    ("루틴", "routine"),
    ("매일", "routine"),
)


def _rule_fallback_classify(raw_text: str) -> InboxClassification:
    """LLM 실패 시 키워드 매칭. confidence=0 → 항상 사용자 override 권장."""
    text_lower = raw_text.lower()
    category: InboxCategory = "other"
    for kw, cat in _KEYWORD_MAP:
        if kw in text_lower:
            category = cat
            break
    return InboxClassification(
        ai_category_guess=category,
        confidence=0.0,
        suggested_title=raw_text[:10],
        needs_user_override=True,
    )


def _to_schema(item: InboxItemModel) -> InboxItem:
    return InboxItem(
        inbox_id=f"{_ID_PREFIX}{item.id}",
        raw_text=decrypt_inbox_text(item.raw_text_encrypted),
        ai_category_guess=item.ai_category_guess,
        user_category=item.user_category,
        status=item.status,
        promoted_goal_id=(
            f"goal_{item.promoted_goal_id}" if item.promoted_goal_id is not None else None
        ),
    )


def _parse_inbox_id(inbox_id: str) -> UUID:
    if not inbox_id.startswith(_ID_PREFIX):
        raise _not_found()
    try:
        return UUID(inbox_id[len(_ID_PREFIX) :])
    except ValueError as e:
        raise _not_found() from e


def _not_found() -> ApiError:
    return ApiError(
        ErrorCode.INBOX_NOT_FOUND,
        "해당 Inbox 항목을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _today_kst() -> datetime:
    return datetime.now(KST)


RepoDep = Annotated[InboxRepo, Depends(get_inbox_repo)]
GoalRepoDep = Annotated[GoalRepo, Depends(get_goal_repo)]
ActionRepoDep = Annotated[ActionItemRepo, Depends(get_action_item_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("")
async def list_inbox(
    user: CurrentUser,
    repo: RepoDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[InboxItem]:
    """내 inbox 항목. `?status=captured|classified|promoted` 필터."""
    items = await repo.list_by_status(user.id, status_filter)
    return [_to_schema(i) for i in items]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_inbox(
    body: InboxCreateRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> InboxItem:
    """1줄 캡처 + AI category 추정 (LLM 1회, 8s timeout, 실패 시 룰 fallback)."""
    # 1) AI category 추정 — Tool Executor 가 prompt 렌더·budget·safety·fallback 일괄 처리.
    result = await aiClient.run(
        module="inbox",
        schema=InboxClassification,
        prompt_id="inbox/classify",
        fallback=lambda: _rule_fallback_classify(body.raw_text),
        timeout=8.0,
        variables={"raw_text": body.raw_text},
        user_id=user.id,
        session=session,
        tone_mode=user.tone_mode,
    )
    classification = result.value

    # 2) 암호화 저장 — raw_text_encrypted = AES-256-GCM(b"reaction:inbox-text").
    item = await repo.create(
        user_id=user.id,
        raw_text_encrypted=encrypt_inbox_text(body.raw_text),
        ai_category_guess=classification.ai_category_guess,
        status="classified",  # ai_category_guess 채워졌으므로 자동 classified
    )
    await session.commit()
    await session.refresh(item)
    return _to_schema(item)


@router.patch("/{inbox_id}")
async def update_inbox(
    inbox_id: str,
    body: InboxUpdateRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> InboxItem:
    """userCategory override 또는 status 변경."""
    item = await repo.get_by_id(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()
    updated = await repo.update(
        item,
        user_category=body.user_category,
        status=body.status,
    )
    await session.commit()
    await session.refresh(updated)
    return _to_schema(updated)


@router.post("/{inbox_id}/convert-to-goal")
async def convert_to_goal(
    inbox_id: str,
    user: CurrentUser,
    repo: RepoDep,
    goal_repo: GoalRepoDep,
    session: SessionDep,
) -> InboxItem:
    """Inbox → Goal 변환 (tier=maintain default, 한도 enforce). inbox.status=promoted."""
    item = await repo.get_by_id(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()

    # Maintain ≤ 5 enforce (Parked 였다면 별도 — 기본 maintain 으로 진입)
    current = await goal_repo.count_by_tier(user.id, "maintain")
    if current + 1 > _MAINTAIN_LIMIT:
        raise ApiError(
            ErrorCode.GOAL_TIER_LIMIT_EXCEEDED,
            f"Maintain 목표는 최대 {_MAINTAIN_LIMIT}개까지 가질 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="goalTier",
        )

    raw_text = decrypt_inbox_text(item.raw_text_encrypted)
    category = item.user_category or item.ai_category_guess or "other"
    goal = await goal_repo.create(
        user_id=user.id,
        title=raw_text,
        category=category,
        goal_tier="maintain",
        priority_level=3,
    )
    await repo.mark_promoted_to_goal(item, goal.id)
    await session.commit()
    await session.refresh(item)
    return _to_schema(item)


@router.post("/{inbox_id}/convert-to-action")
async def convert_to_action(
    inbox_id: str,
    user: CurrentUser,
    repo: RepoDep,
    action_repo: ActionRepoDep,
    session: SessionDep,
) -> InboxItem:
    """Inbox → ActionItem(source=inbox) 변환. inbox.status=promoted."""
    item = await repo.get_by_id(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()
    raw_text = decrypt_inbox_text(item.raw_text_encrypted)
    category = item.user_category or item.ai_category_guess or "other"
    await action_repo.create_from_inbox(
        user_id=user.id,
        inbox_item_id=item.id,
        title=raw_text[:300],  # ActionItem.title 컬럼 길이 제한
        category=category,
        target_date=_today_kst().date(),
    )
    await repo.mark_promoted_to_action(item)
    await session.commit()
    await session.refresh(item)
    return _to_schema(item)


@router.post("/{inbox_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_inbox(
    inbox_id: str,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> None:
    """Inbox 항목 soft delete (`archived_at` + `status=archived`)."""
    item = await repo.get_by_id(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()
    await repo.soft_delete(item)
    await session.commit()
    return None
