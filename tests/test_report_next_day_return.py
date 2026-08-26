"""다음날 복귀율(`next_day_return_rate`) 리포트 판정 고정 (근거 대장 §7.3 SQL#3).

핵심 판정 함수(`_is_next_day_return`)만 고정한다 — 나머지(쿼리 조립)는
`test_report_proximal_execution.py` 와 같은 이유로 얇게 둔다.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from scripts.report_next_day_return import _is_next_day_return, _rate

_USER = uuid4()
_OTHER_USER = uuid4()
_DAY = date(2026, 7, 21)


def test_win_the_very_next_day_counts() -> None:
    win_days = {(_USER, date(2026, 7, 22))}
    assert _is_next_day_return(_USER, _DAY, win_days) is True


def test_win_two_days_later_does_not_count() -> None:
    """ "다음날"은 정확히 +1일 — 그 이후는 next_day_return_rate 가 아니다."""
    win_days = {(_USER, date(2026, 7, 23))}
    assert _is_next_day_return(_USER, _DAY, win_days) is False


def test_win_the_same_day_does_not_count() -> None:
    win_days = {(_USER, _DAY)}
    assert _is_next_day_return(_USER, _DAY, win_days) is False


def test_other_users_next_day_win_does_not_count() -> None:
    """다른 사용자의 승리일은 이 사용자의 복귀로 안 샌다 — 사용자 경계가 핵심 회귀 지점."""
    win_days = {(_OTHER_USER, date(2026, 7, 22))}
    assert _is_next_day_return(_USER, _DAY, win_days) is False


def test_no_win_days_at_all_does_not_count() -> None:
    assert _is_next_day_return(_USER, _DAY, set()) is False


def test_rate_computes_hits_over_total() -> None:
    fail_days = {(_USER, _DAY), (_USER, date(2026, 7, 25)), (_OTHER_USER, _DAY)}
    win_days = {(_USER, date(2026, 7, 22)), (_OTHER_USER, date(2026, 7, 22))}
    hits, total, rate = _rate(fail_days, win_days)
    assert total == 3
    assert hits == 2  # _USER 의 7/21 실패→7/22 복귀, _OTHER_USER 의 7/21 실패→7/22 복귀
    assert rate == 2 / 3


def test_rate_with_no_fail_days_is_zero_not_a_division_error() -> None:
    hits, total, rate = _rate(set(), set())
    assert (hits, total, rate) == (0, 0, 0.0)
