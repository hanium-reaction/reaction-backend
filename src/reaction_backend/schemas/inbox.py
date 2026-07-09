"""Inbox 도메인 스키마 (api-contract §18) — S24 Life Inbox / S25 Triage."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel

# Inbox 항목 라이프사이클 + category enum (DB 모델과 일치)
InboxStatus = Literal["captured", "classified", "archived", "promoted"]
InboxCategory = Literal["study", "project", "health", "routine", "schedule", "other"]


class InboxItem(CamelModel):
    """Inbox 항목 — GET/POST/PATCH/convert-* 응답.

    `raw_text` 는 응답에서 복호화된 평문. DB 는 `raw_text_encrypted` (AES-256-GCM).
    """

    inbox_id: str
    raw_text: str
    ai_category_guess: str | None
    user_category: str | None
    status: str
    promoted_goal_id: str | None
    # 승격 대상 구분 — status=promoted 일 때만 'goal'/'action', 그 외 null.
    # goal 은 promoted_goal_id 로 딥링크, action 은 오늘 실행 화면으로 이동.
    promoted_to: Literal["goal", "action"] | None = None


class InboxCreateRequest(CamelModel):
    """POST /inbox — 1줄 캡처."""

    raw_text: str = Field(min_length=1)


class InboxUpdateRequest(CamelModel):
    """PATCH /inbox/{id} — userCategory override 또는 status 변경."""

    user_category: InboxCategory | None = None
    status: InboxStatus | None = None


class InboxClassification(CamelModel):
    """LLM Structured Output — `aiClient.run("inbox/classify")` 응답 schema (내부 사용).

    Tool Executor 가 강제 검증. fallback 룰도 같은 schema 로 반환.
    """

    ai_category_guess: InboxCategory
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_title: str
    needs_user_override: bool
