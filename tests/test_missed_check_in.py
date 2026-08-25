"""블록 후 미체크 판정 — 단일 진실 소스 고정 (근거 대장 §6.2 T1, reaction-frontend#224).

`GET /today/agenda` 의 `missedCheckIn` 과 이 판정이 갈라지면 FE 가 배지를 잘못 그린다
— `action_cancel.py`(#214)와 같은 이유로 판정을 여기 하나로 고정한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reaction_backend.domain.missed_check_in import MISSED_CHECK_IN_DELAY, is_missed_check_in

_START = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)


def test_scheduled_and_past_the_delay_is_missed() -> None:
    now = _START + MISSED_CHECK_IN_DELAY
    assert is_missed_check_in(block_status="scheduled", start_at=_START, now=now) is True


def test_scheduled_but_still_within_the_delay_is_not_missed() -> None:
    now = _START + MISSED_CHECK_IN_DELAY - timedelta(minutes=1)
    assert is_missed_check_in(block_status="scheduled", start_at=_START, now=now) is False


def test_started_block_is_never_missed() -> None:
    """이미 [▶ 시작] 을 눌렀다 — 아무리 시간이 지나도 '미체크' 가 아니다."""
    now = _START + timedelta(days=1)
    assert is_missed_check_in(block_status="started", start_at=_START, now=now) is False


def test_finished_block_is_never_missed() -> None:
    now = _START + timedelta(hours=1)
    assert is_missed_check_in(block_status="finished", start_at=_START, now=now) is False


def test_cancelled_block_is_never_missed() -> None:
    now = _START + timedelta(hours=1)
    assert is_missed_check_in(block_status="cancelled", start_at=_START, now=now) is False


def test_boundary_exactly_at_the_delay_counts_as_missed() -> None:
    """경계는 포함(`>=`) — 좁히는 뮤턴트를 잡는다."""
    now = _START + MISSED_CHECK_IN_DELAY
    assert is_missed_check_in(block_status="scheduled", start_at=_START, now=now) is True


def test_future_block_is_not_missed() -> None:
    """아직 시작 시각도 안 된 블록 — 당연히 미체크가 아니다."""
    now = _START - timedelta(minutes=5)
    assert is_missed_check_in(block_status="scheduled", start_at=_START, now=now) is False
