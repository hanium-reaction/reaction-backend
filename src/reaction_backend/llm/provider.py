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


class ProviderRecitationBlocked(ProviderError):
    """Google 이 **저작권 낭송**(`finish_reason=RECITATION`)으로 응답을 통째로 막았다.

    라이브 실측(2026-08-23): 상업 교재 목차(해커스 토익 RC)를 그라운딩으로 물으면 간헐적으로
    이걸로 막힌다. 인프런 강의 커리큘럼은 4/4 로 통과했다 — **강의는 되고 상업 출판물은
    안 된다**는 경계가 우리 정책이 아니라 provider 쪽에서도 그어져 있다.

    일반 실패와 갈라 두는 이유는 사용자 안내가 완전히 다르기 때문이다: "잠시 후 다시" 가
    아니라 "이 자료는 저작권 때문에 가져올 수 없다" 이고, 재시도해도 소용없다.
    """


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
    """호출별 thinking 예산 → Gemini `thinking_config` (None = 설정을 아예 넘기지 않음).

    정책: **예산을 명시하지 않은 호출은 thinking 을 쓰지 않는다.** 분류·짧은 구조화 출력에
    thinking 은 품질 이득 대비 지연 손해가 크고, 그 지연이 agent lock 점유를 늘려 동시성
    충돌을 유발한다(#76). 추론이 필요한 호출(계획 분해·검토)만 예산을 명시한다.

    문제는 "thinking 을 쓰지 않는다" 를 표현하는 방법이 **모델군마다 다르다**는 것이다.
    실측(2026-07-30):

        모델                     예산 미지정      예산 0
        gemini-3.5-flash         사고 479 발생    OK (사고 0)
        gemini-3.5-flash-lite    사고 0           400 INVALID_ARGUMENT
        gemini-pro-latest        사고 243 발생    400 "Budget 0 is invalid"

    중간 티어 flash 만 0 을 받아들인다. lite 는 기본이 이미 비활성이라 0 이 무의미해 거부하고,
    pro 는 **thinking 을 끌 수 없다**(기본 활성 + 0 거부) — pro 로 올리면 모든 호출이 사고
    요금을 물게 되므로 도입 전에 비용을 다시 계산해야 한다. 현재 pro 는 쓰지 않는다.

    이전 구현은 `"2.5-flash" in model_name` 으로 판정했는데, 모델을 `-latest` alias 로 옮긴
    뒤 **어떤 모델에도 매칭되지 않는 죽은 가드**가 됐다. 그 사이 alias 가 `gemini-3.6-flash`
    로 올라가 예산 미지정 호출이 전부 기본 thinking 을 태웠고, 사고 토큰은 출력 요금으로
    과금되면서 기록에는 남지 않았다. 모델명 문자열 판정이 근본적으로 깨지기 쉬우므로
    **고정 모델**과 짝지어 쓰고, 아래 테스트로 두 계열을 모두 잠근다.

    `budget=0` 과 `budget=None` 은 **같은 의도**(thinking 끄기)라 같은 경로로 보낸다. 0 을
    그대로 넘기면 lite/pro 가 400 으로 거부하는데, 호출부는 "이 작업엔 thinking 이 불필요"
    를 표현했을 뿐 모델별 와이어 포맷까지 알 이유가 없다. 그 변환이 이 함수의 역할이다.
    (이 구분이 없으면 호출부에 모델 지식이 새고, 모델을 바꾸는 순간 조용히 깨진다 — 실제로
    `recovery` 라우트가 `thinking_budget=0` 을 하드코딩하고 있어서, 회복 모델을 lite 로
    내리는 순간 전 호출이 400 → 룰 폴백이 될 뻔했다. 화면엔 템플릿 카드가 나오므로
    에러로 보이지도 않는다.)
    """
    if thinking_budget is not None and thinking_budget > 0:
        return {"thinking_budget": thinking_budget}
    if _rejects_zero_thinking(model_name):
        return None
    return {"thinking_budget": 0}


def _rejects_zero_thinking(model_name: str) -> bool:
    """`thinking_budget=0` 을 400 으로 거부하는 모델인가.

    lite(기본이 이미 비활성) · pro(끌 수 없음) 둘 다 거부한다. 중간 티어 flash 만 받는다.
    새 모델을 도입할 때는 **실제로 호출해 보고** 이 판정을 갱신해야 한다 — 문서가 아니라
    응답이 진실이다. 잘못 판정하면 전 호출이 400 → 조용한 룰 폴백이 된다(화면엔 결과가
    나오므로 에러로 보이지 않는다).
    """
    return "lite" in model_name or "pro" in model_name


