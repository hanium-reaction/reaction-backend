"""재관여율(`re_engagement_rate`) 리포트 판정 고정 (근거 대장 §7.2, A3).

핵심 판정 함수(`_re_engaged`)만 고정한다 — 나머지(쿼리 조립)는 얇게 둔다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from scripts.report_re_engagement import _RE_ENGAGEMENT_WINDOW, _re_engaged

from reaction_backend.schemas.common import KST

_GOAL = uuid4()
_ANCHOR = datetime(2026, 7, 21, 9, 0, tzinfo=KST)


def test_success_right_after_anchor_counts() -> None:
    starts = {_GOAL: [_ANCHOR + timedelta(minutes=1)]}
    assert _re_engaged(_ANCHOR, _GOAL, starts) is True


def test_success_exactly_at_window_boundary_counts() -> None:
    """경계는 포함 — `<=` 를 좁히는 뮤턴트를 잡는다."""
    starts = {_GOAL: [_ANCHOR + _RE_ENGAGEMENT_WINDOW]}
    assert _re_engaged(_ANCHOR, _GOAL, starts) is True


def test_success_exactly_at_anchor_does_not_count() -> None:
    """앵커 시각 자체(그 이전)는 재관여가 아니다 — 창은 앵커 '이후'만."""
    starts = {_GOAL: [_ANCHOR]}
    assert _re_engaged(_ANCHOR, _GOAL, starts) is False


def test_success_past_the_window_does_not_count() -> None:
    starts = {_GOAL: [_ANCHOR + _RE_ENGAGEMENT_WINDOW + timedelta(minutes=1)]}
    assert _re_engaged(_ANCHOR, _GOAL, starts) is False


def test_no_goal_id_never_counts() -> None:
    """계보가 없으면(습관/인박스/수동) 판정 불가 — 보수적으로 False."""
    starts = {_GOAL: [_ANCHOR + timedelta(minutes=1)]}
    assert _re_engaged(_ANCHOR, None, starts) is False


def test_success_in_a_different_goal_lineage_does_not_count() -> None:
    other_goal = uuid4()
    starts = {other_goal: [_ANCHOR + timedelta(minutes=1)]}
    assert _re_engaged(_ANCHOR, _GOAL, starts) is False
