"""PolicySnapshot — 정책 버전 이력.

학습 루프의 산출물:
  주간 KPI → Policy Update Agent → 새 PolicySnapshot 후보 → 사용자 [적용] → INSERT

DB 설계서 v0.7.1 §5.24:
- payload (단일 JSONB) — 우리는 ADR 0001 §3.2 따라 **4 영역 분리** 유지
- is_active BOOLEAN — 현재 활성 스냅샷 여부 (append-only, in-place X)
- source: rule/llm/user_manual
- reason_for_update VARCHAR(200)
- prompt_version VARCHAR(40)

규칙 (ADR 0001):
- append-only — 새 INSERT + 이전 행 is_active=false 토글
- UPDATE 는 is_active 컬럼에만 허용 (DB 정책 또는 트리거)
- 4 영역 (behavioral / execution / interaction / recovery) 각각 JSONB 분리 유지
  (ADR §3.2 — 인덱싱·부분 업데이트·schema 진화 모두 유리)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User


# DB 설계서 §5.24 — rule/llm/user_manual
POLICY_SOURCE_VALUES = ("rule", "llm", "user_manual")


class PolicySnapshot(Base, TimestampMixin):
    __tablename__ = "policy_snapshots"

    __table_args__ = (
        UniqueConstraint("user_id", "version", name="uq_policy_snapshots_user_version"),
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

    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # 현재 활성 스냅샷 여부 — DB 설계서 §5.24 (UPDATE 는 이 컬럼에만 허용)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # ── 4 영역 JSONB (ADR §3.2 — 우리 결정: 분리 유지) ──
    behavioral_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    execution_constraints: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    interaction_style: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    recovery_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # 변경 출처 — DB 설계서 §5.24
    source: Mapped[str] = mapped_column(
        Enum(*POLICY_SOURCE_VALUES, name="policy_source"),
        nullable=False,
        server_default="rule",
    )

    # 변경 사유 (자연어) — DB 설계서 §5.24
    reason_for_update: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # LLM 사용 시 프롬프트 버전 — DB 설계서 §5.24
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)

    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── relationships ──
    user: Mapped[User] = relationship()
