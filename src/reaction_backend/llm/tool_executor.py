"""LLM Tool Executor (ADR-0003).

핵심 호출 시그니처 — **동결**:
    aiClient.run(module, schema, prompt_id, fallback, timeout=8.0)

이 모듈이 모든 외부 LLM 호출의 단일 게이트다. 에이전트/오케스트레이터는
Gemini SDK 를 직접 import 하지 않고 `aiClient.run()` 만 호출한다 (AGENTS.md §2).

흐름 (성공):
    1) `prompts.registry` 에서 prompt 렌더
    2) `safety.llm_budget.check()` 로 일일 예산 확인
    3) `provider.generate_structured()` 호출 — schema 강제 + JSON 검증
    4) `safety.banned_words.enforce_structured()` 후처리
    5) `safety.llm_budget.record()` 로 `llm_runs` INSERT (success=True, fell_back=False)
    6) validated schema 인스턴스 반환

흐름 (fallback) — 어떤 단계든 실패하면 다음을 즉시 실행:
    - Rate limit (429/quota)
    - asyncio TimeoutError (timeout 초과)
    - ProviderUnavailable (API key 누락 / SDK 미설치)
    - ProviderValidationError (schema 불일치, 재시도 후에도)
    - BudgetExceeded (일일 토큰 한도 초과)
    - 금지어 차단 (재생성 1회 후에도 hits)

fallback 은 다음 형태 모두 지원:
    - `BaseModel` 인스턴스 (그대로 반환)
    - `Callable[[], T]` (호출 후 반환)
    - `Callable[[], Awaitable[T]]` (await 후 반환)

호출 결과는 항상 `llm_runs` 에 1행 기록 (success/fell_back/error 메타 포함).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.llm.prompt_compose import compose_system_prompt
from reaction_backend.llm.provider import (
    ProviderError,
    ProviderRateLimited,
    ProviderResponse,
    ProviderUnavailable,
    ProviderValidationError,
    generate_structured,
)
from reaction_backend.prompts import registry as prompt_registry
from reaction_backend.prompts.registry import PromptNotFound, PromptRenderError
from reaction_backend.safety.banned_words import enforce_structured
from reaction_backend.safety.llm_budget import (
    BudgetExceeded,
    LlmRunRecord,
    estimate_cost_cents,
)
from reaction_backend.safety.llm_budget import (
    check as budget_check,
)
from reaction_backend.safety.llm_budget import (
    record as record_run,
)

_log = logging.getLogger(__name__)


type Fallback[T] = T | Callable[[], T] | Callable[[], Awaitable[T]]


@dataclass(slots=True)
class RunResult[T: BaseModel]:
    """`aiClient.run()` 결과.

    호출자는 `result.value` 만 보면 되고, fallback 여부·hit 등은 디버깅용.
    """

    value: T
    fell_back: bool
    reason: str | None
    """fallback 사유 코드 (rate_limited / timeout / validation / budget / banned / unavailable / no_prompt)."""
    prompt_id: str
    prompt_version: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    banned_hits: tuple[str, ...] = ()


class LLMToolExecutor:
    """단일 게이트. 인스턴스는 `aiClient` 로 노출."""

    async def run[T: BaseModel](
        self,
        module: str,
        schema: type[T],
        prompt_id: str,
        fallback: Fallback[T],
        timeout: float = 8.0,
        *,
        variables: Mapping[str, str] | None = None,
        user_id: uuid.UUID | None = None,
        session: AsyncSession | None = None,
        trace_id: str | None = None,
        log_payloads: bool = False,
        tone_mode: str | None = None,
        thinking_budget: int | None = None,
    ) -> RunResult[T]:
        """ADR-0003 동결 시그니처 (+ #23 tone_mode addendum + thinking_budget addendum).

        Parameters
        ----------
        module:
            `llm_runs.module` enum 5종 (interview/planning/brief/recovery/inbox).
        schema:
            Structured Output 으로 강제할 Pydantic 모델 타입.
        prompt_id:
            `prompts.registry` 의 `"<domain>/<name>"` 키. 없거나 렌더 실패 시 fallback.
        fallback:
            BaseModel | callable | async callable. 실패 시 즉시 분기.
        timeout:
            단일 시도 timeout (초). ADR-0003 § 동결 = 8.0.
        variables:
            프롬프트 `{{var}}` 치환 변수.
        user_id:
            null 이면 system 호출 (cron 등).
        session:
            제공되면 budget check + `llm_runs` INSERT. 없으면 logging 만.
        log_payloads:
            True 면 input/output 요약을 암호화 저장 (테스트에선 False 권장).
        tone_mode:
            gentle/strict/encouraging. 주어지면 렌더된 시스템 프롬프트 앞에 톤 prefix 1줄을
            덧붙인다 (ADR-0003 addendum, #23). None/미지원 값이면 prefix 없음 = 기존 동작.
        thinking_budget:
            호출별 Gemini thinking 예산(토큰). None(기본)이면 flash 계열 0(비활성) — 지연
            민감 호출(인터뷰 턴 등)용. 계획 분해·검토처럼 추론이 필요한 호출만 양수로 넘겨
            thinking 을 켠다 (provider._thinking_config). timeout 도 함께 상향 권장.
        """
        settings = get_settings()
        # task 별 모델 — 계획·회복은 상위 모델, 그 외는 base (config.model_for_module).
        resolved_model = settings.model_for_module(module)
        started = time.monotonic()
        prompt_version = "unknown"
        resolved_prompt_id = prompt_id

        # ── 1) 프롬프트 ─────────────────────────────────────────────
        try:
            prompt_text, tmpl = prompt_registry.render(prompt_id, dict(variables or {}))
            resolved_prompt_id = tmpl.prompt_id
            prompt_version = tmpl.version
        except (PromptNotFound, PromptRenderError) as exc:
            return await self._fallback(
                fallback,
                module=module,
                schema=schema,
                prompt_id=resolved_prompt_id,
                prompt_version=prompt_version,
                reason="no_prompt",
                error=str(exc),
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                latency_ms=int((time.monotonic() - started) * 1000),
                log_payloads=False,
            )

        # ── 1.5) 톤 prefix (ADR-0003 addendum, #23) — tone 없으면 원문 그대로 ──
        prompt_text = compose_system_prompt(prompt_text, tone_mode)

        # ── 2) 예산 가드 ────────────────────────────────────────────
        if session is not None:
            try:
                await budget_check(session, user_id=user_id)
            except BudgetExceeded as exc:
                return await self._fallback(
                    fallback,
                    module=module,
                    schema=schema,
                    prompt_id=resolved_prompt_id,
                    prompt_version=prompt_version,
                    reason="budget",
                    error=str(exc),
                    user_id=user_id,
                    session=session,
                    trace_id=trace_id,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    log_payloads=log_payloads,
                    input_summary=prompt_text if log_payloads else None,
                )

        # ── 3) provider 호출 + retry/backoff ────────────────────────
        last_error: BaseException | None = None
        last_reason: str | None = None
        provider_resp: ProviderResponse | None = None
        validated: T | None = None
        max_attempts = max(1, settings.llm_max_retries)

        for attempt in range(1, max_attempts + 1):
            try:
                validated, provider_resp = await asyncio.wait_for(
                    generate_structured(
                        schema=schema,
                        prompt_text=prompt_text,
                        timeout=timeout,
                        thinking_budget=thinking_budget,
                        model=resolved_model,
                    ),
                    timeout=timeout,
                )
                break
            except TimeoutError as exc:
                last_error, last_reason = exc, "timeout"
            except ProviderRateLimited as exc:
                last_error, last_reason = exc, "rate_limited"
                # 429 는 backoff 의미가 작지만 한 번은 더 시도.
            except ProviderUnavailable as exc:
                # key 누락·SDK 미설치 → 재시도 무의미.
                last_error, last_reason = exc, "unavailable"
                break
            except ProviderValidationError as exc:
                last_error, last_reason = exc, "validation"
            except ProviderError as exc:
                last_error, last_reason = exc, "provider_error"

            if attempt < max_attempts:
                await asyncio.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))

        if validated is None or provider_resp is None:
            return await self._fallback(
                fallback,
                module=module,
                schema=schema,
                prompt_id=resolved_prompt_id,
                prompt_version=prompt_version,
                reason=last_reason or "provider_error",
                error=str(last_error) if last_error else None,
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                latency_ms=int((time.monotonic() - started) * 1000),
                log_payloads=log_payloads,
                input_summary=prompt_text if log_payloads else None,
            )

        # ── 4) 금지어 후처리 (마지막 단계) ──────────────────────────
        sanitized_payload, blocked, hits = enforce_structured(validated.model_dump())
        if blocked:
            return await self._fallback(
                fallback,
                module=module,
                schema=schema,
                prompt_id=resolved_prompt_id,
                prompt_version=prompt_version,
                reason="banned",
                error=f"banned_words_blocked: {hits}",
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                tokens_in=provider_resp.tokens_in,
                tokens_out=provider_resp.tokens_out,
                latency_ms=int((time.monotonic() - started) * 1000),
                log_payloads=log_payloads,
                input_summary=prompt_text if log_payloads else None,
                output_summary=validated.model_dump_json() if log_payloads else None,
                banned_hits=hits,
            )

        # 치환 결과를 schema 로 재검증 — 안전.
        sanitized = schema.model_validate(sanitized_payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        # ── 5) llm_runs INSERT ──────────────────────────────────────
        if session is not None:
            await record_run(
                session,
                LlmRunRecord(
                    module=module,
                    model=provider_resp.model,
                    prompt_id=resolved_prompt_id,
                    prompt_version=prompt_version,
                    tokens_in=provider_resp.tokens_in,
                    tokens_out=provider_resp.tokens_out,
                    latency_ms=latency_ms,
                    success=True,
                    fell_back=False,
                    cost_cents=estimate_cost_cents(
                        provider_resp.tokens_in, provider_resp.tokens_out
                    ),
                    user_id=user_id,
                    trace_id=trace_id,
                    input_summary=(prompt_text if log_payloads else None),
                    output_summary=(sanitized.model_dump_json() if log_payloads else None),
                ),
            )

        return RunResult(
            value=sanitized,
            fell_back=False,
            reason=None,
            prompt_id=resolved_prompt_id,
            prompt_version=prompt_version,
            tokens_in=provider_resp.tokens_in,
            tokens_out=provider_resp.tokens_out,
            latency_ms=latency_ms,
            banned_hits=hits,
        )

    # ───────────────────────────────────────────────────────────────
    async def _fallback[T: BaseModel](
        self,
        fallback: Fallback[T],
        *,
        module: str,
        schema: type[T],
        prompt_id: str,
        prompt_version: str,
        reason: str,
        error: str | None,
        user_id: uuid.UUID | None,
        session: AsyncSession | None,
        trace_id: str | None,
        latency_ms: int,
        log_payloads: bool,
        tokens_in: int = 0,
        tokens_out: int = 0,
        input_summary: str | None = None,
        output_summary: str | None = None,
        banned_hits: tuple[str, ...] = (),
    ) -> RunResult[T]:
        value = await _resolve_fallback(fallback, schema=schema)

        # 룰 fallback 도 **사용자에게 나가는 문자열**이므로 금지어 필터를 통과시킨다.
        # 대부분의 fallback 은 신뢰된 카탈로그 템플릿이지만, 사용자 입력을 되돌려주는 것도
        # 있다(예: inbox 의 suggested_title=raw_text[:10]) — 그 경로가 필터를 우회하면
        # 잠금 결정(AGENTS.md §1 금지어 필터 강제)에 구멍이 난다.
        # 여기서는 치환만 하고 blocked 는 무시한다: fallback 의 fallback 은 없고,
        # 치환된 문구가 원문보다 항상 낫기 때문(무한 재귀 방지).
        sanitized_fallback, _, fallback_hits = enforce_structured(value.model_dump())
        if fallback_hits:
            value = schema.model_validate(sanitized_fallback)

        _log.warning(
            "llm_fallback",
            extra={
                # 'module' 은 logging.LogRecord reserved 라 rename (Python 3.12 검증 강함).
                "llm_module": module,
                "prompt_id": prompt_id,
                "prompt_version": prompt_version,
                "reason": reason,
                "error": error,
                "user_id": str(user_id) if user_id else None,
                "trace_id": trace_id,
            },
        )

        if session is not None:
            await record_run(
                session,
                LlmRunRecord(
                    module=module,
                    model=get_settings().model_for_module(module),
                    prompt_id=prompt_id,
                    prompt_version=prompt_version,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency_ms,
                    success=False,
                    fell_back=True,
                    cost_cents=estimate_cost_cents(tokens_in, tokens_out),
                    user_id=user_id,
                    trace_id=trace_id,
                    error=error,
                    input_summary=input_summary if log_payloads else None,
                    output_summary=output_summary if log_payloads else None,
                ),
            )

        return RunResult(
            value=value,
            fell_back=True,
            reason=reason,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            banned_hits=banned_hits,
        )


async def _resolve_fallback[T: BaseModel](fallback: Fallback[T], *, schema: type[T]) -> T:
    """`BaseModel` / sync callable / async callable 통합 해결."""
    if isinstance(fallback, schema):
        return fallback
    if isinstance(fallback, BaseModel):  # 잘못된 타입의 BaseModel — 명시 에러
        raise TypeError(
            f"fallback BaseModel must be {schema.__name__}, got {type(fallback).__name__}"
        )
    if callable(fallback):
        result: Any = fallback()
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, schema):
            raise TypeError(
                f"fallback callable must return {schema.__name__}, got {type(result).__name__}"
            )
        return result
    raise TypeError(
        f"fallback must be {schema.__name__} or Callable, got {type(fallback).__name__}"
    )


# 단일 진입점. 에이전트는 `from reaction_backend.llm import aiClient` 로 사용.
aiClient = LLMToolExecutor()
