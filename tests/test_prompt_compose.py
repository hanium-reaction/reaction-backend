"""LLM 톤 prefix 합성 헬퍼 (Issue #23, S23) — 순수 함수 단위 테스트."""

from __future__ import annotations

import pytest

from reaction_backend.llm.prompt_compose import (
    TONE_SYSTEM_PREFIXES,
    compose_system_prompt,
    tone_system_prefix,
)


@pytest.mark.parametrize("tone", ["gentle", "strict", "encouraging"])
def test_tone_prefix_nonempty_for_supported(tone: str) -> None:
    assert tone_system_prefix(tone) == TONE_SYSTEM_PREFIXES[tone]
    assert tone_system_prefix(tone) != ""


@pytest.mark.parametrize("tone", [None, "", "aggressive", "unknown"])
def test_tone_prefix_empty_for_missing_or_unknown(tone: str | None) -> None:
    assert tone_system_prefix(tone) == ""


def test_compose_prepends_prefix() -> None:
    composed = compose_system_prompt("원본 프롬프트", "gentle")
    assert composed.startswith(TONE_SYSTEM_PREFIXES["gentle"])
    assert composed.endswith("원본 프롬프트")
    assert "원본 프롬프트" in composed


def test_compose_no_tone_returns_original() -> None:
    assert compose_system_prompt("원본 프롬프트", None) == "원본 프롬프트"


def test_compose_unknown_tone_returns_original() -> None:
    assert compose_system_prompt("원본 프롬프트", "aggressive") == "원본 프롬프트"
