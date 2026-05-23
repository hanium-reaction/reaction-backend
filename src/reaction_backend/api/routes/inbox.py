"""Inbox — Life Inbox (S24, api-contract §18).

#3-D 단계는 **mock 스텁**: 데모 Inbox 항목 + 캡처/승격 정적 응답.
실제 AI 카테고리 추정·암호화 저장·Goal 승격 트랜잭션은 후속 (#22).
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from reaction_backend.api.mock.inbox import DEMO_INBOX_ITEMS, DemoInboxItem
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.inbox import InboxCreateRequest, InboxItem, InboxUpdateRequest

router = APIRouter(prefix="/inbox", tags=["inbox"])


def _to_schema(demo: DemoInboxItem) -> InboxItem:
    return InboxItem(
        inbox_id=demo.inbox_id,
        raw_text=demo.raw_text,
        ai_category_guess=demo.ai_category_guess,
        user_category=demo.user_category,
        status=demo.status,
        promoted_goal_id=demo.promoted_goal_id,
    )


def _find(inbox_id: str) -> DemoInboxItem:
    """스텁은 DEMO_INBOX_ITEMS 의 id 만 유효 — 그 외는 404."""
    for item in DEMO_INBOX_ITEMS:
        if item.inbox_id == inbox_id:
            return item
    raise ApiError(
        ErrorCode.INBOX_NOT_FOUND,
        "해당 Inbox 항목을 찾을 수 없어요.",
        http_status=status.HTTP_404_NOT_FOUND,
    )


@router.get("")
async def list_inbox(
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[InboxItem]:
    """[stub] 내 inbox 항목. `?status=` 으로 필터."""
    items = [_to_schema(item) for item in DEMO_INBOX_ITEMS]
    if status_filter:
        items = [item for item in items if item.status == status_filter]
    return items


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_inbox(body: InboxCreateRequest) -> InboxItem:
    """[stub] 1줄 캡처. AI 카테고리 추정은 후속."""
    return InboxItem(
        inbox_id="inbox_new_stub",
        raw_text=body.raw_text,
        ai_category_guess=None,
        user_category=None,
        status="captured",
        promoted_goal_id=None,
    )


@router.patch("/{inbox_id}")
async def update_inbox(inbox_id: str, body: InboxUpdateRequest) -> InboxItem:
    """[stub] userCategory override 또는 status 변경."""
    demo = _find(inbox_id)
    return InboxItem(
        inbox_id=demo.inbox_id,
        raw_text=demo.raw_text,
        ai_category_guess=demo.ai_category_guess,
        user_category=(
            body.user_category if body.user_category is not None else demo.user_category
        ),
        status=body.status if body.status is not None else demo.status,
        promoted_goal_id=demo.promoted_goal_id,
    )


@router.post("/{inbox_id}/promote")
async def promote_to_goal(inbox_id: str) -> InboxItem:
    """[stub] Inbox 항목을 Goal 로 승격. 실제 Goal 생성·트랜잭션은 후속."""
    demo = _find(inbox_id)
    return InboxItem(
        inbox_id=demo.inbox_id,
        raw_text=demo.raw_text,
        ai_category_guess=demo.ai_category_guess,
        user_category=demo.user_category,
        status="promoted",
        promoted_goal_id="goal_promoted_stub",
    )


@router.delete("/{inbox_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_inbox(inbox_id: str) -> None:
    """[stub] Inbox 항목 soft delete (archived_at)."""
    _find(inbox_id)
    return None
