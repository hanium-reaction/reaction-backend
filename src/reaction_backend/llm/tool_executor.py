"""LLM Tool Executor (ADR-0003).

핵심 호출 시그니처 — **동결**:
    aiClient.run(module, schema, prompt_id, fallback, timeout=8.0)

이 모듈이 모든 외부 LLM 호출의 단일 게이트다. 에이전트/오케스트레이터는
Gemini SDK 를 직접 import 하지 않고 `aiClient.run()` 만 호출한다 (AGENTS.md §2).

흐름 (성공):
    1) `prompts.registry` 에서 prompt 렌더
    2) `safety.llm_budget.check()` 로 일일 예산 확인
    3) `provider.generate_structured()` 호출 — schema 강제 + JSON 검증
    4) `safety.banned_words.enforce_structured()` 후처리 (명사 1:1 치환)
    5) `safety.tone_gate.check_structured()` — 치환으로 못 고치는 문장 구조 문제(사람
       귀인·자존감 부양) 검증. 걸리면 치환하지 않고 곧장 fallback(근거 대장 §4 S6)
    6) `safety.llm_budget.record()` 로 `llm_runs` INSERT (success=True, fell_back=False)
    7) validated schema 인스턴스 반환

흐름 (fallback) — 어떤 단계든 실패하면 다음을 즉시 실행:
    - Rate limit (429/quota)
    - asyncio TimeoutError (timeout 초과)
    - ProviderUnavailable (API key 누락 / SDK 미설치)
    - ProviderValidationError (schema 불일치, 재시도 후에도)
    - BudgetExceeded (일일 토큰 한도 초과)
    - 금지어 차단 (치환 후에도 재매칭되거나 `HARD_BLOCK_TERMS` 잔존)
    - 톤 게이트 차단 (사람 귀인·자존감 부양 마커 검출, `reason="tone_gate"`)

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
    GroundingSource as GroundingSource,
)
from reaction_backend.llm.provider import (
    ProviderError,
    ProviderRateLimited,
    ProviderResponse,
    ProviderUnavailable,
    ProviderValidationError,
    generate_grounded_text,
    generate_structured,
)
from reaction_backend.prompts import registry as prompt_registry
from reaction_backend.prompts.registry import PromptNotFound, PromptRenderError
from reaction_backend.safety.banned_words import enforce_structured
from reaction_backend.safety.banned_words import scan as banned_scan
from reaction_backend.safety.llm_budget import (
    BudgetExceeded,
    GroundingBudgetExceeded,
    LlmRunRecord,
    estimate_cost_cents,
    estimate_cost_micro_usd,
)
from reaction_backend.safety.llm_budget import (
    check as budget_check,
)
from reaction_backend.safety.llm_budget import (
    check_grounding as grounding_check,
)
from reaction_backend.safety.llm_budget import (
    record as record_run,
)
from reaction_backend.safety.tone_gate import check_structured as tone_gate_check

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
    """fallback 사유 코드 (rate_limited / timeout / validation / budget / banned /
    tone_gate / unavailable / no_prompt / provider_error)."""
    prompt_id: str
    prompt_version: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    banned_hits: tuple[str, ...] = ()


@dataclass(slots=True)
class GroundedResult:
    """`aiClient.run_grounded()` 결과 (#259 §4.2 ⑥).

    `RunResult` 와 달리 **fallback 값을 담지 않는다.** 자료 조사의 실패는 "자료 없음" 이고,
    그건 이미 시스템의 정상 경로다(분해 프롬프트가 `(없음)` 을 받는다). 실패했을 때 대신
    끼워 넣을 그럴듯한 목차를 만드는 것이야말로 이 기능이 막으려는 바로 그 실패다.

    그래서 호출자는 `text is None` 만 보면 된다 — None 이면 자료 없이 진행한다.
    """

    text: str | None
    """쓸 수 있는 자료 원문. **None 이면 쓰면 안 된다** (사유는 `reason`)."""
    sources: tuple[GroundingSource, ...]
    """사용자에게 고지할 출처 (⑩). `text` 가 None 이어도 진단용으로 채워질 수 있다."""
    search_queries: tuple[str, ...]
    reason: str | None
    """폐기 사유 코드 (ungrounded / empty / timeout / budget / grounding_budget /
    unavailable / rate_limited / provider_error / no_prompt). 성공은 None."""
    prompt_id: str
    prompt_version: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    grounding_requests: int = 0
    banned_hits: tuple[str, ...] = ()
    """자료 원문에서 **발견된** 금지어. 치환하지 않는다 — `run_grounded` 주석 참조."""

    @property
    def usable(self) -> bool:
        return self.text is not None


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

        # ── 4) 금지어 후처리 (명사 치환 — 톤 게이트는 다음 단계) ──────
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

        # ── 5) 톤 게이트 (근거 대장 §4 S6) ──────────────────────────
        # banned_words 는 명사 1:1 치환이라 "당신이 게을러서" 류의 문장 구조 문제는 못
        # 고친다 — 안전한 대체 표현이 없으므로 치환하지 않고 곧장 fallback 한다.
        tone_blocked, tone_hits = tone_gate_check(sanitized.model_dump())
        if tone_blocked:
            return await self._fallback(
                fallback,
                module=module,
                schema=schema,
                prompt_id=resolved_prompt_id,
                prompt_version=prompt_version,
                reason="tone_gate",
                error=f"tone_gate_blocked: {tone_hits}",
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                tokens_in=provider_resp.tokens_in,
                tokens_out=provider_resp.tokens_out,
                latency_ms=int((time.monotonic() - started) * 1000),
                log_payloads=log_payloads,
                input_summary=prompt_text if log_payloads else None,
                output_summary=sanitized.model_dump_json() if log_payloads else None,
                banned_hits=tone_hits,
            )

        latency_ms = int((time.monotonic() - started) * 1000)

        # ── 6) llm_runs INSERT ──────────────────────────────────────
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
                        provider_resp.model, provider_resp.tokens_in, provider_resp.tokens_out
                    ),
                    cost_micro_usd=estimate_cost_micro_usd(
                        provider_resp.model, provider_resp.tokens_in, provider_resp.tokens_out
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
    async def run_grounded(
        self,
        module: str,
        prompt_id: str,
        *,
        variables: Mapping[str, str] | None = None,
        user_id: uuid.UUID | None = None,
        session: AsyncSession | None = None,
        trace_id: str | None = None,
        timeout: float | None = None,
        log_payloads: bool = False,
    ) -> GroundedResult:
        """검색 그라운딩 진입점 (#259 §4.2 ⑥). `run()` 과 **별도**다.

        `run()` 시그니처는 ADR-0003 에서 동결이고, 그라운딩은 반환 계약부터 다르다
        (schema 인스턴스가 아니라 원문 텍스트 + 출처). 덮지 않고 옆에 낸다.

        `run()` 과 다른 점 세 가지, 모두 의도적이다:

        **1. 재시도하지 않는다.** `run()` 은 3회까지 재시도하지만 그라운딩은 **요청 건수로
        과금**된다(초과분 $14/1,000건). 재시도는 조용히 비용을 3배로 만든다. 실패의 대가는
        "자료 없음" 뿐이고 그건 이미 정상 경로라, 여기선 재시도가 사는 값보다 비싸다.

        **2. 출처 0 건이면 텍스트를 버린다.** 이 가드가 기능 전체의 핵심이다 — 모델은
        존재하지 않는 교재에 대해서도 **에러 없이, 자신 있게** 5챕터 목차를 만들어낸다
        (#259 §2 실측). 출처 개수만이 "실제로 확인했는가" 의 신뢰할 만한 신호다.

        **3. 금지어를 치환하지 않고 스캔만 한다.** `run()` 은 우리가 생성한 문장을 치환하지만
        (DevBaseline §4.2), 여기 텍스트는 **인용한 외부 자료**다. 목차의 "실패 없는 영어" 를
        "한 번 멈춤 없는 영어" 로 바꾸면 **존재하지 않는 챕터를 사실인 양 인용**하게 된다 —
        이 기능이 막으려는 실패를 우리 손으로 만드는 셈이다. 그래서 발견 사실만
        `banned_hits` 로 올리고, 사용자에게 보여줄 때의 처리는 표시 계층의 결정으로 남긴다.
        사용자에게 실제로 나가는 **계획 문장**은 `run()` 경로가 그대로 필터링한다.

        Parameters
        ----------
        module:
            `llm_runs.module` enum. 자료 조사는 계획 앞단이므로 보통 `"planning"`.
        prompt_id:
            보통 `"planning/materials_search"`. 변수 `query` 는 **사용자가 확인·편집한
            검색어**여야 한다 (#259 §4.1 ① 결정 — 목표 텍스트를 그대로 외부 검색에 보내지
            않는다). 이 함수는 그 계약을 강제하지 못하니 호출부가 지켜야 한다.
        timeout:
            None 이면 `llm_grounding_timeout_seconds`(20s). 동결값 8.0 은 실측 중앙값
            8.5s 보다 짧아 절반이 타임아웃난다.
        """
        settings = get_settings()
        resolved_model = settings.llm_model_grounding
        effective_timeout = (
            timeout if timeout is not None else settings.llm_grounding_timeout_seconds
        )
        started = time.monotonic()

        def _elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        # ── 1) 프롬프트 ─────────────────────────────────────────────
        try:
            prompt_text, tmpl = prompt_registry.render(prompt_id, dict(variables or {}))
            resolved_prompt_id, prompt_version = tmpl.prompt_id, tmpl.version
        except (PromptNotFound, PromptRenderError) as exc:
            return await self._grounding_discard(
                module=module,
                prompt_id=prompt_id,
                prompt_version="unknown",
                reason="no_prompt",
                error=str(exc),
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                latency_ms=_elapsed(),
                model=resolved_model,
            )

        # ── 2) 예산 가드 — 토큰과 그라운딩 **둘 다** ────────────────
        # 둘은 서로를 대신하지 못한다: 그라운딩 호출은 토큰을 거의 안 써서(실측 in 17)
        # 토큰 가드를 언제나 통과한다. 토큰 가드만 걸면 이 경로엔 상한이 없는 것과 같다.
        if session is not None:
            try:
                await budget_check(session, user_id=user_id)
            except BudgetExceeded as exc:
                return await self._grounding_discard(
                    module=module,
                    prompt_id=resolved_prompt_id,
                    prompt_version=prompt_version,
                    reason="budget",
                    error=str(exc),
                    user_id=user_id,
                    session=session,
                    trace_id=trace_id,
                    latency_ms=_elapsed(),
                    model=resolved_model,
                )
            try:
                await grounding_check(session, user_id=user_id)
            except GroundingBudgetExceeded as exc:
                return await self._grounding_discard(
                    module=module,
                    prompt_id=resolved_prompt_id,
                    prompt_version=prompt_version,
                    reason="grounding_budget",
                    error=str(exc),
                    user_id=user_id,
                    session=session,
                    trace_id=trace_id,
                    latency_ms=_elapsed(),
                    model=resolved_model,
                )

        # ── 3) 단 한 번의 provider 호출 ─────────────────────────────
        # 여기부터는 **요청이 나갔다고 본다.** 아래 어느 경로로 끝나든 grounding_requests=1
        # 로 기록한다: 타임아웃이든 5xx 든 요청은 이미 Google 에 닿았을 수 있고, 청구 여부를
        # 우리가 관측할 방법이 없다. 예산 가드에서 **과소 계수가 위험한 방향**이므로 보수적으로
        # 센다. 과대 계수의 대가는 상한에 조금 일찍 닿는 것뿐이다.
        try:
            resp = await asyncio.wait_for(
                generate_grounded_text(
                    prompt_text=prompt_text,
                    timeout=effective_timeout,
                    model=resolved_model,
                ),
                timeout=effective_timeout,
            )
        except TimeoutError as exc:
            return await self._grounding_discard(
                module=module,
                prompt_id=resolved_prompt_id,
                prompt_version=prompt_version,
                reason="timeout",
                error=str(exc),
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                latency_ms=_elapsed(),
                model=resolved_model,
                grounding_requests=1,
            )
        except ProviderUnavailable as exc:
            # key 누락·SDK 미설치 — 요청이 **나가지 않았다.** 여기만 0 건이다.
            return await self._grounding_discard(
                module=module,
                prompt_id=resolved_prompt_id,
                prompt_version=prompt_version,
                reason="unavailable",
                error=str(exc),
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                latency_ms=_elapsed(),
                model=resolved_model,
            )
        except ProviderRateLimited as exc:
            return await self._grounding_discard(
                module=module,
                prompt_id=resolved_prompt_id,
                prompt_version=prompt_version,
                reason="rate_limited",
                error=str(exc),
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                latency_ms=_elapsed(),
                model=resolved_model,
                grounding_requests=1,
            )
        except ProviderError as exc:
            return await self._grounding_discard(
                module=module,
                prompt_id=resolved_prompt_id,
                prompt_version=prompt_version,
                reason="provider_error",
                error=str(exc),
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                latency_ms=_elapsed(),
                model=resolved_model,
                grounding_requests=1,
            )

        latency_ms = _elapsed()
        text = resp.text.strip()

        # ── 4) 그라운딩 증거 검사 — 이 가드가 기능의 핵심 ───────────
        discard_reason = None if resp.sources else "ungrounded"
        if discard_reason is None and not text:
            discard_reason = "empty"

        if discard_reason is not None:
            return await self._grounding_discard(
                module=module,
                prompt_id=resolved_prompt_id,
                prompt_version=prompt_version,
                reason=discard_reason,
                error=None,
                user_id=user_id,
                session=session,
                trace_id=trace_id,
                latency_ms=latency_ms,
                model=resp.model,
                grounding_requests=1,
                tokens_in=resp.tokens_in,
                tokens_out=resp.tokens_out,
                sources=resp.sources,
                search_queries=resp.search_queries,
                input_summary=prompt_text if log_payloads else None,
            )

        # ── 5) 금지어 스캔 (치환 아님 — docstring 3 참조) ───────────
        banned_hits = banned_scan(text)

        # ── 6) llm_runs INSERT ──────────────────────────────────────
        if session is not None:
            await record_run(
                session,
                LlmRunRecord(
                    module=module,
                    model=resp.model,
                    prompt_id=resolved_prompt_id,
                    prompt_version=prompt_version,
                    tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out,
                    latency_ms=latency_ms,
                    success=True,
                    fell_back=False,
                    cost_cents=estimate_cost_cents(resp.model, resp.tokens_in, resp.tokens_out),
                    cost_micro_usd=estimate_cost_micro_usd(
                        resp.model, resp.tokens_in, resp.tokens_out
                    ),
                    user_id=user_id,
                    trace_id=trace_id,
                    grounding_requests=1,
                    input_summary=(prompt_text if log_payloads else None),
                    output_summary=(text if log_payloads else None),
                ),
            )

        return GroundedResult(
            text=text,
            sources=resp.sources,
            search_queries=resp.search_queries,
            reason=None,
            prompt_id=resolved_prompt_id,
            prompt_version=prompt_version,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            latency_ms=latency_ms,
            grounding_requests=1,
            banned_hits=banned_hits,
        )

    async def _grounding_discard(
        self,
        *,
        module: str,
        prompt_id: str,
        prompt_version: str,
        reason: str,
        error: str | None,
        user_id: uuid.UUID | None,
        session: AsyncSession | None,
        trace_id: str | None,
        latency_ms: int,
        model: str,
        grounding_requests: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        sources: tuple[GroundingSource, ...] = (),
        search_queries: tuple[str, ...] = (),
        input_summary: str | None = None,
    ) -> GroundedResult:
        """자료를 버리고 `text=None` 으로 돌아온다 + `llm_runs` 에 남긴다.

        `_fallback` 과 달리 대체 값을 만들지 않는다 — 자료 조사의 실패는 "자료 없음" 이다.
        그래도 **행은 반드시 남긴다**: 폐기가 잦다는 사실(특히 `ungrounded`)이야말로
        트리거 설계를 다시 봐야 한다는 신호이고, 기록이 없으면 그 신호도 없다.
        """
        _log.warning(
            "llm_grounding_discarded",
            extra={
                "llm_module": module,
                "prompt_id": prompt_id,
                "prompt_version": prompt_version,
                "reason": reason,
                "error": error,
                "sources": len(sources),
                "user_id": str(user_id) if user_id else None,
                "trace_id": trace_id,
            },
        )

        if session is not None:
            await record_run(
                session,
                LlmRunRecord(
                    module=module,
                    model=model,
                    prompt_id=prompt_id,
                    prompt_version=prompt_version,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency_ms,
                    success=False,
                    fell_back=True,
                    cost_cents=estimate_cost_cents(model, tokens_in, tokens_out),
                    cost_micro_usd=estimate_cost_micro_usd(model, tokens_in, tokens_out),
                    user_id=user_id,
                    trace_id=trace_id,
                    error=error,
                    reason=reason,
                    grounding_requests=grounding_requests,
                    input_summary=input_summary,
                ),
            )

        return GroundedResult(
            text=None,
            sources=sources,
            search_queries=search_queries,
            reason=reason,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            grounding_requests=grounding_requests,
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
            # 폴백 경로엔 provider 응답이 없다 → 설정상 이 모듈이 썼을 모델로 단가를 잡는다.
            # 토큰이 0 이면(호출 전 차단) 비용도 0 이라 모델명이 틀려도 영향이 없다.
            fallback_model = get_settings().model_for_module(module)
            await record_run(
                session,
                LlmRunRecord(
                    module=module,
                    model=fallback_model,
                    prompt_id=prompt_id,
                    prompt_version=prompt_version,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency_ms,
                    success=False,
                    fell_back=True,
                    cost_cents=estimate_cost_cents(fallback_model, tokens_in, tokens_out),
                    cost_micro_usd=estimate_cost_micro_usd(fallback_model, tokens_in, tokens_out),
                    user_id=user_id,
                    trace_id=trace_id,
                    error=error,
                    reason=reason,
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
