"""금지어 치환으로 길이 상한을 넘어도 500 이 나지 않는다 (llm-3).

치환어는 원어보다 길다('실패'→'한 번 멈춤', +3~7자). 만다라 축(≤10자)·칸(≤16자)처럼 상한에
딱 맞춘 제목이 치환 뒤 넘치면, 예전엔 재검증 `ValidationError` 가 그대로 올라가 만다라
생성·재생성이 500 으로 끝났다(성공 경로와 룰 폴백 경로 둘 다).
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel

from reaction_backend.llm import aiClient
from reaction_backend.llm.provider import ProviderResponse, ProviderUnavailable
from reaction_backend.orchestrator.mandala_adapter import rule_branch_cells
from reaction_backend.prompts import registry
from reaction_backend.schemas.mandala import (
    MandalaCellPlan,
    MandalaSubgoal,
    MandalaSubgoalItem,
    MandalaSubgoalPlan,
)

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _vars(prompt_id: str, **values: str) -> dict[str, str]:
    names = set(_VAR.findall(registry.get(prompt_id).body))
    out = dict.fromkeys(names, "(없음)")
    out.update(values)
    return out


def _provider_returns(monkeypatch: pytest.MonkeyPatch, value: BaseModel) -> None:
    async def fake(**kwargs: object) -> tuple[BaseModel, ProviderResponse]:
        return value, ProviderResponse(raw_text="{}", tokens_in=40, tokens_out=20, model="fake")

    monkeypatch.setattr("reaction_backend.llm.tool_executor.generate_structured", fake)


def _provider_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(**kwargs: object) -> tuple[BaseModel, ProviderResponse]:
        raise ProviderUnavailable("no key")

    monkeypatch.setattr("reaction_backend.llm.tool_executor.generate_structured", fake)


async def test_llm_title_that_overflows_after_substitution_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI 가 쓴 '실패노트 정리'(7자) → '한 번 멈춤노트 정리'(11자 > 10). 500 대신 폴백."""
    _provider_returns(
        monkeypatch,
        MandalaSubgoalPlan(
            subgoals=[MandalaSubgoalItem(title="실패노트 정리"), MandalaSubgoalItem(title="운동")]
        ),
    )

    result = await aiClient.run(
        module="planning",
        schema=MandalaSubgoalPlan,
        prompt_id="planning/mandala_subgoals",
        fallback=lambda: MandalaSubgoalPlan(subgoals=[MandalaSubgoalItem(title="건강 관리")]),
        variables=_vars("planning/mandala_subgoals", statement="창업가 되기"),
        timeout=1.0,
    )

    assert result.fell_back is True
    assert result.reason == "banned"
    assert [s.title for s in result.value.subgoals] == ["건강 관리"]
    # 토큰은 이미 썼다 — 폴백이어도 사용량으로 남는다(예전엔 500 롤백으로 기록이 사라졌다).
    assert (result.tokens_in, result.tokens_out) == (40, 20)
    assert result.banned_hits == ("실패",)


async def test_rule_fallback_that_overflows_after_substitution_is_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """룰 폴백 칸 '실패 원인 분석하기 1단계'(15자) → 치환 18자. 폴백의 폴백은 없으니 잘라서 맞춘다."""
    _provider_down(monkeypatch)
    axis = MandalaSubgoal(order_index=0, title="실패 원인 분석하기")

    result = await aiClient.run(
        module="planning",
        schema=MandalaCellPlan,
        prompt_id="planning/mandala_cells_branch",
        fallback=lambda: rule_branch_cells(axis, []),
        # 축 제목이 이 호출의 입력에 없다 → 사용자 원문 보호(llm-2)가 걸리지 않는 경로.
        variables=_vars("planning/mandala_cells_branch", subgoal="시장 조사"),
        timeout=1.0,
    )

    assert result.fell_back is True
    titles = [c.title for c in result.value.cells]
    assert len(titles) == 8
    assert all(len(t) <= 16 for t in titles)
    assert not any("실패" in t for t in titles)
    assert titles[0].startswith("한 번 멈춤 원인 분석하기")


async def test_rule_fallback_keeps_the_users_axis_title_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사용자가 고친 축이 입력에 있으면 칸 제목은 원문 그대로 — 치환도 잘림도 없다."""
    _provider_down(monkeypatch)
    axis = MandalaSubgoal(order_index=0, title="실패 원인 분석하기")

    result = await aiClient.run(
        module="planning",
        schema=MandalaCellPlan,
        prompt_id="planning/mandala_cells_branch",
        fallback=lambda: rule_branch_cells(axis, []),
        variables=_vars("planning/mandala_cells_branch", subgoal=axis.title),
        timeout=1.0,
    )

    assert [c.title for c in result.value.cells] == [
        f"실패 원인 분석하기 {j}단계" for j in range(1, 9)
    ]
