"""부담 지표(`burden_index`) 리포트 판정 고정 (근거 대장 §7.2, E6).

핵심 판정 함수(`_rejection_rate`/`_reflection_non_response_rate`)만 고정한다.
"""

from __future__ import annotations

from scripts.report_burden_index import _reflection_non_response_rate, _rejection_rate


def test_rejection_rate_counts_rejected_and_skipped() -> None:
    n, total, rate = _rejection_rate(["accepted", "rejected", "skipped", "edited"])
    assert (n, total, rate) == (2, 4, 0.5)


def test_rejection_rate_with_no_decisions_is_zero_not_a_division_error() -> None:
    assert _rejection_rate([]) == (0, 0, 0.0)


def test_rejection_rate_all_accepted_is_zero() -> None:
    assert _rejection_rate(["accepted", "edited"]) == (0, 2, 0.0)


def test_reflection_non_response_rate_counts_only_the_expiry_reason() -> None:
    n, total, rate = _reflection_non_response_rate(
        ["reflection_skipped", None, "cancelled_by_replan", "reflection_skipped"]
    )
    assert (n, total, rate) == (2, 4, 0.5)


def test_reflection_non_response_rate_with_no_reflectable_executions_is_zero() -> None:
    assert _reflection_non_response_rate([]) == (0, 0, 0.0)


def test_reflection_non_response_rate_all_responded_is_zero() -> None:
    assert _reflection_non_response_rate([None, None]) == (0, 2, 0.0)
