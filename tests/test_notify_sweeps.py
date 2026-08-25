"""알림 sweep 2종 — 저녁 회고 알림 · pre_card (#20).

게이트 규칙 자체는 test_push_gate 가 고정 — 여기는 sweep 층의 책임만 본다:
누구를 고르고(활성·구독·설정시각·pending) · 무엇을 보내고(payload) · 실패 격리.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from uuid import UUID, uuid4

import pytest

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.notification_send import NOTIFICATION_ID_PREFIX
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.scheduler import notify_sweeps
from reaction_backend.schemas.common import KST
from tests.conftest import (
    FakeActionItemRepo,
    FakeExecutionRepo,
    FakeNotificationRepo,
    FakeNotificationSendRepo,
    FakeRecoveryRepo,
    FakeUserRepo,
    FakeWebPushSender,
    _FakeSession,
)

# 화요일 21:02 KST — 기본 설정(21:00) 직후의 폴.
NOW = datetime(2026, 7, 21, 21, 2, tzinfo=KST)

_SUBSCRIPTION = {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}}


def _user() -> User:
    u = User()
    u.id = uuid4()
    u.email = f"{u.id}@reaction.local"
    u.name = "tester"
    u.timezone = "Asia/Seoul"
    u.onboarding_state = "ACTIVE"
    u.is_anonymized = False
    u.tone_mode = "gentle"
    u.archived_at = None
    return u


def _subscribed_setting(
    user_id: UUID, *, evening: time = time(21, 0), pre_card: bool = False
) -> NotificationSetting:
    s = NotificationSetting()
    s.id = uuid4()
    s.user_id = user_id
    s.morning_brief_time = time(8, 0)
    s.evening_reflection_time = evening
    s.pre_card_enabled = pre_card
    s.push_subscription = dict(_SUBSCRIPTION)
    return s


def _pending_execution(user_id: UUID, *, plan_start: datetime) -> ExecutionEvent:
    e = ExecutionEvent()
    e.id = uuid4()
    e.user_id = user_id
    e.action_item_id = uuid4()
    e.plan_start_at = plan_start
    e.actual_start_at = None
    e.completion_status = "in_progress"
    return e


class _EveningHarness:
    """저녁 sweep 의존성 묶음 — 사용자 1명 시드가 반복이라 조립을 모은다."""

    def __init__(self) -> None:
        self.user_repo = FakeUserRepo()
        self.notif_repo = FakeNotificationRepo()
        self.execution_repo = FakeExecutionRepo()
        self.send_repo = FakeNotificationSendRepo()
        self.sender = FakeWebPushSender()
        self.session = _FakeSession()

    def seed_user(
        self,
        *,
        evening: time = time(21, 0),
        subscribed: bool = True,
        pending: int = 1,
        pending_relative_to: datetime = NOW,
    ) -> User:
        user = _user()
        self.user_repo.register(user)
        setting = _subscribed_setting(user.id, evening=evening)
        if not subscribed:
            setting.push_subscription = None
        self.notif_repo._items[user.id] = setting
        for _ in range(pending):
            e = _pending_execution(user.id, plan_start=pending_relative_to - timedelta(hours=3))
            self.execution_repo._executions[e.id] = e
        return user

    async def run(self, now: datetime = NOW) -> notify_sweeps.NotifySweepResult:
        return await notify_sweeps.run_evening_reflection_notify_sweep(
            now,
            user_repo=self.user_repo,  # type: ignore[arg-type]
            notif_repo=self.notif_repo,  # type: ignore[arg-type]
            execution_repo=self.execution_repo,  # type: ignore[arg-type]
            send_repo=self.send_repo,  # type: ignore[arg-type]
            sender=self.sender,  # type: ignore[arg-type]
            session=self.session,  # type: ignore[arg-type]
            clock=lambda: now,  # 결정론 — 기본값(now_kst)은 벽시계라 테스트가 흔들린다
        )


# ── 저녁 회고 알림 ──


async def test_evening_sends_after_user_time_with_pending() -> None:
    h = _EveningHarness()
    h.seed_user(pending=2)

    result = await h.run()

    assert result == notify_sweeps.NotifySweepResult(total=1, sent=1, skipped=0, failed=0)
    payload = h.sender.calls[0][1]
    assert payload["class"] == "evening_reflection"
    assert "2장" in payload["body"]  # pending 수가 문구에 반영
    # 발송 직후 사용자 단위 commit — 뮤테이션 실증: commit 을 지운 뮤턴트가 이 단언 없이
    # 전 스위트를 통과했고, 운영에선 이력이 매 폴 롤백돼 5분마다 재발송(스팸)이 된다.
    assert h.session.commit_count >= 1


async def test_evening_not_before_user_time() -> None:
    """21:30 설정 사용자는 21:02 폴에서 아직 — 각자의 시각을 존중 (#20 이슈 원문)."""
    h = _EveningHarness()
    h.seed_user(evening=time(21, 30))

    result = await h.run()

    assert result.sent == 0
    assert h.sender.calls == []


async def test_evening_late_poll_still_sends() -> None:
    """설정 시각을 지난 어느 폴이든 발송 — 재시작으로 21:00 폴을 놓쳐도 주워 담는다."""
    h = _EveningHarness()
    h.seed_user(evening=time(21, 0))

    result = await h.run(now=datetime(2026, 7, 21, 22, 40, tzinfo=KST))

    assert result.sent == 1


async def test_evening_skips_when_no_pending() -> None:
    """돌아볼 카드가 없으면 안 부른다 — 소음이자 주 3건 예산 낭비 (ADR-0006 §4)."""
    h = _EveningHarness()
    h.seed_user(pending=0)

    result = await h.run()

    assert result == notify_sweeps.NotifySweepResult(total=1, sent=0, skipped=1, failed=0)
    assert h.sender.calls == []


async def test_evening_skips_unsubscribed_and_missing_row() -> None:
    h = _EveningHarness()
    h.seed_user(subscribed=False)
    no_row = _user()
    h.user_repo.register(no_row)  # notification_settings 행 자체가 없는 사용자

    result = await h.run()

    assert result.sent == 0
    assert result.skipped == 2
    # 행 없는 사용자에게 행을 만들지 않는다 (GET 계약과 동일 — 행 생성은 사용자 행동만).
    assert no_row.id not in h.notif_repo._items


async def test_evening_second_poll_does_not_duplicate() -> None:
    """같은 저녁의 다음 폴(21:07)은 게이트 dedup 에 걸린다 — 하루 1건."""
    h = _EveningHarness()
    h.seed_user()

    first = await h.run()
    second = await h.run(now=NOW + timedelta(minutes=5))

    assert first.sent == 1
    assert second.sent == 0
    assert len(h.sender.calls) == 1
    assert len(h.send_repo._sends) == 1  # 차단 폴이 이력·예산을 소모하지 않는다


async def test_evening_setting_after_2255_is_clamped_to_last_poll() -> None:
    """22:56~23:00 설정은 22:55(quiet 전 마지막 폴)로 클램프 — 영구 미발송 방지.

    회귀: 클램프 없이는 22:57 설정의 첫 통과 폴이 23:00 인데 quiet hours 가 막아
    **매일 조용히 미발송**된다 (계약상 19~23시는 유효 설정인데도). ADR-0006 §7.
    """
    h = _EveningHarness()
    h.seed_user(evening=time(22, 57))
    h.seed_user(evening=time(23, 0))

    before = await h.run(now=datetime(2026, 7, 21, 22, 50, tzinfo=KST))
    assert before.sent == 0  # 클램프 시각(22:55) 전에는 안 보낸다

    result = await h.run(now=datetime(2026, 7, 21, 22, 55, tzinfo=KST))
    assert result.sent == 2, "22:55 폴에서 클램프 발송돼야 한다 — 그날의 마지막 기회"


async def test_evening_sunday_attaches_weekly_report_text_and_deeplink() -> None:
    """일요일은 같은 클래스에 문구·딥링크만 갈라진다(ADR-0008 §8 "F") — 새 클래스 없음."""
    h = _EveningHarness()
    sunday = datetime(2026, 7, 26, 21, 2, tzinfo=KST)  # 2026-07-26 은 일요일
    h.seed_user(pending=3, pending_relative_to=sunday)

    result = await h.run(now=sunday)

    assert result.sent == 1
    payload = h.sender.calls[0][1]
    assert payload["class"] == "evening_reflection"  # 새 클래스 아님
    assert "3장" in payload["body"]
    assert "리포트" in payload["title"] or "리포트" in payload["body"]
    assert payload["url"] == "/reviews/weekly"


async def test_evening_non_sunday_keeps_reflection_deeplink() -> None:
    """평일은 기존 문구·딥링크 그대로(회귀 방지) — NOW(화요일) 로 고정 확인."""
    h = _EveningHarness()
    h.seed_user(pending=1)

    result = await h.run()

    assert result.sent == 1
    payload = h.sender.calls[0][1]
    assert payload["url"] == "/reflection"
    assert "리포트" not in payload["title"]
    assert "리포트" not in payload["body"]


async def test_evening_sunday_still_gated_by_pending_cards() -> None:
    """일요일이라고 발송 조건이 느슨해지지 않는다 — 회고할 카드 없으면 여전히 no-op."""
    h = _EveningHarness()
    h.seed_user(pending=0)
    sunday = datetime(2026, 7, 26, 21, 2, tzinfo=KST)

    result = await h.run(now=sunday)

    assert result.sent == 0
    assert h.sender.calls == []


async def test_evening_isolates_one_user_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _EveningHarness()
    bad = h.seed_user()
    h.seed_user()

    original = h.execution_repo.list_pending_reflection

    async def _flaky(user_id: UUID, *, since: datetime) -> list[ExecutionEvent]:
        if user_id == bad.id:
            raise RuntimeError("boom")
        return await original(user_id, since=since)

    monkeypatch.setattr(h.execution_repo, "list_pending_reflection", _flaky)

    result = await h.run()

    assert result.failed == 1
    assert result.sent == 1  # 나머지는 진행
    # 실패 시 세션 rollback — 없으면 실 DB 에선 aborted 세션이 남아 이후 사용자 전원이
    # PendingRollbackError 로 죽는다 (실패 격리가 허상이 된다).
    assert h.session.rollback_count == 1


# ── pre_card 알림 ──


class _PreCardHarness:
    def __init__(self) -> None:
        self.notif_repo = FakeNotificationRepo()
        self.execution_repo = FakeExecutionRepo()
        self.send_repo = FakeNotificationSendRepo()
        self.sender = FakeWebPushSender()
        self.session = _FakeSession()

    def seed_block(
        self,
        *,
        starts_in: timedelta = timedelta(minutes=4),
        status: str = "scheduled",
        title: str = "리포트 초안 쓰기",
        enabled: bool = True,
    ) -> ScheduledBlock:
        user_id = uuid4()
        self.notif_repo._items[user_id] = _subscribed_setting(user_id, pre_card=enabled)

        action = ActionItem()
        action.id = uuid4()
        action.user_id = user_id
        action.title = title
        action.archived_at = None
        self.execution_repo._actions[action.id] = action

        block = ScheduledBlock()
        block.id = uuid4()
        block.user_id = user_id
        block.action_item_id = action.id
        block.start_at = NOW + starts_in
        block.end_at = block.start_at + timedelta(minutes=30)
        block.block_status = status
        self.execution_repo._blocks[block.id] = block
        return block

    async def run(self, now: datetime = NOW) -> notify_sweeps.NotifySweepResult:
        return await notify_sweeps.run_pre_card_notify_sweep(
            now,
            execution_repo=self.execution_repo,  # type: ignore[arg-type]
            notif_repo=self.notif_repo,  # type: ignore[arg-type]
            send_repo=self.send_repo,  # type: ignore[arg-type]
            sender=self.sender,  # type: ignore[arg-type]
            session=self.session,  # type: ignore[arg-type]
            clock=lambda: now,
        )


async def test_pre_card_sends_for_block_in_window() -> None:
    h = _PreCardHarness()
    h.seed_block(starts_in=timedelta(minutes=4), title="리포트 초안 쓰기")

    result = await h.run()

    assert result.sent == 1
    payload = h.sender.calls[0][1]
    assert payload["class"] == "pre_card"
    assert "리포트 초안 쓰기" in payload["body"]
    assert "21:06" in payload["body"]  # 시작 시각 HH:MM
    assert h.session.commit_count >= 1  # 건당 commit (evening 쪽 테스트와 같은 근거)


async def test_pre_card_records_target_action_item_and_matching_payload_id() -> None:
    """근거 대장 §6.1 — 발송 이력이 그 카드를 가리키고, payload 의 id 가 그 행의 PK 와 같다.

    id 를 payload 구성 **전에** 미리 만들어야 하는 이유가 여기서 실제로 검증된다 —
    발송 후에 새로 발급했다면 이 둘이 절대 같을 수 없다.
    """
    h = _PreCardHarness()
    block = h.seed_block()

    await h.run()

    payload = h.sender.calls[0][1]
    sent_row = h.send_repo._sends[0]
    assert sent_row.target_action_item_id == block.action_item_id
    assert payload["id"] == f"{NOTIFICATION_ID_PREFIX}{sent_row.id}"


async def test_evening_leaves_target_action_item_id_none() -> None:
    """회고 알림은 카드 전체에 대한 것이라 특정 카드 하나로 좁히지 않는다."""
    h = _EveningHarness()
    h.seed_user(pending=2)

    await h.run()

    sent_row = h.send_repo._sends[0]
    assert sent_row.target_action_item_id is None
    payload = h.sender.calls[0][1]
    assert payload["id"] == f"{NOTIFICATION_ID_PREFIX}{sent_row.id}"


async def test_pre_card_window_is_2_to_7_minutes() -> None:
    """리드타임 경계 — 1분 뒤(너무 임박)와 8분 뒤(다음 폴 몫)는 이번 폴 대상이 아니다."""
    h = _PreCardHarness()
    h.seed_block(starts_in=timedelta(minutes=1))
    h.seed_block(starts_in=timedelta(minutes=8))

    result = await h.run()

    assert result == notify_sweeps.NotifySweepResult(total=0, sent=0, skipped=0, failed=0)


async def test_pre_card_respects_opt_in() -> None:
    """pre_card_enabled=false(기본값) 사용자는 후보여도 발송 없음 — opt-in (§15)."""
    h = _PreCardHarness()
    h.seed_block(enabled=False)

    result = await h.run()

    assert result == notify_sweeps.NotifySweepResult(total=1, sent=0, skipped=1, failed=0)
    assert h.sender.calls == []


async def test_pre_card_skips_started_blocks() -> None:
    """이미 착수한 블록엔 '곧 시작' 알림을 보내지 않는다."""
    h = _PreCardHarness()
    h.seed_block(status="started")

    result = await h.run()

    assert result.total == 0
    assert h.sender.calls == []


async def test_pre_card_second_card_same_day_is_deduped() -> None:
    """하루 두 번째 pre_card 는 게이트(클래스 dedup)가 자른다 — 같은 사용자 기준."""
    h = _PreCardHarness()
    first_block = h.seed_block(starts_in=timedelta(minutes=4))
    # 같은 사용자의 두 번째 블록 (같은 창 안).
    action2 = ActionItem()
    action2.id = uuid4()
    action2.user_id = first_block.user_id
    action2.title = "두 번째 카드"
    action2.archived_at = None
    h.execution_repo._actions[action2.id] = action2
    block2 = ScheduledBlock()
    block2.id = uuid4()
    block2.user_id = first_block.user_id
    block2.action_item_id = action2.id
    block2.start_at = NOW + timedelta(minutes=6)
    block2.end_at = block2.start_at + timedelta(minutes=30)
    block2.block_status = "scheduled"
    h.execution_repo._blocks[block2.id] = block2

    result = await h.run()

    assert result.total == 2
    assert result.sent == 1  # 첫 카드만
    assert result.skipped == 1
    assert len(h.send_repo._sends) == 1  # 차단된 두 번째가 이력을 남기지 않는다


# ── morning_brief 알림 — T2 재관여 (근거 대장 §6.2) ──

# 화요일 08:02 KST — 기본 morning_brief_time(08:00) 직후의 폴.
MORNING_NOW = datetime(2026, 7, 21, 8, 2, tzinfo=KST)


def _due_attempt(
    user_id: UUID,
    execution_id: UUID,
    *,
    group: str,
    anchor_at: datetime,
    resulting_action_item_id: UUID | None = None,
) -> RecoveryAttempt:
    a = RecoveryAttempt()
    a.id = uuid4()
    a.user_id = user_id
    a.execution_id = execution_id
    a.recovery_option_group = group
    a.recovery_strategy_type = "NANO_STEP"
    a.suggested_action_text = "다시 5분만 해볼까요?"
    a.trigger_tag = None
    a.llm_fallback_used = False
    a.prompt_version = None
    a.assigned_arm = None
    a.first_viewed_at = None
    a.user_decision = "accepted"
    a.decision_reason = None
    a.recovery_decided_at = anchor_at
    a.recovery_started_at = anchor_at
    a.recovery_completed_at = None
    a.recovery_duration_minutes = None
    a.recovery_result = "pending"
    a.resulting_action_item_id = resulting_action_item_id
    a.re_engagement_anchor_at = anchor_at
    a.created_at = anchor_at
    return a


class _MorningBriefHarness:
    def __init__(self) -> None:
        self.user_repo = FakeUserRepo()
        self.notif_repo = FakeNotificationRepo()
        self.recovery_repo = FakeRecoveryRepo()
        self.action_repo = FakeActionItemRepo()
        self.send_repo = FakeNotificationSendRepo()
        self.sender = FakeWebPushSender()
        self.session = _FakeSession()

    def _seed_action(self, user_id: UUID, title: str) -> ActionItem:
        a = ActionItem()
        a.id = uuid4()
        a.user_id = user_id
        a.title = title
        a.archived_at = None
        self.action_repo._items[a.id] = a
        return a

    def seed_user(
        self,
        *,
        subscribed: bool = True,
        morning_brief_time: time = time(8, 0),
    ) -> User:
        user = _user()
        self.user_repo.register(user)
        setting = _subscribed_setting(user.id)
        setting.morning_brief_time = morning_brief_time
        if not subscribed:
            setting.push_subscription = None
        self.notif_repo._items[user.id] = setting
        return user

    def seed_park_due(
        self,
        user: User | None = None,
        *,
        anchor_at: datetime = MORNING_NOW,
        title: str = "밀린 리포트",
    ) -> User:
        """PARK — 새 카드를 안 만들어(§5.16) 원본 카드 제목으로 문구를 채운다."""
        user = user or self.seed_user()
        original = self._seed_action(user.id, title)
        execution = self.recovery_repo.register_execution(
            user_id=user.id, action_item_id=original.id
        )
        attempt = _due_attempt(user.id, execution.id, group="PARK", anchor_at=anchor_at)
        self.recovery_repo._attempts[attempt.id] = attempt
        return user

    def seed_carry_over_due(
        self,
        user: User | None = None,
        *,
        anchor_at: datetime = MORNING_NOW,
        title: str = "내일 카드",
    ) -> tuple[User, ActionItem]:
        """CARRY_OVER — 이미 만들어진 새 카드(`resulting_action_item_id`)의 제목을 쓴다."""
        user = user or self.seed_user()
        original = self._seed_action(user.id, "원본 카드")
        resulting = self._seed_action(user.id, title)
        execution = self.recovery_repo.register_execution(
            user_id=user.id, action_item_id=original.id
        )
        attempt = _due_attempt(
            user.id,
            execution.id,
            group="CARRY_OVER",
            anchor_at=anchor_at,
            resulting_action_item_id=resulting.id,
        )
        self.recovery_repo._attempts[attempt.id] = attempt
        return user, resulting

    async def run(self, now: datetime = MORNING_NOW) -> notify_sweeps.NotifySweepResult:
        return await notify_sweeps.run_morning_brief_notify_sweep(
            now,
            user_repo=self.user_repo,  # type: ignore[arg-type]
            notif_repo=self.notif_repo,  # type: ignore[arg-type]
            recovery_repo=self.recovery_repo,  # type: ignore[arg-type]
            action_repo=self.action_repo,  # type: ignore[arg-type]
            send_repo=self.send_repo,  # type: ignore[arg-type]
            sender=self.sender,  # type: ignore[arg-type]
            session=self.session,  # type: ignore[arg-type]
            clock=lambda: now,
        )


async def test_morning_brief_sends_for_due_park_with_original_title() -> None:
    h = _MorningBriefHarness()
    h.seed_park_due(title="밀린 리포트")

    result = await h.run()

    assert result == notify_sweeps.NotifySweepResult(total=1, sent=1, skipped=0, failed=0)
    payload = h.sender.calls[0][1]
    assert payload["class"] == "morning_brief"
    assert "밀린 리포트" in payload["body"]
    assert payload["url"] == "/today"
    # PARK 는 새 카드를 안 만든다 — target 은 원본이 아니라 None(카드 자체가 없다).
    assert h.send_repo._sends[0].target_action_item_id is None


async def test_morning_brief_sends_for_due_carry_over_with_resulting_title() -> None:
    h = _MorningBriefHarness()
    _, resulting = h.seed_carry_over_due(title="내일 카드")

    result = await h.run()

    assert result.sent == 1
    payload = h.sender.calls[0][1]
    assert "내일 카드" in payload["body"]
    assert h.send_repo._sends[0].target_action_item_id == resulting.id


async def test_morning_brief_skips_when_nothing_due() -> None:
    h = _MorningBriefHarness()
    h.seed_user()

    result = await h.run()

    assert result == notify_sweeps.NotifySweepResult(total=1, sent=0, skipped=1, failed=0)
    assert h.sender.calls == []


async def test_morning_brief_skips_anchor_on_a_different_day() -> None:
    """anchor 가 다른 날이면(예: 내일) 오늘 폴 대상이 아니다 — T2 는 그날 하루 한정."""
    h = _MorningBriefHarness()
    h.seed_park_due(anchor_at=MORNING_NOW + timedelta(days=1))

    result = await h.run()

    assert result.sent == 0
    assert h.sender.calls == []


async def test_morning_brief_not_before_user_time() -> None:
    h = _MorningBriefHarness()
    h.seed_park_due(user=h.seed_user(morning_brief_time=time(9, 30)))

    result = await h.run(now=datetime(2026, 7, 21, 9, 2, tzinfo=KST))

    assert result.sent == 0


async def test_morning_brief_early_setting_is_clamped_to_0700() -> None:
    """06:00~06:59 설정은 07:00(quiet hours 끝)으로 클램프 — 없으면 영구 미발송."""
    h = _MorningBriefHarness()
    h.seed_park_due(user=h.seed_user(morning_brief_time=time(6, 0)))

    before = await h.run(now=datetime(2026, 7, 21, 6, 30, tzinfo=KST))
    assert before.sent == 0  # 클램프 시각 전에는 안 보낸다

    result = await h.run(now=datetime(2026, 7, 21, 7, 0, tzinfo=KST))
    assert result.sent == 1  # 07:00 폴에서 클램프 발송


async def test_morning_brief_skips_unsubscribed() -> None:
    h = _MorningBriefHarness()
    h.seed_park_due(user=h.seed_user(subscribed=False))

    result = await h.run()

    assert result.sent == 0
    assert h.sender.calls == []


async def test_morning_brief_multiple_due_combines_into_one_message() -> None:
    """PARK + CARRY_OVER 가 같은 날 겹치면 개수로 뭉뚱그린다 — 클래스 dedup 이 하루 1건이라
    어차피 한 번만 나간다."""
    h = _MorningBriefHarness()
    user = h.seed_user()
    h.seed_park_due(user)
    h.seed_carry_over_due(user)

    result = await h.run()

    assert result.sent == 1
    payload = h.sender.calls[0][1]
    assert "2개" in payload["body"]


async def test_morning_brief_second_poll_does_not_duplicate() -> None:
    h = _MorningBriefHarness()
    h.seed_park_due()

    first = await h.run()
    second = await h.run(now=MORNING_NOW + timedelta(minutes=5))

    assert first.sent == 1
    assert second.sent == 0
    assert len(h.send_repo._sends) == 1


async def test_morning_brief_isolates_one_user_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _MorningBriefHarness()
    bad = h.seed_park_due()
    h.seed_park_due()

    original = h.recovery_repo.list_due_re_engagement

    async def _flaky(user_id: UUID, target_date: object) -> list[RecoveryAttempt]:
        if user_id == bad.id:
            raise RuntimeError("boom")
        return await original(user_id, target_date)

    monkeypatch.setattr(h.recovery_repo, "list_due_re_engagement", _flaky)

    result = await h.run()

    assert result.failed == 1
    assert result.sent == 1
    assert h.session.rollback_count == 1
