"""예정 블록 × 캘린더 일정 겹침 판정 (`domain/calendar_conflict.py`).

화면(오늘·주간)과 아침 브리프가 같은 판정을 쓴다 — 규칙이 두 곳에서 따로 자라면 화면에는
배지가 있는데 브리프는 조용한 식으로 어긋난다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from reaction_backend.domain.calendar_conflict import conflicting_keys, overlaps
from reaction_backend.schemas.common import KST

_NOW = datetime(2026, 9, 17, 9, 0, tzinfo=KST)


def _at(hour: int, minute: int = 0) -> datetime:
    return _NOW.replace(hour=hour, minute=minute)


def test_overlap_is_half_open_so_touching_is_not_a_conflict() -> None:
    """수업이 10:00 에 끝나고 블록이 10:00 에 시작하면 스케줄러가 일부러 붙인 자리다."""
    assert overlaps(_at(10), _at(11), _at(9), _at(10)) is False
    assert overlaps(_at(10), _at(11), _at(11), _at(12)) is False
    assert overlaps(_at(10), _at(11), _at(10, 59), _at(12)) is True
    assert overlaps(_at(10), _at(11), _at(9), _at(12)) is True  # 일정이 블록을 감쌈


def test_only_future_scheduled_blocks_that_overlap_are_flagged() -> None:
    busy = [(_at(10, 30), _at(12))]
    blocks = [
        ("overlap", "scheduled", _at(10), _at(11)),
        ("touching", "scheduled", _at(12), _at(13)),
        ("started", "started", _at(10), _at(11)),  # 이미 시작 — 옮길 수 없다
        ("finished", "finished", _at(10), _at(11)),
        ("cancelled", "cancelled", _at(10), _at(11)),
        ("elsewhere", "scheduled", _at(14), _at(15)),
    ]

    assert conflicting_keys(blocks, busy, now=_NOW) == {"overlap"}


def test_blocks_that_already_ended_are_not_flagged() -> None:
    """지나간 시간은 옮길 수 없다 — 지난주 그리드에 배지가 차면 지금 볼 겹침이 묻힌다."""
    busy = [(_at(7), _at(8, 30))]
    blocks = [
        ("past", "scheduled", _at(7), _at(8)),
        ("running", "scheduled", _at(8), _at(9, 30)),  # 아직 안 끝남 — 알릴 가치가 있다
    ]

    assert conflicting_keys(blocks, busy, now=_NOW) == {"running"}


def test_no_busy_means_no_conflicts() -> None:
    blocks = [("a", "scheduled", _NOW + timedelta(hours=1), _NOW + timedelta(hours=2))]

    assert conflicting_keys(blocks, [], now=_NOW) == set()
