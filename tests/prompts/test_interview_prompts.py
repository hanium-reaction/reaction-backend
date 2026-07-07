"""Interview 프롬프트 렌더 회귀 (AGENTS.md §6 — prompt 변경은 tests/prompts/ 로 보호).

누락 변수는 `PromptRenderError` → tool_executor 가 조용히 룰 fallback 으로 빠진다
(사용자에겐 정상처럼 보임). 그 은폐를 막기 위해:
1. 각 프롬프트의 `{{var}}` 집합이 **코드가 실제로 넘기는 변수 집합과 정확히 일치**하는지
   (템플릿에 코드가 안 주는 변수가 생기면 = 런타임 fallback → 여기서 잡는다).
2. 그 변수 집합으로 렌더하면 예외 없이 모든 치환이 끝나는지.
3. 변수를 빼먹으면 실제로 `PromptRenderError` 가 나는지 (안전망 자체가 살아있는지).

`CODE_VARS` 는 `orchestrator/interview.py` 의 `ask_question`/`validate_answer`/
`summarize_interview` 가 넘기는 variables 와 동기화한다 (바뀌면 여기도 갱신).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import reaction_backend
from reaction_backend.prompts import registry
from reaction_backend.prompts.registry import PromptRenderError

_PROMPTS_DIR = Path(reaction_backend.__file__).parent / "prompts" / "interview"

# 코드가 각 프롬프트에 실제로 넘기는 변수 집합.
CODE_VARS: dict[str, set[str]] = {
    "interview/next_question": {
        "goal_title",
        "turn_index",
        "ambiguous_slot",
        "slot_label",
        "answer_type",
        "options",
        "last_answer",
        "retry",
    },
    "interview/ambiguity_score": {"slot_key", "answer", "answer_type", "options", "today"},
    "interview/summary": {
        "identity",
        "goals",
        "heaviest",
        "deadlines",
        "success_image",
        "time_window",
        "peak_window",
        "no_touch",
        "tone",
        "rest_ok",
        "downscope_unit",
    },
}

_FILES = {
    "interview/next_question": "next_question.v1.md",
    "interview/ambiguity_score": "ambiguity_score.v1.md",
    "interview/summary": "summary.v1.md",
}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _placeholders(prompt_id: str) -> set[str]:
    text = (_PROMPTS_DIR / _FILES[prompt_id]).read_text(encoding="utf-8")
    return set(_PLACEHOLDER_RE.findall(text))


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_placeholders_match_code_variables(prompt_id: str) -> None:
    """템플릿 {{var}} 집합 == 코드가 넘기는 변수 집합 (드리프트 = 런타임 fallback 방지)."""
    assert _placeholders(prompt_id) == CODE_VARS[prompt_id]


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_renders_without_missing_variables(prompt_id: str) -> None:
    """코드 변수 집합으로 렌더하면 예외 없이 모든 {{}} 가 치환된다."""
    text, _tmpl = registry.render(prompt_id, dict.fromkeys(CODE_VARS[prompt_id], "x"))
    assert text.strip()
    assert "{{" not in text  # 남은 미치환 플레이스홀더 없음


def test_missing_variable_raises() -> None:
    """변수 누락 시 PromptRenderError — 안전망(그리고 이 회귀 테스트의 전제)이 살아있는지."""
    with pytest.raises(PromptRenderError):
        registry.render("interview/next_question", {})
