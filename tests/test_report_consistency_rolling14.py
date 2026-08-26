"""연속성 지표(`consistency_rolling14`) 리포트 판정 고정 (근거 대장 §7.1/§7.2, C1/C2).

핵심 판정 함수(`_consistency_rate`)만 고정한다 — 특히 창(14일)을 넘는 날수가 들어와도
100% 를 넘기지 않는지(옛 `_longest_streak` 처럼 무한정 커지는 지표가 아님을 보장).
"""

from __future__ import annotations

from scripts.report_consistency_rolling14 import _WINDOW_DAYS, _consistency_rate


def test_zero_qualifying_days_is_zero() -> None:
    assert _consistency_rate(0) == 0.0


def test_full_window_is_one() -> None:
    assert _consistency_rate(_WINDOW_DAYS) == 1.0


def test_half_window_is_half() -> None:
    assert _consistency_rate(7) == 0.5


def test_more_than_window_days_is_capped_at_one() -> None:
    """윈도우보다 많은 '날'이 들어올 수 없어야 정상이지만, 방어적으로 100% 를 넘기지 않는다."""
    assert _consistency_rate(20) == 1.0


def test_custom_window_size() -> None:
    assert _consistency_rate(3, window_days=6) == 0.5
