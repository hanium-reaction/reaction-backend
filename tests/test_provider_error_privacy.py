"""provider 검증 실패 메시지에 LLM 응답 내용이 실리지 않는다 (llm-8).

이 메시지는 `llm_runs.error`(평문 컬럼)와 로그로 간다. 인터뷰 질문·추출은 학생의 자유서술
답(건강·개인사)을 옮겨 쓰곤 해서, 응답 조각이 실리면 암호화된 input/output 요약을 우회해
평문으로 남는다. 어디가 왜 틀렸는지(필드 경로·오류 종류)만 남아야 한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from reaction_backend.llm import provider
from reaction_backend.llm.provider import ProviderValidationError, generate_structured
from reaction_backend.schemas.mandala import MandalaSubgoalPlan

_SENTINEL = "SENTINEL_우울증_약_복용_중"


class _Usage:
    prompt_token_count = 10
    candidates_token_count = 5
    thoughts_token_count = 0


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text
        self.candidates: list[Any] = []
        self.usage_metadata = _Usage()
        self.model_version = "fake"


def _client_returning(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    class _Models:
        async def generate_content(self, *, model: str, contents: str, config: Any) -> Any:
            return _Response(text)

    class _Aio:
        models = _Models()

    class _Client:
        aio = _Aio()

    monkeypatch.setattr(provider, "_get_client", lambda: _Client())


async def test_schema_violation_message_names_the_field_but_not_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client_returning(monkeypatch, '{"subgoals": [{"title": "' + _SENTINEL + '"}]}')

    with pytest.raises(ProviderValidationError) as exc:
        await generate_structured(
            schema=MandalaSubgoalPlan, prompt_text="x", timeout=1.0, model="fake"
        )

    message = str(exc.value)
    assert _SENTINEL not in message
    assert "우울증" not in message
    assert "subgoals.0.title:string_too_long" in message


async def test_non_json_message_does_not_echo_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client_returning(monkeypatch, f"죄송해요, {_SENTINEL} 에 대해서는")

    with pytest.raises(ProviderValidationError) as exc:
        await generate_structured(
            schema=MandalaSubgoalPlan, prompt_text="x", timeout=1.0, model="fake"
        )

    message = str(exc.value)
    assert _SENTINEL not in message
    assert message.startswith("non-JSON response (len=")
