"""Scheduler cron job 단위 테스트 (Issue #19-C).

job 함수를 fake repo + 직접 호출로 검증 (HTTP 아님). `GEMINI_API_KEY` 없으니 brief 는 룰 fallback.
시각은 인자 주입(now_kst_dt) 이라 결정적.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.interruption_event import InterruptionEvent
from reaction_backend.scheduler.interruption_resolver import run_interruption_resolver
from reaction_backend.scheduler.morning_brief import run_morning_brief_for_user
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeDailyBriefRepo,
    _FakeSession,
)


def _action(title: str, priority: int, target_date) -> ActionItem:  # noqa: ANN001
    a = ActionItem()
    a.id = uuid4()
    a.user_id = DEMO_USER_UUID
    a.title = title
    a.target_date = target_date
    a.category = "project"
    a.source = "manual"
    a.status = "planned"
    a.priority = priority
    a.estimated_minutes = 30
    a.why_now = None
    a.first_step = "첫 걸음"
    a.goal_id = None
    a.archived_at = None
    return a


# ───── Morning Brief ─────


@pytest.mark.asyncio
async def test_morning_brief_creates_with_rule_fallback() -> None:
    now = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)
    action_repo = FakeActionItemRepo()
    brief_repo = FakeDailyBriefRepo()
    action_repo.seed(_action("캡스톤", 1, now.date()))
    action_repo.seed(_action("토익", 2, now.date()))

    brief = await run_morning_brief_for_user(
        DEMO_USER_UUID, now, action_repo=action_repo, brief_repo=brief_repo, session=_FakeSession()
    )
    assert brief.headline_text  # 룰 헤드라인 채워짐
    assert brief.fallback_used is True  # GEMINI 없음 → 룰
    # big_rock = priority 최상위 (1)
    seeded = sorted(action_repo._items.values(), key=lambda a: a.priority)
    assert brief.big_rock_action_item_id == seeded[0].id


@pytest.mark.asyncio
async def test_morning_brief_idempotent() -> None:
    """같은 날 재실행 — 새로 만들지 않고 기존 반환."""
    now = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)
    action_repo = FakeActionItemRepo()
    brief_repo = FakeDailyBriefRepo()

    first = await run_morning_brief_for_user(
        DEMO_USER_UUID, now, action_repo=action_repo, brief_repo=brief_repo, session=_FakeSession()
    )
    second = await run_morning_brief_for_user(
        DEMO_USER_UUID, now, action_repo=action_repo, brief_repo=brief_repo, session=_FakeSession()
    )
    assert first.id == second.id
    assert len(brief_repo._items) == 1


@pytest.mark.asyncio
async def test_morning_brief_empty_cards() -> None:
    now = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)
    brief = await run_morning_brief_for_user(
        DEMO_USER_UUID,
        now,
        action_repo=FakeActionItemRepo(),
        brief_repo=FakeDailyBriefRepo(),
        session=_FakeSession(),
    )
    assert brief.big_rock_action_item_id is None
    assert brief.headline_text


# ───── Interruption resolver ─────


class _FakeInterruptionRepo:
    def __init__(self) -> None:
        self._items: list[InterruptionEvent] = []

    def seed(self, *, resumed, created_at) -> InterruptionEvent:  # noqa: ANN001
        e = InterruptionEvent()
        e.id = uuid4()
        e.user_id = DEMO_USER_UUID
        e.execution_id = uuid4()
        e.resumed_after_interrupt = resumed
        e.created_at = created_at
        self._items.append(e)
        return e

    async def list_stale_unresolved(self, *, before) -> list[InterruptionEvent]:  # noqa: ANN001
        return [
            e for e in self._items if e.resumed_after_interrupt is None and e.created_at < before
        ]

    async def mark_unresumed(self, event: InterruptionEvent) -> None:
        event.resumed_after_interrupt = False


@pytest.mark.asyncio
async def test_interruption_resolver_marks_stale() -> None:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    repo = _FakeInterruptionRepo()
    stale = repo.seed(resumed=None, created_at=now - timedelta(hours=7))  # 7h 전 — 대상
    fresh = repo.seed(resumed=None, created_at=now - timedelta(hours=2))  # 2h 전 — 제외
    resolved = repo.seed(resumed=True, created_at=now - timedelta(hours=8))  # 이미 처리 — 제외

    count = await run_interruption_resolver(now, repo=repo)
    assert count == 1
    assert stale.resumed_after_interrupt is False
    assert fresh.resumed_after_interrupt is None
    assert resolved.resumed_after_interrupt is True


@pytest.mark.asyncio
async def test_interruption_resolver_idempotent() -> None:
    """재실행 — 이미 false 처리된 행은 다시 대상 안 됨 (NULL 만)."""
    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    repo = _FakeInterruptionRepo()
    repo.seed(resumed=None, created_at=now - timedelta(hours=7))

    first = await run_interruption_resolver(now, repo=repo)
    second = await run_interruption_resolver(now, repo=repo)
    assert first == 1
    assert second == 0
