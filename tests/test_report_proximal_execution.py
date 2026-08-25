"""근접 실행률(`proximal_execution_rate_60m`) 리포트 판정 고정 (근거 대장 §7.2, D3).

핵심 판정 함수(`_had_proximal_execution`)만 고정한다 — 나머지(쿼리 조립)는
`test_report_recovery_followthrough.py` 와 같은 이유로 얇게 둔다(SELECT 뿐, 값 조합
로직이 없다).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.report_proximal_execution import PROXIMAL_WINDOW, _had_proximal_execution

_SENT_AT = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)


def test_start_right_after_sending_counts() -> None:
    assert _had_proximal_execution(_SENT_AT, [_SENT_AT + timedelta(minutes=3)]) is True


def test_start_exactly_at_the_window_boundary_counts() -> None:
    """경계는 포함 — `<=`를 좁히는 뮤턴트를 잡는다."""
    assert _had_proximal_execution(_SENT_AT, [_SENT_AT + PROXIMAL_WINDOW]) is True


def test_start_one_minute_past_the_window_does_not_count() -> None:
    assert (
        _had_proximal_execution(_SENT_AT, [_SENT_AT + PROXIMAL_WINDOW + timedelta(minutes=1)])
        is False
    )


def test_start_before_sending_does_not_count() -> None:
    """알림 전에 이미 시작된 실행은 그 알림 때문이라고 볼 수 없다 — 창은 미래 방향뿐."""
    assert _had_proximal_execution(_SENT_AT, [_SENT_AT - timedelta(minutes=5)]) is False


def test_no_starts_at_all_does_not_count() -> None:
    assert _had_proximal_execution(_SENT_AT, []) is False


def test_any_one_matching_start_is_enough_among_several() -> None:
    """같은 카드에 여러 실행(재시도 등)이 있으면 하나라도 창 안이면 센다."""
    starts = [
        _SENT_AT - timedelta(days=1),  # 무관 — 창 밖
        _SENT_AT + timedelta(minutes=45),  # 창 안
        _SENT_AT + timedelta(days=2),  # 무관 — 창 밖
    ]
    assert _had_proximal_execution(_SENT_AT, starts) is True
