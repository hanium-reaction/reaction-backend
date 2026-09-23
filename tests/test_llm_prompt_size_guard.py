"""아주 긴 입력 한 번이 예산을 통째로 넘기지 못한다 (llm-5).

예전 예산 가드는 호출 **전** "지금까지 쓴 양" 만 봤다(`projected_tokens=0`). 사용 0 인
사용자가 수십만 자를 보내면 그 한 번이 가드를 통과해 사용자별 한도를 통째로 넘겼고
(실측: 인박스 3만 자 1건 = tokens_in 21,179, 보통 ~185), 몇 명이면 전역 한도가 바닥나 그날
모든 사용자의 AI 가 조용히 룰로 떨어졌다. 이제는
- 이번 프롬프트의 입력 토큰 추정치를 **미리 더해서** 검사하고,
- 호출 1회 프롬프트 상한(`llm_max_prompt_chars`)을 넘으면 provider 를 부르지 않는다.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from reaction_backend.config import get_settings
from reaction_backend.llm import aiClient
from reaction_backend.llm.provider import ProviderResponse
from reaction_backend.safety import llm_budget
from reaction_backend.safety.llm_budget import BudgetExceeded, estimate_prompt_tokens


class _Out(BaseModel):
    text: str


class _FakeSession:
    """`llm_runs` 기록만 받아 두는 세션 대역 — 예산 조회는 monkeypatch 로 대신한다."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


@pytest.fixture
def provider_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def fake(**kwargs: Any) -> tuple[_Out, ProviderResponse]:
        calls.append(len(str(kwargs.get("prompt_text", ""))))
        return _Out(text="좋아요"), ProviderResponse(
            raw_text="{}", tokens_in=10, tokens_out=5, model="fake"
        )

    monkeypatch.setattr("reaction_backend.llm.tool_executor.generate_structured", fake)
    return calls


@pytest.fixture
def used_today(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    usage = {"user": 0, "global": 0}

    async def user_used(session: Any, *, user_id: Any) -> int:
        return usage["user"]

    async def global_used(session: Any) -> int:
        return usage["global"]

    monkeypatch.setattr(llm_budget, "_used_tokens_today", user_used)
    monkeypatch.setattr(llm_budget, "_used_tokens_today_global", global_used)
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_daily_token_budget", 200_000, raising=False)
    monkeypatch.setattr(settings, "llm_global_daily_token_budget", 2_000_000, raising=False)
    return usage


def test_prompt_token_estimate_rounds_up_from_two_thirds_of_the_length() -> None:
    assert estimate_prompt_tokens("") == 0
    assert estimate_prompt_tokens("가") == 1
    assert estimate_prompt_tokens("가" * 30_000) == 20_000  # 실측 21,179 에 가까운 쪽


async def test_budget_check_counts_the_upcoming_prompt(used_today: dict[str, int]) -> None:
    used_today["user"] = 150_000

    await llm_budget.check(_FakeSession(), user_id=None, projected_tokens=40_000)  # type: ignore[arg-type]
    with pytest.raises(BudgetExceeded):
        await llm_budget.check(_FakeSession(), user_id=None, projected_tokens=60_000)  # type: ignore[arg-type]


async def test_one_oversized_call_cannot_overshoot_the_user_budget(
    monkeypatch: pytest.MonkeyPatch, provider_calls: list[int], used_today: dict[str, int]
) -> None:
    """사용 15만 + 이번 프롬프트 ~6만 토큰 > 20만 → provider 를 부르지 않고 룰 폴백."""
    monkeypatch.setattr(get_settings(), "llm_max_prompt_chars", 0, raising=False)  # 상한 끔
    used_today["user"] = 150_000
    session = _FakeSession()

    result = await aiClient.run(
        module="inbox",
        schema=_Out,
        prompt_id="inbox/classify",
        fallback=lambda: _Out(text="룰"),
        variables={"raw_text": "가" * 90_000},
        session=session,  # type: ignore[arg-type]
        timeout=1.0,
    )

    assert result.fell_back is True
    assert result.reason == "budget"
    assert provider_calls == []
    assert [r.reason for r in session.added] == ["budget"]


async def test_normal_call_still_passes_with_the_projection(
    provider_calls: list[int], used_today: dict[str, int]
) -> None:
    used_today["user"] = 150_000

    result = await aiClient.run(
        module="inbox",
        schema=_Out,
        prompt_id="inbox/classify",
        fallback=lambda: _Out(text="룰"),
        variables={"raw_text": "토익 단어 30개 외우기"},
        session=_FakeSession(),  # type: ignore[arg-type]
        timeout=1.0,
    )

    assert result.fell_back is False
    assert len(provider_calls) == 1


async def test_prompt_over_the_per_call_ceiling_is_never_sent(
    provider_calls: list[int],
) -> None:
    """예산이 넉넉해도(세션 없음 = 예산 조회 없음) 한 번에 보낼 수 있는 크기는 정해져 있다."""
    limit = get_settings().llm_max_prompt_chars
    assert limit > 0

    result = await aiClient.run(
        module="inbox",
        schema=_Out,
        prompt_id="inbox/classify",
        fallback=lambda: _Out(text="룰"),
        variables={"raw_text": "가" * (limit + 1)},
        timeout=1.0,
    )

    assert result.fell_back is True
    assert result.reason == "budget"
    assert provider_calls == []


async def test_grounded_call_over_the_ceiling_is_discarded_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(**kwargs: Any) -> Any:
        raise AssertionError("provider must not be called")

    monkeypatch.setattr("reaction_backend.llm.tool_executor.generate_grounded_text", fail)
    limit = get_settings().llm_max_prompt_chars

    result = await aiClient.run_grounded(
        "planning",
        "planning/materials_search",
        variables={"query": "가" * (limit + 1)},
    )

    assert result.text is None
    assert result.reason == "budget"
    assert result.grounding_requests == 0
