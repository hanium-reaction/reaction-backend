"""Inbox 도메인 스키마 (api-contract §18) — S24 Life Inbox."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel

# Inbox 항목 라이프사이클
InboxStatus = Literal["captured", "classified", "archived", "promoted"]


class InboxItem(CamelModel):
    """Inbox 항목 — GET/POST/PATCH/promote 응답."""

    inbox_id: str
    raw_text: str
    ai_category_guess: str | None
    user_category: str | None
    status: str
    promoted_goal_id: str | None


class InboxCreateRequest(CamelModel):
    """POST /inbox — 1줄 캡처."""

    raw_text: str = Field(min_length=1)


class InboxUpdateRequest(CamelModel):
    """PATCH /inbox/{id} — userCategory override 또는 status 변경."""

    user_category: str | None = None
    status: InboxStatus | None = None
