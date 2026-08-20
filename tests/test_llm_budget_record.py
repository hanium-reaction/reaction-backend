"""llm_runs.reason — fallback 사유 코드 영속 (L1-4 fallback 3분해 선행).

`RunResult.reason`(rate_limited/timeout/validation/budget/banned/unavailable/no_prompt/
provider_error)은 이전엔 로그에만 남고 DB 엔 안 남았다. `llm_runs.error` 는 자유 텍스트라
원인별 집계가 안 됐고, 특히 timeout 은 `str(TimeoutError())` 가 빈 문자열이라 falsy 체크에
걸려 NULL 로 저장됐다 — 가장 필요한 원인이 가장 안 남는 역설이었다.
"""

from __future__ import annotations

from typing import Any

from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.safety.llm_budget import LlmRunRecord, record


class _SpySession:
    """add() 로 넘어온 행을 그대로 잡아둔다 — commit 은 호출자 책임이라 흉내내지 않는다."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _record_kwargs(**overrides: Any) -> LlmRunRecord:
    base: dict[str, Any] = {
        "module": "recovery",
        "model": "gemini-flash-latest",
        "prompt_id": "recovery/if_then_proposal",
        "prompt_version": "2",
        "tokens_in": 0,
        "tokens_out": 0,
        "latency_ms": 10,
        "success": False,
        "fell_back": True,
        "cost_cents": 0,
    }
    base.update(overrides)
    return LlmRunRecord(**base)


async def test_record_persists_reason_on_fallback() -> None:
    session = _SpySession()
    await record(session, _record_kwargs(reason="timeout"))

    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, LlmRun)
    assert row.reason == "timeout"


async def test_record_leaves_reason_null_on_success() -> None:
    """성공 호출은 reason 을 안 넘긴다(기본값 None) — success=True 행은 NULL 이어야 한다."""
    session = _SpySession()
    await record(session, _record_kwargs(success=True, fell_back=False))

    row = session.added[0]
    assert row.reason is None


async def test_record_persists_reason_even_when_error_text_is_falsy() -> None:
    """timeout 처럼 error 가 빈 문자열이라 error 컬럼이 NULL 로 떨어져도 reason 은 살아있다.

    이게 이 컬럼이 존재하는 이유다 — error 하나로는 timeout 을 구분할 방법이 없었다.
    """
    session = _SpySession()
    await record(session, _record_kwargs(reason="timeout", error=""))

    row = session.added[0]
    assert row.error is None  # 기존 동작(빈 문자열 → falsy → None) 그대로
    assert row.reason == "timeout"


async def test_record_persists_each_reason_code_distinctly() -> None:
    """원인별 3분해가 실제로 구분되는지 — 9개 사유 코드가 서로 다른 값으로 저장된다."""
    codes = (
        "rate_limited",
        "timeout",
        "validation",
        "budget",
        "banned",
        "tone_gate",
        "unavailable",
        "no_prompt",
        "provider_error",
    )
    for code in codes:
        session = _SpySession()
        await record(session, _record_kwargs(reason=code))
        assert session.added[0].reason == code
