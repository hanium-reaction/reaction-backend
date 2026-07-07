"""Gemini Structured Output provider (단일 라이브러리 의존성 격리).

에이전트/오케스트레이터는 이 모듈을 **직접 import 하지 않는다** (AGENTS.md §2 —
LLM SDK 직접 import 금지). 진입점은 `llm/tool_executor.py` 의 `aiClient.run()` 뿐.

요구 사항:
- Pydantic 모델을 받아 Gemini Structured Output 으로 강제.
- 재시도/타임아웃/예산 가드는 상위(`tool_executor`) 책임.
- API key 없거나 SDK 미설치는 명시 에러 (`ProviderUnavailable`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from reaction_backend.config import get_settings

if TYPE_CHECKING:
    # 타입 체크용 — 런타임 import 는 `_get_client()` 안에서.
    from google.genai import Client as GenaiClient  # noqa: F401

_log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """모든 provider 레벨 에러의 베이스."""


class ProviderUnavailable(ProviderError):
    """API key 누락·SDK 미설치 등 호출 자체가 불가능."""


class ProviderRateLimited(ProviderError):
    """429 / quota — Tool Executor 가 fallback 분기."""


class ProviderValidationError(ProviderError):
    """Structured Output 이 schema 검증을 통과하지 못함."""


@dataclass(slots=True)
class ProviderResponse:
    """raw provider 호출 결과 (구조화 검증 전)."""

    raw_text: str
    """Gemini 가 돌려준 JSON 문자열."""
    tokens_in: int
    tokens_out: int
    model: str


def _get_client() -> Any:
    """`google.genai.Client` 를 늦은 import 로 가져온다.

    API key 가 비어있으면 `ProviderUnavailable`.
    """
    api_key = get_settings().gemini_api_key
    if not api_key:
        raise ProviderUnavailable("GEMINI_API_KEY is not set")
    try:
        from google import genai  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise ProviderUnavailable("google-genai is not installed") from exc
    return genai.Client(api_key=api_key)


def _thinking_config(model_name: str, thinking_budget: int | None) -> dict[str, int] | None:
    """호출별 thinking 예산 → Gemini `thinking_config` (없으면 None = SDK 기본).

    - `thinking_budget=None`(대다수 호출): 워크로드가 분류·짧은 구조화 출력이라 thinking 이
      품질 이득 대비 지연 손해가 크고, 그 지연이 agent lock 점유를 늘려 동시성 충돌을
      유발한다(#76). 그래서 `gemini-2.5-flash` 계열은 기본 0(비활성). (2.5-pro 는 budget 0
      미지원이라 손대지 않음 → None.)
    - `thinking_budget` 지정(계획 분해·검토 등 추론 필요): 그 예산을 그대로 적용한다. 0 이면
      명시적 비활성, 양수면 thinking 활성.
    """
    if thinking_budget is not None:
        return {"thinking_budget": thinking_budget}
    if "2.5-flash" in model_name:
        return {"thinking_budget": 0}
    return None


async def generate_structured[T: BaseModel](
    *,
    schema: type[T],
    prompt_text: str,
    timeout: float,
    thinking_budget: int | None = None,
) -> tuple[T, ProviderResponse]:
    """Gemini 한 번 호출 → schema 인스턴스로 검증.

    - timeout 은 호출자(`tool_executor`)가 asyncio.wait_for 로 래핑.
    - Structured Output 은 Gemini 의 `response_schema` 기능을 활용,
      그래도 모델이 schema 를 어기면 `ProviderValidationError`.
    - thinking_budget 은 호출별 thinking 예산(`_thinking_config`). None 이면 모델 기본 정책.
    """
    client = _get_client()
    model_name = get_settings().llm_model

    config: dict[str, Any] = {
        "response_mime_type": "application/json",
        "response_schema": schema,
    }
    tcfg = _thinking_config(model_name, thinking_budget)
    if tcfg is not None:
        config["thinking_config"] = tcfg

    try:
        # `google-genai` 2.x 비동기 API
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt_text,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        message = str(exc).lower()
        if "rate" in message or "quota" in message or "429" in message:
            raise ProviderRateLimited(str(exc)) from exc
        raise ProviderError(str(exc)) from exc

    raw_text = _extract_text(response)
    usage = _extract_usage(response, model_name)

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ProviderValidationError(f"non-JSON response: {raw_text[:200]}") from exc

    try:
        validated = schema.model_validate(parsed)
    except ValidationError as exc:
        raise ProviderValidationError(str(exc)) from exc

    return validated, usage


def _extract_text(response: Any) -> str:
    """`google-genai` 응답에서 텍스트 페이로드 추출. SDK 버전 차이 흡수."""
    text = getattr(response, "text", None)
    if isinstance(text, str) and text:
        return text
    # 폴백: candidates[0].content.parts[0].text
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        if parts:
            inner = getattr(parts[0], "text", None)
            if isinstance(inner, str):
                return inner
    raise ProviderError("Gemini response missing text payload")


def _extract_usage(response: Any, model_name: str) -> ProviderResponse:
    """`usage_metadata` 가 있으면 활용, 없으면 0 으로 채움."""
    usage = getattr(response, "usage_metadata", None)
    tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
    tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)
    return ProviderResponse(
        raw_text=_extract_text(response),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model=model_name,
    )