async def generate_structured[T: BaseModel](
    *,
    schema: type[T],
    prompt_text: str,
    timeout: float,
    thinking_budget: int | None = None,
    model: str | None = None,
) -> tuple[T, ProviderResponse]:
    """Gemini 한 번 호출 → schema 인스턴스로 검증.

    - timeout 은 호출자(`tool_executor`)가 asyncio.wait_for 로 래핑.
    - Structured Output 은 Gemini 의 `response_schema` 기능을 활용,
      그래도 모델이 schema 를 어기면 `ProviderValidationError`.
    - thinking_budget 은 호출별 thinking 예산(`_thinking_config`). None 이면 모델 기본 정책.
    - model 은 task 별 모델 오버라이드(`tool_executor` 가 module→model 로 결정). None 이면 base.
    """
    client = _get_client()
    model_name = model or get_settings().llm_model

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
    """`google-genai` 응답에서 텍스트 페이로드 추출. SDK 버전 차이 흡수.

    **전 part 를 이어 붙인다.** 이전엔 `parts[0].text` 만 읽었는데, 그라운딩 응답은 텍스트가
    여러 part 로 쪼개져 오고 **첫 part 가 비어 있을 수 있다**. 그때 `response.text` 도
    None 이면 본문이 멀쩡히 있는데도 "missing text payload" 로 죽었다 — 라이브 검증에서
    같은 질의 3회 중 1회 재현됐다(2026-08-23). `generate_structured` 도 이 함수를 쓰므로
    구조화 호출에도 같은 간헐적 실패가 있었을 것이다.

    끝까지 텍스트가 없으면 `finish_reason` 을 에러에 실어 준다. 이게 없으면 "왜 없는지"
    (MAX_TOKENS 인지 SAFETY 인지)를 로그만 보고는 알 수 없다.
    """
    text = getattr(response, "text", None)
    if isinstance(text, str) and text:
        return text

    candidates = getattr(response, "candidates", None) or []
    if candidates:
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        chunks = [
            part_text
            for part_text in (getattr(part, "text", None) for part in parts)
            if isinstance(part_text, str) and part_text
        ]
        if chunks:
            return "".join(chunks)

    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    if "RECITATION" in str(finish).upper():
        raise ProviderRecitationBlocked(f"blocked by recitation filter (finish_reason={finish})")
    raise ProviderError(f"Gemini response missing text payload (finish_reason={finish})")


def _extract_usage(response: Any, model_name: str) -> ProviderResponse:
    """`usage_metadata` 가 있으면 활용, 없으면 0 으로 채움.

    `tokens_out` 은 **보이는 출력 + 사고(thinking) 토큰**이다. Gemini 는 셋을 따로 주는데
    (`candidates_token_count` / `thoughts_token_count`), **사고 토큰도 출력 요금으로 과금된다.**
    이전엔 `candidates` 만 셌다 — 실측한 계획 분해 1회에서 보이는 출력 4,118 · 사고 1,990 으로
    **33% 를 놓쳤다.** 그 숫자로 일일 토큰 예산을 판정했으니 비용 상한이 상한이 아니었고,
    청구서와 우리 기록이 어긋나 원인 추적도 안 됐다.

    `model` 은 응답이 알려주는 **실제 모델 버전**을 우선한다. 요청에 쓴 이름이 alias 면
    (`gemini-flash-latest`) 우리 기록만 봐서는 무엇이 돌았는지 알 수 없다 — 실제로 alias 가
    말없이 `gemini-3.6-flash` 로 올라간 것을 자체 기록으로는 발견하지 못했다.
    """
    usage = getattr(response, "usage_metadata", None)
    tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
    visible_out = int(getattr(usage, "candidates_token_count", 0) or 0)
    thoughts = int(getattr(usage, "thoughts_token_count", 0) or 0)
    resolved = getattr(response, "model_version", None)
    return ProviderResponse(
        raw_text=_extract_text(response),
        tokens_in=tokens_in,
        tokens_out=visible_out + thoughts,
        model=str(resolved) if resolved else model_name,
    )


# ═══════════════════════════════════════════════════════════════════
# 검색 그라운딩 진입점 (#259 §4.2 ⑥)
# ═══════════════════════════════════════════════════════════════════
#
# `generate_structured` 를 못 쓰는 이유 — **`response_schema` 를 붙이면 검색이 돌지
# 않는다. 에러도 안 난다.** 조용히 그라운딩만 빠지고 JSON 은 멀쩡히 나온다(#259 §2,
# 5/5 회 재현). 더 나쁜 건, 그 상태로 **존재하지 않는 교재를 물으면 5챕터 목차를 자신
# 있게 지어낸다** — 출처 0, 에러 0. 그래서 schema 없는 별도 함수가 필요하고, 아래
# `_SEARCH_ONLY_CONFIG_KEYS` 테스트로 schema 가 다시 섞여 들어오는 것을 막는다.


