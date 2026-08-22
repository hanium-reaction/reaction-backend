"""검색 그라운딩 진입점 (#259 §4.2 ⑥).

이 파일이 지키는 것은 하나로 요약된다: **모델이 확인하지 않은 목차를 계획에 넣지 않는다.**

#259 §2 실측에서 나온 두 가지 사실이 설계 전체를 결정했다.

1. `response_schema` 를 붙이면 **검색이 조용히 빠진다.** 에러가 없어서 알아챌 수 없다
   (5/5 회 재현). 그래서 `generate_structured` 를 못 쓰고 별도 함수가 필요하다.
2. 그 상태로 **존재하지 않는 교재를 물으면 5챕터 목차를 자신 있게 만들어낸다** — 출처 0,
   에러 0. 그래서 "출처 개수" 가 유일하게 믿을 만한 통과 조건이다.

두 사실 모두 "정상처럼 보이는 실패" 라서, 테스트가 없으면 회귀해도 초록불이 켜진다.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.models.user import User
from reaction_backend.llm import provider, tool_executor
from reaction_backend.llm.provider import (
    GroundingSource,
    ProviderError,
    ProviderUnavailable,
    generate_grounded_text,
)

# 없는 교재를 물었을 때 모델이 실제로 만들어낸 류의 응답 — 그럴듯해서 눈으로는 못 거른다.
_CONFIDENT_FABRICATION = """제1장 기초 다지기
제2장 문형 익히기
제3장 실전 연습
제4장 모의고사
제5장 마무리"""

_REAL_TOC = """섹션 8. 자바 메모리 구조와 static
섹션 9. final"""


class _Tmpl:
    prompt_id = "planning/materials_search"
    version = "1"


# ──────────────────────── provider 레벨 ────────────────────────


class _FakeWeb:
    def __init__(self, title: str, uri: str) -> None:
        self.title = title
        self.uri = uri


class _FakeChunk:
    def __init__(self, web: _FakeWeb | None) -> None:
        self.web = web


class _FakeMeta:
    def __init__(self, chunks: list[_FakeChunk], queries: list[str]) -> None:
        self.grounding_chunks = chunks
        self.web_search_queries = queries


class _FakeCandidate:
    def __init__(self, meta: _FakeMeta | None) -> None:
        self.grounding_metadata = meta


class _FakeUsage:
    prompt_token_count = 17
    candidates_token_count = 1_263
    thoughts_token_count = 0


class _FakeResponse:
    def __init__(self, text: str, meta: _FakeMeta | None) -> None:
        self.text = text
        self.candidates = [_FakeCandidate(meta)]
        self.usage_metadata = _FakeUsage()
        self.model_version = "gemini-3.5-flash-lite"


def _patch_provider_client(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse
) -> dict[str, Any]:
    """`google.genai` 클라이언트를 가로채고 넘어간 config 를 캡처한다."""
    captured: dict[str, Any] = {}

    class _Models:
        async def generate_content(self, *, model: str, contents: str, config: Any) -> Any:
            captured["model"] = model
            captured["contents"] = contents
            captured["config"] = config
            return response

    class _Aio:
        models = _Models()

    class _Client:
        aio = _Aio()

    monkeypatch.setattr(provider, "_get_client", lambda: _Client())
    # SDK 미설치 환경에서도 돌아야 한다 — 툴 객체는 모양만 있으면 된다.
    monkeypatch.setattr(provider, "_search_tool", lambda: {"google_search": {}})
    return captured


async def test_grounded_call_never_sends_a_response_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**이 파일의 첫 번째 핵심.**

    `response_schema` 가 config 에 다시 섞여 들어오면 검색이 조용히 꺼진다 — 에러도, 빈
    응답도 없이 JSON 만 잘 나온다(#259 §2, 5/5 회). 눈으로도 로그로도 안 보이는 회귀라
    여기서 못 박는다. 반대로 `tools` 는 반드시 있어야 한다.
    """
    captured = _patch_provider_client(
        monkeypatch, _FakeResponse(_REAL_TOC, _FakeMeta([], ["자바 기본편 커리큘럼"]))
    )
    await generate_grounded_text(prompt_text="목차 알려줘", timeout=20.0, model="x-lite")

    config = captured["config"]
    assert "response_schema" not in config
    assert "response_mime_type" not in config
    assert config["tools"], "검색 툴이 빠지면 그냥 일반 호출이다"


