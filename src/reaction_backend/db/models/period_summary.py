"""PeriodSummary — 주간/월간 집계 (S21 Weekly Review).

생성: 사용자 timezone 일요일 03:00 cron (weekly_review_precompute).
Weekly Review Agent 가 KPI + insights 를 계산해 INSERT.

DB 설계서 v0.7.1 §5.27:
- period_type: weekly/monthly (우리는 +quarterly 보존 — 향후 확장)
- start_date / end_date (DB 설계서 컬럼명 정렬 — period_start → start_date)
- KPI 12개 항목 (Memory Structure Weekly Report)
- avg_delay_minutes / restart_success_rate / repeated_failure_count
- llm_one_liner / failure_analysis (LLM 생성 텍스트)

우리 개선 (ADR §4 보존):
- peak_point_window (drain 의 짝)
- generated_at (cron 추적용)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


PERIOD_TYPE_VALUES = ("weekly", "monthly", "quarterly")


class PeriodSummary(Base, TimestampMixin):
    __tablename__ = "period_summaries"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "period_type",
            "start_date",
            name="uq_period_summaries_user_type_start",
        ),
    )

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

    period_type: Mapped[str] = mapped_column(
        Enum(*PERIOD_TYPE_VALUES, name="period_type"),
        nullable=False,
    )
    # DB 설계서 §5.27 컬럼명 정렬: period_start → start_date
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    # ── KPI ──
    adherence_rate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    consistency_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resilience_rate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)

    # 평균 시작 지연 분 — DB 설계서 §5.27
    avg_delay_minutes: Mapped[float | None] = mapped_column(Numeric(7, 2), nullable=True)

    # 인사이트 — "tuesday_morning", "wednesday_afternoon" 등
    drain_point_window: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 우리 개선 (ADR §4) — drain 의 짝
    peak_point_window: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # 카테고리별 성공률 — {"study": 0.72, "health": 0.5, ...}
    category_success_rate: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # 재시작 성공률 — DB 설계서 §5.27
    restart_success_rate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)

    # 동일 사유 반복 실패 수 — DB 설계서 §5.27
    repeated_failure_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 평균 회복 소요 분 — DB 설계서 §5.27
    average_recovery_minutes: Mapped[float | None] = mapped_column(Numeric(7, 2), nullable=True)

    # LLM 생성 텍스트 — DB 설계서 §5.27
    llm_one_liner: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_analysis: Mapped[str | None] = mapped_column(Text, nullable=True)

    # PolicySnapshot 갱신 후보들 — JSONB
    policy_update_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # 우리 개선 (ADR §4) — cron 추적용
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # ── relationships ──
    user: Mapped[User] = relationship()
