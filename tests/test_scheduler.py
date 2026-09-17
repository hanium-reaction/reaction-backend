"""Scheduler cron job 단위 테스트 (Issue #19-C).

job 함수를 fake repo + 직접 호출로 검증 (HTTP 아님). `GEMINI_API_KEY` 없으니 brief 는 룰 fallback.
시각은 인자 주입(now_kst_dt) 이라 결정적.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.interruption_event import InterruptionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.safety import banned_words
from reaction_backend.scheduler.interruption_resolver import run_interruption_resolver
from reaction_backend.scheduler.morning_brief import run_morning_brief_for_user
from reaction_backend.schemas.common import KST
from tests.conftest import (
    DEMO_USER_UUID,
    FakeActionItemRepo,
    FakeDailyBriefRepo,
    FakeExecutionRepo,
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


@pytest.mark.asyncio
async def test_morning_brief_feeds_real_yesterday_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """어제 요약이 하드코딩 스텁이 아니라 **어제 카드의 실제 상태**로 계산된다 (#224 문제 1).

    회귀: "(데이터 없음)" 스텁을 받은 LLM 이 "어제는 조용히 잘 보냈어요" 를 지어냈다 —
    어제 실패한 사용자가 앱이 자기를 안 보고 있음을 즉시 아는 문장.
    """
    from reaction_backend.llm import aiClient
    from reaction_backend.llm.tool_executor import RunResult
    from reaction_backend.schemas.today import MorningBriefDraft

    seen: dict[str, Any] = {}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        seen.update(kwargs["variables"])
        return RunResult(
            value=MorningBriefDraft(headline_ko="오늘의 브리프"),
            fell_back=False,
            reason=None,
            prompt_id="brief/morning_brief",
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", fake_run)

    now = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)
    action_repo = FakeActionItemRepo()
    yesterday = now.date() - timedelta(days=1)
    for status in ("done", "over_done", "partial_done", "planned"):
        card = _action(f"어제-{status}", 1, yesterday)
        card.status = status
        action_repo.seed(card)
    action_repo.seed(_action("오늘 카드", 1, now.date()))

    await run_morning_brief_for_user(
        DEMO_USER_UUID,
        now,
        action_repo=action_repo,
        brief_repo=FakeDailyBriefRepo(),
        session=_FakeSession(),
    )

    assert seen["yesterday_summary"] == "어제 카드 4장 — 완료 2장, 부분 완료 1장, 못 마친 카드 1장"
    assert seen["today_focus_cards"] == "오늘 카드"


@pytest.mark.asyncio
async def test_morning_brief_marks_missing_yesterday(monkeypatch: pytest.MonkeyPatch) -> None:
    """어제 카드가 없으면 명시 마커 — 프롬프트가 이 마커일 때 '어제 언급 금지'를 강제한다."""
    from reaction_backend.llm import aiClient
    from reaction_backend.llm.tool_executor import RunResult
    from reaction_backend.schemas.today import MorningBriefDraft

    seen: dict[str, Any] = {}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        seen.update(kwargs["variables"])
        return RunResult(
            value=MorningBriefDraft(headline_ko="h"),
            fell_back=False,
            reason=None,
            prompt_id="brief/morning_brief",
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", fake_run)
    now = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)
    await run_morning_brief_for_user(
        DEMO_USER_UUID,
        now,
        action_repo=FakeActionItemRepo(),
        brief_repo=FakeDailyBriefRepo(),
        session=_FakeSession(),
    )
    assert seen["yesterday_summary"] == "(어제 카드 없음)"


@pytest.mark.asyncio
async def test_morning_brief_big_rock_skips_done_cards() -> None:
    """이미 done 인 카드는 big rock/focus 후보가 아니다 (#224 5번).

    회귀(라이브 실측): cards[:3] 이 status 를 안 보고 앞에서 잘라, 어제 완료한 카드가
    big_rock 으로 선정됐다 — "이미 끝낸 일부터 시작해보라"는 아침 브리프.
    """
    now = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)
    action_repo = FakeActionItemRepo()
    finished = _action("이미 완료한 카드", 1, now.date())
    finished.status = "done"
    todo = _action("오늘 할 카드", 2, now.date())
    action_repo.seed(finished)
    action_repo.seed(todo)

    brief = await run_morning_brief_for_user(
        DEMO_USER_UUID,
        now,
        action_repo=action_repo,
        brief_repo=FakeDailyBriefRepo(),
        session=_FakeSession(),
    )
    assert brief.big_rock_action_item_id == todo.id
    # 룰 fallback 헤드라인도 시작할 수 있는 카드 기준으로 만든다.
    assert "오늘 할 카드" in brief.headline_text
    assert "이미 완료한 카드" not in brief.headline_text


@pytest.mark.asyncio
async def test_morning_brief_splits_maintain_cards_by_goal_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """goal_repo 가 있으면 maintain tier 목표의 카드가 maintain 변수로 갈라진다 (#224 스텁 제거)."""
    from reaction_backend.db.models.goal import Goal
    from reaction_backend.llm import aiClient
    from reaction_backend.llm.tool_executor import RunResult
    from reaction_backend.schemas.today import MorningBriefDraft
    from tests.conftest import FakeGoalRepo

    seen: dict[str, Any] = {}

    async def fake_run(**kwargs: Any) -> RunResult[Any]:
        seen.update(kwargs["variables"])
        return RunResult(
            value=MorningBriefDraft(headline_ko="h"),
            fell_back=False,
            reason=None,
            prompt_id="brief/morning_brief",
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", fake_run)
    now = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)

    goal_repo = FakeGoalRepo()
    maintain_goal = Goal()
    maintain_goal.id = uuid4()
    maintain_goal.user_id = DEMO_USER_UUID
    maintain_goal.title = "스트레칭 습관"
    maintain_goal.category = "health"
    maintain_goal.goal_tier = "maintain"
    maintain_goal.status = "active"
    maintain_goal.archived_at = None
    goal_repo._items[maintain_goal.id] = maintain_goal

    action_repo = FakeActionItemRepo()
    focus_card = _action("캡스톤 마무리", 1, now.date())
    maintain_card = _action("저녁 스트레칭", 2, now.date())
    maintain_card.goal_id = maintain_goal.id
    action_repo.seed(focus_card)
    action_repo.seed(maintain_card)

    await run_morning_brief_for_user(
        DEMO_USER_UUID,
        now,
        action_repo=action_repo,
        brief_repo=FakeDailyBriefRepo(),
        session=_FakeSession(),
        goal_repo=goal_repo,  # type: ignore[arg-type]
    )
    assert seen["today_focus_cards"] == "캡스톤 마무리"
    assert seen["today_maintain_cards"] == "저녁 스트레칭"


def test_morning_brief_headline_length_is_enforced() -> None:
    """헤드라인 길이가 스키마로 강제된다 (#224 문제 3) — 초과 시 검증 실패 → 룰 fallback.

    회귀: 규칙이 프롬프트에만 있고 강제가 없어 154자 헤드라인이 그대로 나가, FE 히어로
    카드가 세로로 밀렸다.
    """
    from pydantic import ValidationError

    from reaction_backend.schemas.today import MorningBriefDraft

    MorningBriefDraft(headline_ko="가" * 140)  # 상한 이내 — 통과
    with pytest.raises(ValidationError):
        MorningBriefDraft(headline_ko="가" * 141)


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


# ── 모닝 브리프 × 캘린더 겹침 ─────────────────────────────────────────────


_BRIEF_NOW = datetime(2026, 6, 2, 6, 0, tzinfo=KST)


def _at_kst(hour: int, minute: int = 0) -> datetime:
    return _BRIEF_NOW.replace(hour=hour, minute=minute)


def _brief_block(
    repo: FakeExecutionRepo, action: ActionItem, start: datetime, minutes: int = 30
) -> None:
    b = ScheduledBlock()
    b.id = uuid4()
    b.user_id = DEMO_USER_UUID
    b.action_item_id = action.id
    b.start_at = start
    b.end_at = start + timedelta(minutes=minutes)
    b.block_status = "scheduled"
    b.source = "ai_plan"
    repo._blocks[b.id] = b


def _stub_calendar(
    monkeypatch: pytest.MonkeyPatch, status: str, busy: list[tuple[datetime, datetime]]
) -> list[str]:
    from reaction_backend.integrations.google_calendar import freebusy, oauth
    from reaction_backend.orchestrator.goal_structuring import TimeInterval

    calls: list[str] = []
    monkeypatch.setattr(oauth, "is_enabled", lambda: True)

    async def _fetch(session: Any, *, user_id: Any, start: datetime, end: datetime) -> Any:
        calls.append("fetch")
        return freebusy.FreeBusyResult(status, [TimeInterval(s, e) for s, e in busy])  # type: ignore[arg-type]

    monkeypatch.setattr(freebusy, "fetch_busy", _fetch)
    return calls


async def _brief(action_repo: FakeActionItemRepo, execution_repo: FakeExecutionRepo | None) -> Any:
    return await run_morning_brief_for_user(
        DEMO_USER_UUID,
        _BRIEF_NOW,
        action_repo=action_repo,
        brief_repo=FakeDailyBriefRepo(),
        session=_FakeSession(),
        execution_repo=execution_repo,
    )


@pytest.mark.asyncio
async def test_morning_brief_leads_with_calendar_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """계획 뒤에 생긴 약속과 겹치는 카드를 아침에 알린다 — 새 알림 없이 브리프 안에서."""
    action_repo, execution_repo = FakeActionItemRepo(), FakeExecutionRepo()
    clash = _action("ERD 그려보기", 1, _BRIEF_NOW.date())
    calm = _action("토익", 2, _BRIEF_NOW.date())
    action_repo.seed(clash)
    action_repo.seed(calm)
    _brief_block(execution_repo, clash, _at_kst(14))
    _brief_block(execution_repo, calm, _at_kst(9))
    _stub_calendar(monkeypatch, "ok", [(_at_kst(14, 15), _at_kst(15))])

    brief = await _brief(action_repo, execution_repo)

    first = brief.adjustment_hints[0]["text"]
    assert first == "오늘 14:00 'ERD 그려보기' 시간이 캘린더 일정과 겹쳐요. 시간을 옮겨 볼까요?"
    assert not any("토익" in h["text"] for h in brief.adjustment_hints)
    # 톤 가드 — 규칙 문장도 금지어 필터를 그대로 통과해야 한다
    assert banned_words.enforce(first).changed is False


@pytest.mark.asyncio
async def test_morning_brief_folds_the_rest_into_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """힌트가 겹침 목록이 되면 안 된다 — 두 개까지만 문장으로, 나머지는 한 줄. 카드당 한 번."""
    action_repo, execution_repo = FakeActionItemRepo(), FakeExecutionRepo()
    for i, hour in enumerate((9, 11, 13)):
        card = _action(f"카드{i}", i + 1, _BRIEF_NOW.date())
        action_repo.seed(card)
        _brief_block(execution_repo, card, _at_kst(hour))
        if i == 0:
            _brief_block(execution_repo, card, _at_kst(hour, 30))  # 같은 카드의 두 번째 세션
    _stub_calendar(monkeypatch, "ok", [(_at_kst(8), _at_kst(18))])

    brief = await _brief(action_repo, execution_repo)

    texts = [h["text"] for h in brief.adjustment_hints]
    assert texts[0].startswith("오늘 09:00 '카드0'")
    assert texts[1].startswith("오늘 11:00 '카드1'")
    assert texts[2] == "그 밖에 1개 카드도 캘린더 일정과 겹쳐요."


@pytest.mark.asyncio
async def test_morning_brief_stays_quiet_when_the_calendar_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """못 읽었다고 브리프가 실패하거나, 겹침이 없다고 말하지 않는다 — 그냥 싣지 않는다."""
    action_repo, execution_repo = FakeActionItemRepo(), FakeExecutionRepo()
    card = _action("캡스톤", 1, _BRIEF_NOW.date())
    action_repo.seed(card)
    _brief_block(execution_repo, card, _at_kst(10))
    _stub_calendar(monkeypatch, "failed", [])

    brief = await _brief(action_repo, execution_repo)

    assert brief.headline_text
    assert not any("캘린더" in h["text"] for h in brief.adjustment_hints)


@pytest.mark.asyncio
async def test_morning_brief_without_execution_repo_never_reads_the_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기존 호출부(execution_repo 미전달)는 동작 무변경 — Google 을 부르지 않는다."""
    action_repo = FakeActionItemRepo()
    action_repo.seed(_action("캡스톤", 1, _BRIEF_NOW.date()))
    calls = _stub_calendar(monkeypatch, "ok", [(_at_kst(0), _at_kst(23))])

    await _brief(action_repo, None)

    assert calls == []
