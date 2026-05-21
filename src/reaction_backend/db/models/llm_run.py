"""LlmRun — 모든 LLM 호출 로그.

Tool Executor (llm/) 가 호출마다 1행 INSERT. Cost dashboard / 디버깅 / 프롬프트 회귀의 원본.

DB 설계서 v0.7.1 §5.28:
- module: interview/planning/brief/recovery/inbox (5 종)
- prompt_version: VARCHAR(40) (ADR §3.4 — A/B 테스트 라벨)
- input_summary_encrypted / output_summary_encrypted: TEXT (PII, 익명화 대상)
- tokens_in / tokens_out (이름 정렬)
- cost_cents: INT (DB 설계서 명시 — 우리 Numeric → Int)
- success / fell_back / trace_id (이름 정렬)

규칙:
- 행은 INSERT only. UPDATE 금지. updated_at 없음.
- 익명화 cron 시 input_summary_encrypted / output_summary_encrypted = '[anonymized]'

우리 개선 (ADR §4 보존):
- prompt_id — 디버깅 가시화
- error — 실패 메시지 (200자 trim)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from reaction_backend.db.base import Base

if TYPE_CHECKING:
    pass


# DB 설계서 §5.28 명세 5 종 정렬
LLM_MODULE_VALUES = (
    "interview",
    "planning",
    "brief",
    "recovery",
    "inbox",
)


class LlmRun(Base):
    __tablename__ = "llm_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # 일부 cron(daily_brief precompute 등)은 system 호출 — nullable.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    module: Mapped[str] = mapped_column(
        Enum(*LLM_MODULE_VALUES, name="llm_module"),
        nullable=False,
    )

    model: Mapped[str] = mapped_column(String(64), nullable=False)

    # 프롬프트 버전 — A/B 테스트 라벨 (예: 'v1.2-shadow', 'interview-deep-v3-canary')
    # ADR 0001 §3.4 — VARCHAR(40) 채택
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)

    # 입출력 요약 (at-rest 암호화, 익명화 대상)
    input_summary_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_summary_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 토큰 (DB 설계서 §5.28 컬럼명 정렬: tokens_in / tokens_out)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # cent 단위 (Integer — DB 설계서 §5.28)
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # 성공 여부 — DB 설계서 §5.28
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # 룰 폴백 사용 — DB 설계서 §5.28 컬럼명: fell_back
    fell_back: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # 분산 추적 ID — DB 설계서 §5.28
    trace_id: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)

    # ── 우리 개선 (ADR §4 보존) ──
    # 프롬프트 ID (prompts/<domain>/<name>.<version>.md 와 매핑)
    prompt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 200자 trim 권장 — 실패 디버깅
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # INSERT only. updated_at 없음.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        index=True,
    )
