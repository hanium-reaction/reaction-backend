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

    # cent 단위 (Integer — DB 설계서 §5.28). **집계에는 쓰지 말 것.**
    # 정수 센트라 싼 호출이 통째로 0 이 된다: 인터뷰 1회는 약 0.05센트 → 0 으로 반올림되고,
    # 1,095회를 더해도 0 이다(실제로는 약 $0.5). 설계서 계약 유지용으로 남긴다.
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # 백만분의 1 USD. **비용 집계는 이 컬럼으로 한다.**
    # 정수라 부동소수 누적 오차가 없고, 가장 싼 호출(인터뷰 ≈ 500μ$)도 세 자리 유효숫자로
    # 남는다. 이 마이그레이션 이전 행은 0 이다 — 그때의 `tokens_out` 은 사고 토큰을 빼고
    # 기록돼 있어(33% 누락) 소급 계산하면 틀린 숫자가 진짜처럼 쌓인다.
    cost_micro_usd: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

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

    # 이 호출이 발생시킨 **검색 그라운딩 요청 수** (#259 §3). 0 이면 그라운딩을 안 쓴 호출.
    #
    # 왜 별도 컬럼인가: 그라운딩은 토큰과 **별도 과금**이고(무료 5,000건/월, 초과분
    # $14/1,000건), 검색이 서버 쪽에서 일어나 **입력 토큰이 17개로 잡힌다**. 즉
    # `tokens_in + tokens_out` 기반인 일일 토큰 예산이 이 비용에 완전히 눈이 멀어 있다 —
    # 루프가 돌면 계량기는 0 인데 1,000건당 $14 가 나간다(#259 §3 실측).
    grounding_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # fallback 사유 코드 (rate_limited/timeout/validation/budget/banned/unavailable/
    # no_prompt/provider_error — RunResult.reason 과 같은 값). success=True 면 NULL.
    # `error` 는 자유 텍스트라 원인별 집계가 안 된다(timeout 은 예외 메시지가 빈 문자열이라
    # falsy 체크에 걸려 NULL 로 저장됨) — L1-4 의 fallback 3분해가 이 컬럼 없이는 불가능했다.
    reason: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # INSERT only. updated_at 없음.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        index=True,
    )