@dataclass(frozen=True, slots=True)
class GroundingSource:
    """검색 그라운딩이 실제로 참조한 출처 1건.

    사용자에게 **그대로 고지**할 값이다(#259 §4.2 ⑩). 출처를 숨기고 자료를 쓰면 다른
    판·다른 강의를 가져왔을 때 사용자가 알아챌 방법이 없다.
    """

    title: str
    uri: str


@dataclass(slots=True)
class GroundedResponse:
    """`generate_grounded_text()` 결과 — 텍스트 + 그라운딩 증거."""

    text: str
    sources: tuple[GroundingSource, ...]
    """`grounding_chunks` 에서 뽑은 출처. **비어 있으면 자료를 쓰면 안 된다** (§2)."""
    search_queries: tuple[str, ...]
    """모델이 실제로 던진 검색어. 사용자 고지·디버깅용."""
    tokens_in: int
    tokens_out: int
    model: str


def _search_tool() -> Any:
    """`google_search` 툴 객체 — 늦은 import (SDK 직접 의존은 이 모듈에만)."""
    from google.genai import types  # noqa: PLC0415

    return types.Tool(google_search=types.GoogleSearch())


async def generate_grounded_text(
    *,
    prompt_text: str,
    timeout: float,
    model: str | None = None,
    thinking_budget: int | None = None,
) -> GroundedResponse:
    """Gemini 검색 그라운딩 1회 호출 → **텍스트 + 출처**.

    `generate_structured` 와 나란한 자매 함수지만 **schema 를 절대 붙이지 않는다** (위
    설명). 반환 타입도 다르다 — 이 호출의 산출물은 구조화된 객체가 아니라 "분해
    프롬프트에 그대로 넣을 원문 텍스트" 다(#259 §4.2 ⑤ — 정형화 호출 불필요).

    - timeout 은 `generate_structured` 와 같이 호출자(`tool_executor`)가
      `asyncio.wait_for` 로 래핑한다. 실측 중앙값 8.5s (6.8~9.2) 라 8s 기본값으론 모자란다.
    - 출처가 0 건이어도 **여기서는 에러를 내지 않는다.** 폐기 판단은 정책이라 게이트
      (`tool_executor.run_grounded`)의 몫이다. 이 함수는 관측한 사실만 돌려준다.
    """
    client = _get_client()
    model_name = model or get_settings().llm_model

    config: dict[str, Any] = {"tools": [_search_tool()]}
    tcfg = _thinking_config(model_name, thinking_budget)
    if tcfg is not None:
        config["thinking_config"] = tcfg

    try:
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

    usage = _extract_usage(response, model_name)
    sources, queries = _extract_grounding(response)
    return GroundedResponse(
        text=usage.raw_text,
        sources=sources,
        search_queries=queries,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        model=usage.model,
    )


def _extract_grounding(response: Any) -> tuple[tuple[GroundingSource, ...], tuple[str, ...]]:
    """`candidates[0].grounding_metadata` 에서 출처·검색어 추출.

    metadata 자체가 없는 응답(모델이 검색을 아예 안 돌린 경우)은 **빈 튜플**이다 — 그게
    곧 "그라운딩 안 됨" 신호이고, 게이트가 이 값으로 자료를 폐기한다.

    `getattr` 로 방어적으로 파는 이유는 `_extract_text`/`_extract_usage` 와 같다: SDK 가
    버전마다 이 트리의 모양을 조금씩 바꾼다. 여기서 AttributeError 가 나면 그라운딩이
    됐는데도 0 건으로 읽혀 **자료가 조용히 버려진다**.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return (), ()
    meta = getattr(candidates[0], "grounding_metadata", None)
    if meta is None:
        return (), ()

    queries = tuple(str(q) for q in (getattr(meta, "web_search_queries", None) or []) if q)

    sources: list[GroundingSource] = []
    for chunk in getattr(meta, "grounding_chunks", None) or []:
        web = getattr(chunk, "web", None)
        uri = getattr(web, "uri", None) if web is not None else None
        if not uri:
            continue
        sources.append(GroundingSource(title=str(getattr(web, "title", "") or ""), uri=str(uri)))
    return tuple(sources), queries
