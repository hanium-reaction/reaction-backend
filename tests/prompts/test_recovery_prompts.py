"""Recovery 프롬프트 렌더 회귀 (AGENTS.md §6).

`recovery/if_then_proposal` 이 personalize 하려면 route 가 넘기는 변수(선두 전략 정보 포함)가
템플릿 `{{var}}` 와 정확히 일치해야 한다. 누락되면 PromptRenderError → 조용히 룰 fallback
(카탈로그 템플릿)으로 빠지므로 — 그 은폐를 여기서 잡는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import reaction_backend
from reaction_backend.prompts import registry
from reaction_backend.prompts.registry import PromptRenderError

_DIR = Path(reaction_backend.__file__).parent / "prompts" / "recovery"

# route(api/routes/recovery.py) 의 generate 가 넘기는 변수 집합과 동기화.
CODE_VARS: dict[str, set[str]] = {
    "recovery/if_then_proposal": {
        "strategy_label",
        "strategy_group",
        "base_template",
        "failure_type",
        "confidence",
        "interruption_summary",
        "context_summary",
    },
}
_FILES = {"recovery/if_then_proposal": "if_then_proposal.v1.md"}
_PH = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _placeholders(pid: str) -> set[str]:
    return set(_PH.findall((_DIR / _FILES[pid]).read_text(encoding="utf-8")))


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_placeholders_match_code_variables(prompt_id: str) -> None:
    assert _placeholders(prompt_id) == CODE_VARS[prompt_id]


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_renders_without_missing_variables(prompt_id: str) -> None:
    text, _ = registry.render(prompt_id, dict.fromkeys(CODE_VARS[prompt_id], "x"))
    assert text.strip()
    assert "{{" not in text


def test_missing_variable_raises() -> None:
    with pytest.raises(PromptRenderError):
        registry.render("recovery/if_then_proposal", {})
