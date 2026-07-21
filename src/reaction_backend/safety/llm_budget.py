"""LLM 일일 토큰 예산 가드 + `llm_runs` 비동기 로깅.

Issue #5 §4.

핵심 책임:
1. `check()` — 호출 직전 사용자/시스템 토큰 누적 합산을 확인하고
   설정된 일일 한도를 넘으면 `BudgetExceeded` 로 차단. Tool Executor 는
   이 신호를 받아 즉시 fallback 분기 (LLM 호출 자체를 안 함).
2. `record()` — 호출 결과를 `llm_runs` 행으로 비동기 INSERT.
   token in/out, latency, cost_cents(추정), success, fell_back, prompt_id/version,
   model, trace_id, 그리고 (옵션) AES-GCM 암호화된 입출력 요약을 함께 기록.

KST 기준 일자(now_kst().date()) 로 day boundary 를 잡는다. — `now_kst()` 사용 강제.

`llm_runs` 행은 INSERT only. UPDATE 금지 (DB 설계서 §5.28).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.db.models.llm_run import LLM_MODULE_VALUES, LlmRun
from reaction_backend.safety.encryption import encrypt_llm_payload
from reaction_backend.schemas.common import now_kst

_log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """일일 토큰 예산 초과. Tool Executor 가 잡아서 fallback 으로 분기."""

    def __init__(self, used: int, limit: int) -> None:
        super().__init__(f"daily LLM token budget exceeded: used={used}, limit={limit}")
        self.used = used
        self.limit = limit


@dataclass(slots=True)
class BudgetStatus:
    """`check()` 의 비차단(=정상) 결과."""

    used: int
    limit: int
    remaining: int


@dataclass(slots=True)
class LlmRunRecord:
    """`record()` 에 넘기는 호출 결과 스냅샷.

    Tool Executor 가 만들어서 넘긴다. 모든 시간 처리는 호출자 쪽에서
    `now_kst()` 등으로 통일.
    """

    module: str
    """`LLM_MODULE_VALUES` 중 하나 (interview/planning/brief/recovery/inbox)."""
    model: str
    prompt_id: str | None
    prompt_version: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    success: bool
    fell_back: bool
    cost_cents: int
    user_id: uuid.UUID | None = None
    trace_id: str | None = None
    error: str | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def estimate_cost_cents(tokens_in: int, tokens_out: int) -> int:
    """Flash 무료 티어 = 0. 유료 전환 시 `LLM_COST_PER_1K_*_CENTS` 환경변수로 산정."""
    s = get_settings()
    cents = (tokens_in / 1000.0) * s.llm_cost_per_1k_input_cents + (
        tokens_out / 1000.0
    ) * s.llm_cost_per_1k_output_cents
    # `cost_cents` 는 Integer — 반올림.
    return int(round(cents))


async def _used_tokens_today(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
) -> int:
    """KST 기준 오늘 0시부터의 누적 (tokens_in + tokens_out)."""
    start_of_day_kst = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.coalesce(func.sum(LlmRun.tokens_in + LlmRun.tokens_out), 0)).where(
        LlmRun.created_at >= start_of_day_kst
    )
    if user_id is not None:
        stmt = stmt.where(LlmRun.user_id == user_id)
    else:
        stmt = stmt.where(LlmRun.user_id.is_(None))
    result = await session.execute(stmt)
    value = result.scalar_one()
    return int(value or 0)


async def check(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    projected_tokens: int = 0,
) -> BudgetStatus:
    """예산 가드. 한도 초과면 `BudgetExceeded` raise.

    `projected_tokens` 은 이번 호출이 추가로 소비할 것으로 예상하는 토큰
    (보통 prompt 토큰 추정치). 0 이면 단순 잔량 확인.
    """
    limit = get_settings().llm_daily_token_budget
    if limit <= 0:
        return BudgetStatus(used=0, limit=0, remaining=2**31 - 1)

    used = await _used_tokens_today(session, user_id=user_id)
    if used + max(projected_tokens, 0) > limit:
        raise BudgetExceeded(used=used, limit=limit)
    return BudgetStatus(used=used, limit=limit, remaining=limit - used)


async def record(
    session: AsyncSession,
    rec: LlmRunRecord,
) -> uuid.UUID:
    """`llm_runs` INSERT. 호출자가 `await session.commit()` 책임.

    민감 텍스트(`input_summary`/`output_summary`)는 AES-GCM 암호화 후 저장 (Issue #5 §3).
    """
    if rec.module not in LLM_MODULE_VALUES:
        raise ValueError(
            f"invalid llm_runs.module={rec.module!r}; must be one of {LLM_MODULE_VALUES}"
        )

    row = LlmRun(
        user_id=rec.user_id,
        module=rec.module,
        model=rec.model,
        prompt_id=rec.prompt_id,
        prompt_version=rec.prompt_version,
        tokens_in=rec.tokens_in,
        tokens_out=rec.tokens_out,
        latency_ms=rec.latency_ms,
        cost_cents=rec.cost_cents,
        success=rec.success,
        fell_back=rec.fell_back,
        trace_id=rec.trace_id,
        error=(rec.error[:200] if rec.error else None),
        input_summary_encrypted=(
            encrypt_llm_payload(rec.input_summary) if rec.input_summary else None
        ),
        output_summary_encrypted=(
            encrypt_llm_payload(rec.output_summary) if rec.output_summary else None
        ),
    )
    session.add(row)
    # flush 만 — commit 은 호출자 트랜잭션과 함께. (Tool Executor 는 보통 background task 로 commit)
    await session.flush()
    _log.info(
        "llm_run_recorded",
        extra={
            # 'module' 은 LogRecord 예약 속성이라 그대로 쓰면 KeyError 로 **호출부가 죽는다**
            # (tool_executor 의 llm_fallback 이 같은 이유로 llm_module 로 rename 돼 있다).
            # INFO 가 꺼져 있으면 레코드 생성 전에 반환돼 안 터질 뿐, 로깅을 켜는 순간 터진다.
            "llm_module": rec.module,
            "model": rec.model,
            "prompt_id": rec.prompt_id,
            "prompt_version": rec.prompt_version,
            "tokens_in": rec.tokens_in,
            "tokens_out": rec.tokens_out,
            "latency_ms": rec.latency_ms,
            "cost_cents": rec.cost_cents,
            "success": rec.success,
            "fell_back": rec.fell_back,
        },
    )
    return row.id
