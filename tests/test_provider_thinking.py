"""provider._thinking_config — 호출별 thinking 예산 정책 (P1-3).

인터뷰 턴(기본 None)은 flash 에서 thinking 0(비활성, #76 지연/락 경합 회피)을 유지하고,
계획 분해·검토처럼 예산을 명시한 호출만 thinking 을 켠다. API key 없이 순수 함수로 검증.
"""

from __future__ import annotations

from reaction_backend.llm.provider import _thinking_config


def test_flash_default_disables_thinking() -> None:
    """인터뷰 등 기본 호출(None) — flash 는 thinking 0 유지 (기존 동작)."""
    assert _thinking_config("gemini-2.5-flash", None) == {"thinking_budget": 0}


def test_flash_explicit_budget_enables_thinking() -> None:
    """계획 호출이 양수 예산을 넘기면 그대로 적용 → thinking 활성."""
    assert _thinking_config("gemini-2.5-flash", 2048) == {"thinking_budget": 2048}


def test_pro_default_left_untouched() -> None:
    """2.5-pro 는 budget 0 미지원 → 기본(None)에선 thinking_config 없음(SDK 기본)."""
    assert _thinking_config("gemini-2.5-pro", None) is None


def test_explicit_budget_applies_to_any_model() -> None:
    """예산을 명시하면 모델 종류와 무관하게 그 값을 적용한다."""
    assert _thinking_config("gemini-2.5-pro", 512) == {"thinking_budget": 512}
