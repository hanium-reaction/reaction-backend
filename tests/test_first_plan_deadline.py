"""First Plan 그래프 엣지의 시간·폴백 가드 (planB-4) + 폴백 고지 (planB-5).

LLM 이 느린 날 `/plans/generate` 가 분해(최악 3×45초) → 검토(3×45초) → 재분해 → 검토로
4.5~8분을 돌던 경로를 막는다. `should_replan` 은 순수 판정 그대로 두고(M33 하네스가 쓴다)
그래프 엣지(`after_schedule`/`after_review`)만 시간을 본다.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from reaction_backend.orchestrator import first_plan, first_plan_adapter
from reaction_backend.schemas.planning import PlanReview


def _state(**over: Any) -> Any:
    base: dict[str, Any] = {
        "review": None,
        "replan_count": 0,
        "decompose_fallback_reason": None,
        "started_at": time.monotonic(),
    }
    return {**base, **over}


@pytest.mark.parametrize(
    "reason", ["timeout", "budget", "tone_gate", "banned", "rate_limited", "unavailable"]
)
def test_futile_fallbacks_skip_the_review(reason: str) -> None:
    """다시 불러도 같은 결과인 폴백이면 검토를 시작하지 않는다 — 반려→재분해 사슬을 끊는다."""
    assert first_plan.after_schedule(_state(decompose_fallback_reason=reason)) == "done"


@pytest.mark.parametrize("reason", ["validation", "provider_error"])
def test_transient_fallbacks_keep_the_review(reason: str) -> None:
    """일시 오류는 종전대로 검토 → 재분해 기회를 남긴다."""
    assert first_plan.after_schedule(_state(decompose_fallback_reason=reason)) == "review"


def test_llm_plan_is_reviewed_within_the_deadline() -> None:
    assert first_plan.after_schedule(_state()) == "review"


def test_no_new_review_or_replan_after_the_deadline() -> None:
    """이미 오래 걸렸으면 검토도 재분해도 새로 시작하지 않고 지금 계획을 HITL 로 넘긴다."""
    late = time.monotonic() - first_plan._REVIEW_DEADLINE_SECONDS - 1
    assert first_plan.after_schedule(_state(started_at=late)) == "done"
    rejected = PlanReview(approved=False, feedback=["세션이 추상적이에요"])
    assert first_plan.after_review(_state(started_at=late, review=rejected, replan_count=1)) == (
        "approve"
    )
    # 순수 판정은 그대로다 — 오프라인 하네스가 반려 여부를 이걸로 잰다.
    assert first_plan.should_replan(_state(review=rejected, replan_count=1)) == "replan"


def test_rejected_review_within_the_deadline_still_replans() -> None:
    rejected = PlanReview(approved=False, feedback=["세션이 추상적이에요"])
    assert first_plan.after_review(_state(review=rejected, replan_count=1)) == "replan"


def test_ai_source_follows_the_last_decompose_only() -> None:
    """검토 폴백(used_fallback)만으로는 'rule' 이 되지 않는다 (planB-5)."""
    assert first_plan.plan_ai_source(
        {"decompose_fallback_reason": None, "used_fallback": True}
    ) == ("llm")
    assert first_plan.plan_ai_source({"decompose_fallback_reason": "timeout"}) == "rule"


def test_fallback_notice_names_what_is_empty_and_what_to_do() -> None:
    assert first_plan_adapter.decompose_fallback_notice(None) is None
    generic = first_plan_adapter.decompose_fallback_notice("timeout")
    assert generic is not None and "칸만 잡아 뒀어요" in generic and "잠시 뒤" in generic
    budget = first_plan_adapter.decompose_fallback_notice("budget")
    assert budget is not None and "내일" in budget
    for text in (generic, budget):
        assert "오프라인" not in text and "룰" not in text
