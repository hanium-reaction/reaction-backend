"""DailyBrief — Morning Brief 캐시 (v0.7, S10).

생성: 매일 06:00 cron (daily_brief_precompute) — LLM 1회 호출 + 결과 저장.
S10 Today Agenda 진입 시 LLM 호출 없이 이 행만 SELECT.

규칙:
- expires_at 지나면 무효 (보통 다음 날 새벽)
- LLM 실패 시 룰 폴백으로 채우고 fallback_used=true 기록
- big_rock_action_item_id 가 매칭되는 카드는 화면 최상단 큰 사이즈로 표시
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


class DailyBrief(Base, TimestampMixin):
    __tablename__ = "daily_briefs"

    __table_args__ = (UniqueConstraint("user_id", "brief_date", name="uq_daily_briefs_user_date"),)

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

    brief_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # AI 헤드라인 1~2문장 (Draft Layer 시각 구분) — DB 설계서 §5.21 컬럼명: headline_text
    headline_text: Mapped[str] = mapped_column(Text, nullable=False)

    # 화면 최상단 큰 카드 — 사용자가 지정했거나 AI 가 결정
    big_rock_action_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # LLM 호출 추적 — DB 설계서 §5.21
    llm_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # 우리 개선 (ADR §4 보존) — "오후 2시 회의 전에 마무리하면 좋아요" 같은 보조 안내
    adjustment_hints: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # ── relationships ──
    user: Mapped[User] = relationship()
