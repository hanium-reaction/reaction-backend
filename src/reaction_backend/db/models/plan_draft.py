"""PlanDraft — First Plan 생성 결과의 HITL Draft 영속화 (#62, ADR-0005 §2.5.1·§7.8).

`POST /plans/generate` 가 만든 임시 계획을 저장하고 실제 `plan_id` 를 부여한다. 사용자가
`GET /plans/{id}` 로 다시 보고 `POST /plans/{id}/approve` 로 [수락] 하면 SAVING 단계에서
goal 트리로 영속화된다. 자동 적용 금지(AGENTS §1.4) — 본 행은 승인 전까지 비활성 Draft.

규칙:
- `payload`(JSONB) 에 생성 시점 스냅샷(outcome·goal_nodes·action_items·blocks 등)을 통째로
  저장한다. GET/approve 는 이 스냅샷을 schema 로 재구성해 사용(LLM 재호출 0회).
- `expires_at` 72h(ADR-0005 §7.8) — 지나면 `expire_stale_drafts` cron 이 status='expired'.
- soft state only: hard delete 금지. 만료는 status 전이로 표현.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


# draft(미승인) → approved(승인·영속화 완료) | expired(72h 미응답 만료, §7.8)
PLAN_DRAFT_STATUS_VALUES = ("draft", "approved", "expired")

# ai_source — LLM 성공 / 룰 fallback (ADR-0005 §7.2)
PLAN_DRAFT_AI_SOURCE_VALUES = ("llm", "rule")


class PlanDraft(Base, TimestampMixin):
    __tablename__ = "plan_drafts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        Enum(*PLAN_DRAFT_STATUS_VALUES, name="plan_draft_status"),
        nullable=False,
        server_default="draft",
        index=True,
    )

    target_date: Mapped[date] = mapped_column(Date, nullable=False)

    horizon: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "YYYY-MM-DD"

    ai_source: Mapped[str] = mapped_column(
        Enum(*PLAN_DRAFT_AI_SOURCE_VALUES, name="plan_draft_ai_source"),
        nullable=False,
        server_default="llm",
    )

    # 생성 시점 스냅샷 — outcome / goal_nodes / action_items / blocks / warnings / policy_violations
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── relationships ──
    user: Mapped[User] = relationship()