async def test_sources_and_queries_come_from_grounding_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = _FakeMeta(
        [
            _FakeChunk(_FakeWeb("인프런 강의", "https://inflearn.com/course/x")),
            _FakeChunk(_FakeWeb("블로그 정리", "https://example.com/toc")),
        ],
        ["김영한 실전 자바 기본편 커리큘럼"],
    )
    _patch_provider_client(monkeypatch, _FakeResponse(_REAL_TOC, meta))

    resp = await generate_grounded_text(prompt_text="q", timeout=20.0, model="x-lite")

    assert resp.sources == (
        GroundingSource(title="인프런 강의", uri="https://inflearn.com/course/x"),
        GroundingSource(title="블로그 정리", uri="https://example.com/toc"),
    )
    assert resp.search_queries == ("김영한 실전 자바 기본편 커리큘럼",)
    assert resp.tokens_in == 17


async def test_missing_or_malformed_metadata_reads_as_zero_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 가 트리 모양을 바꿔도 **터지지 않고 0 건**으로 읽혀야 한다.

    여기서 AttributeError 가 나면 그라운딩이 실제로 됐는데도 호출이 실패로 끝난다.
    `web` 이 없는 chunk(예: retrieved_context)는 조용히 건너뛴다.
    """
    _patch_provider_client(monkeypatch, _FakeResponse(_REAL_TOC, _FakeMeta([_FakeChunk(None)], [])))
    assert (await generate_grounded_text(prompt_text="q", timeout=20.0)).sources == ()

    _patch_provider_client(monkeypatch, _FakeResponse(_REAL_TOC, None))
    assert (await generate_grounded_text(prompt_text="q", timeout=20.0)).sources == ()


# ──────────────────────── 게이트(tool_executor) 레벨 ────────────────────────


def _patch_executor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: str = _REAL_TOC,
    sources: tuple[GroundingSource, ...] = (GroundingSource("인프런", "https://inflearn.com/x"),),
    raises: Exception | None = None,
) -> dict[str, Any]:
    """`generate_grounded_text` 를 가로채고 호출 횟수·인자를 센다."""
    calls: dict[str, Any] = {"count": 0}

    monkeypatch.setattr(
        tool_executor.prompt_registry, "render", lambda pid, variables: ("렌더된 프롬프트", _Tmpl())
    )

    async def _fake(*, prompt_text: str, timeout: float, model: str | None = None, **_: Any) -> Any:
        calls["count"] += 1
        calls["timeout"] = timeout
        calls["model"] = model
        if raises is not None:
            raise raises
        return provider.GroundedResponse(
            text=text,
            sources=sources,
            search_queries=("q",),
            tokens_in=17,
            tokens_out=1_263,
            model="gemini-3.5-flash-lite",
        )

    monkeypatch.setattr(tool_executor, "generate_grounded_text", _fake)
    return calls


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="그라운딩 진입점 테스트"))
    await session.flush()
    return user_id


async def _grounding_rows(session: AsyncSession, user_id: uuid.UUID) -> tuple[int, int]:
    """(행 수, grounding_requests 합)."""
    result = await session.execute(
        select(func.count(LlmRun.id), func.coalesce(func.sum(LlmRun.grounding_requests), 0)).where(
            LlmRun.user_id == user_id
        )
    )
    rows, total = result.one()
    return int(rows), int(total)


async def test_zero_sources_discards_the_text_even_when_it_looks_perfect(
    monkeypatch: pytest.MonkeyPatch, real_db_session: AsyncSession
) -> None:
    """**이 파일의 두 번째 핵심.**

    출처가 0 건이면 응답이 아무리 그럴듯해도 버린다. 실측에서 없는 교재에 대해 나온 게
    정확히 이 모양이었다 — 5챕터짜리 완성된 목차, 에러 없음, 출처 없음. 이 가드가 빠지면
    사용자는 존재하지 않는 챕터를 공부하려다 자기가 못 찾는 줄 안다.
    """
    _patch_executor(monkeypatch, text=_CONFIDENT_FABRICATION, sources=())
    user_id = await _seed_user(real_db_session)

    result = await tool_executor.aiClient.run_grounded(
        "planning", "planning/materials_search", user_id=user_id, session=real_db_session
    )

    assert result.text is None
    assert result.usable is False
    assert result.reason == "ungrounded"
    assert _CONFIDENT_FABRICATION not in str(result.text)


async def test_grounded_text_passes_through_with_its_sources(
    monkeypatch: pytest.MonkeyPatch, real_db_session: AsyncSession
) -> None:
    src = (GroundingSource("인프런 강의", "https://inflearn.com/course/x"),)
    _patch_executor(monkeypatch, text=_REAL_TOC, sources=src)
    user_id = await _seed_user(real_db_session)

    result = await tool_executor.aiClient.run_grounded(
        "planning", "planning/materials_search", user_id=user_id, session=real_db_session
    )

    assert result.text == _REAL_TOC
    assert result.usable is True
    assert result.reason is None
    # 출처는 사용자 고지용(⑩) — 여기서 사라지면 "이 자료를 참고했어요" 를 보여줄 수 없다.
    assert result.sources == src


async def test_it_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, real_db_session: AsyncSession
) -> None:
    """그라운딩은 **건수로 과금**된다 — `run()` 처럼 3회 재시도하면 비용이 조용히 3배 된다.

    실패의 대가는 "자료 없음" 뿐이고 그건 이미 정상 경로라, 재시도가 사는 값보다 비싸다.
    """
    calls = _patch_executor(monkeypatch, raises=ProviderError("500 boom"))
    user_id = await _seed_user(real_db_session)

    result = await tool_executor.aiClient.run_grounded(
        "planning", "planning/materials_search", user_id=user_id, session=real_db_session
    )

    assert calls["count"] == 1, f"재시도가 생겼다: {calls['count']}회"
    assert result.reason == "provider_error"
    assert result.text is None
    assert get_settings().llm_max_retries > 1, "이 테스트의 전제 — run() 은 재시도한다"


async def test_grounding_budget_blocks_before_the_provider_is_called(
    monkeypatch: pytest.MonkeyPatch, real_db_session: AsyncSession
) -> None:
    """예산 초과면 **호출 자체를 안 한다.** 호출하고 버리면 가드가 돈을 못 막는다."""
    calls = _patch_executor(monkeypatch)
    user_id = await _seed_user(real_db_session)
    limit = get_settings().llm_daily_grounding_budget
    real_db_session.add(
        LlmRun(
            user_id=user_id,
            module="planning",
            model="gemini-3.5-flash-lite",
            prompt_id="planning/materials_search",
            prompt_version="1",
            tokens_in=17,
            tokens_out=1_263,
            latency_ms=8_500,
            cost_cents=0,
            cost_micro_usd=3_200,
            grounding_requests=limit,
            success=True,
            fell_back=False,
        )
    )
    await real_db_session.flush()

    result = await tool_executor.aiClient.run_grounded(
        "planning", "planning/materials_search", user_id=user_id, session=real_db_session
    )

    assert calls["count"] == 0
    assert result.reason == "grounding_budget"
    assert result.grounding_requests == 0


async def test_every_dispatched_request_is_counted_even_when_discarded(
    monkeypatch: pytest.MonkeyPatch, real_db_session: AsyncSession
) -> None:
    """자료를 버려도 **요청은 나갔다** — 세지 않으면 예산 가드에 구멍이 난다.

    과소 계수가 위험한 방향이라(청구는 이미 발생) 보수적으로 센다.
    """
    _patch_executor(monkeypatch, text=_CONFIDENT_FABRICATION, sources=())
    user_id = await _seed_user(real_db_session)

    result = await tool_executor.aiClient.run_grounded(
        "planning", "planning/materials_search", user_id=user_id, session=real_db_session
    )

    assert result.grounding_requests == 1
    rows, total = await _grounding_rows(real_db_session, user_id)
    assert rows == 1, "폐기도 llm_runs 에 남아야 한다 — 폐기율이 트리거 설계의 신호다"
    assert total == 1


async def test_a_request_that_never_left_is_not_counted(
    monkeypatch: pytest.MonkeyPatch, real_db_session: AsyncSession
) -> None:
    """API key 누락은 요청이 **나가지 않았다** — 여기까지 세면 가드가 헛돈다."""
    _patch_executor(monkeypatch, raises=ProviderUnavailable("no key (test)"))
    user_id = await _seed_user(real_db_session)

    result = await tool_executor.aiClient.run_grounded(
        "planning", "planning/materials_search", user_id=user_id, session=real_db_session
    )

    assert result.reason == "unavailable"
    assert result.grounding_requests == 0
    _, total = await _grounding_rows(real_db_session, user_id)
    assert total == 0


async def test_material_text_is_reported_but_never_rewritten(
    monkeypatch: pytest.MonkeyPatch, real_db_session: AsyncSession
) -> None:
    """금지어 필터를 **자료 원문에 적용하지 않는다** — 치환하면 없는 챕터를 인용하게 된다.

    `run()` 은 우리가 생성한 문장을 치환하지만(DevBaseline §4.2), 여기 텍스트는 인용한
    외부 자료다. "실패 없는 영어" 를 "한 번 멈춤 없는 영어" 로 바꿔 놓고 그 챕터를 계획에
    넣으면, 이 기능이 막으려는 실패(존재하지 않는 항목을 사실인 양 제시)를 우리 손으로
    만드는 셈이다. 대신 **발견 사실은 올린다** — 표시 계층이 판단할 수 있게.

    사용자에게 실제로 나가는 계획 문장은 `run()` 경로가 그대로 필터링한다.
    """
    quoted = "제3장 실패 없는 영어 학습법"
    _patch_executor(monkeypatch, text=quoted)
    user_id = await _seed_user(real_db_session)

    result = await tool_executor.aiClient.run_grounded(
        "planning", "planning/materials_search", user_id=user_id, session=real_db_session
    )

    assert result.text == quoted, "원문이 바뀌었다 — 없는 챕터를 인용하게 된다"
    assert "실패" in result.banned_hits


async def test_default_timeout_is_not_the_frozen_eight_seconds(
    monkeypatch: pytest.MonkeyPatch, real_db_session: AsyncSession
) -> None:
    """동결값 8.0 은 **실측 중앙값 8.5s 보다 짧다** — 그대로 쓰면 절반이 타임아웃난다."""
    calls = _patch_executor(monkeypatch)
    user_id = await _seed_user(real_db_session)

    await tool_executor.aiClient.run_grounded(
        "planning", "planning/materials_search", user_id=user_id, session=real_db_session
    )

    assert calls["timeout"] == get_settings().llm_grounding_timeout_seconds
    assert calls["timeout"] > 8.5, "그라운딩 실측 중앙값보다는 길어야 한다"


async def test_grounding_model_is_pinned_separately_from_planning() -> None:
    """지연 때문에 lite 로 고정한다 — planning 을 상위 모델로 올려도 따라가면 안 된다.

    실측(#259 §2): lite 8.5s / flash 23~80s. 사용자가 화면 앞에서 기다리는 단계다.
    """
    settings = get_settings()
    assert "lite" in settings.llm_model_grounding
    assert settings.llm_model_grounding != "", "빈 값이면 base 를 따라가 결합이 되살아난다"


async def test_missing_prompt_falls_through_without_spending(
    monkeypatch: pytest.MonkeyPatch, real_db_session: AsyncSession
) -> None:
    calls = _patch_executor(monkeypatch)
    monkeypatch.setattr(
        tool_executor.prompt_registry,
        "render",
        lambda pid, variables: (_ for _ in ()).throw(tool_executor.PromptNotFound(pid)),
    )
    user_id = await _seed_user(real_db_session)

    result = await tool_executor.aiClient.run_grounded(
        "planning", "planning/does_not_exist", user_id=user_id, session=real_db_session
    )

    assert calls["count"] == 0
    assert result.reason == "no_prompt"
    assert result.grounding_requests == 0


async def test_materials_search_prompt_exists_and_takes_the_user_query() -> None:
    """프롬프트는 **사용자가 확인·편집한 검색어**만 받는다 (#259 §4.1 ① 결정).

    목표 슬롯을 그대로 질의로 만들면 사용자가 쓴 문장("이혼 준비", "병원 검사")이 외부
    검색으로 나간다. 변수를 `query` 하나로 묶어 두면, 무엇이 나가는지가 호출부에서 한눈에
    보인다.
    """
    import re

    from reaction_backend.prompts import registry

    body = registry.get("planning/materials_search").body
    variables = sorted(set(re.findall(r"\{\{\s*(\w+)\s*\}\}", body)))
    assert variables == ["query"], f"검색어 외 변수가 늘었다: {variables}"
    assert "지어내지 마라" in body
    assert "확인되지 않음" in body
