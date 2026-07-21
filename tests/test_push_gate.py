"""발송 게이트 — 잠금 3규칙 enforce (#20, ADR-0006).

여기가 잠금 결정의 단일 고정 지점이다:
- 주 ≤ 3건 (AGENTS.md §1) — **전 클래스 합산 · rolling 7일 · 실발송만 카운트**
- 23~07시 자동 푸시 금지 (api-contract §15) — [23:00, 07:00) 경계 포함 검증
- 같은 클래스 하루 1건 (architecture.md §3 "24h 중복 금지"의 KST 달력일 구현 — ADR-0006 §3)

각 테스트는 게이트를 **직접** 태운다 — sweep 을 거치면 어느 층이 막았는지 애매해진다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.safety.push_gate import (
    PUSH_WEEKLY_BUDGET,
    PushResult,
    in_quiet_hours,
    send_push,
)
from reaction_backend.schemas.common import KST
from tests.conftest import FakeNotificationSendRepo, FakeWebPushSender

# 화요일 21:00 KST — quiet hours 밖.
NOW = datetime(2026, 7, 21, 21, 0, tzinfo=KST)

_SUBSCRIPTION = {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}}
_PAYLOAD: dict[str, Any] = {"class": "evening_reflection", "title": "t", "body": "b"}


def _setting(*, subscribed: bool = True) -> NotificationSetting:
    s = NotificationSetting()
    s.id = uuid4()
    s.user_id = uuid4()
    s.push_subscription = dict(_SUBSCRIPTION) if subscribed else None
    return s


async def _send(
    setting: NotificationSetting,
    *,
    now: datetime = NOW,
    klass: str = "evening_reflection",
    send_repo: FakeNotificationSendRepo | None = None,
    sender: FakeWebPushSender | None = None,
) -> tuple[PushResult, FakeNotificationSendRepo, FakeWebPushSender]:
    send_repo = send_repo or FakeNotificationSendRepo()
    sender = sender or FakeWebPushSender()
    result = await send_push(
        setting=setting,
        notification_class=klass,
        payload=_PAYLOAD,
        now=now,
        send_repo=send_repo,  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
    )
    return result, send_repo, sender


# ── 발송 성공 경로 ──


async def test_send_records_history() -> None:
    setting = _setting()
    result, send_repo, sender = await _send(setting)

    assert result == PushResult(sent=True, reason="sent")
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == _SUBSCRIPTION
    assert len(send_repo._sends) == 1
    assert send_repo._sends[0].notification_class == "evening_reflection"
    assert send_repo._sends[0].sent_at == NOW


async def test_unknown_class_is_rejected() -> None:
    """3 클래스 밖은 코드 오류 — 조용히 보내지 말고 즉시 터뜨린다 (AGENTS.md §1)."""
    with pytest.raises(ValueError, match="허용되지 않은"):
        await _send(_setting(), klass="marketing_blast")


# ── 규칙 1: 구독 없음 ──


async def test_no_subscription_skips_without_transport() -> None:
    result, send_repo, sender = await _send(_setting(subscribed=False))

    assert result == PushResult(sent=False, reason="no_subscription")
    assert sender.calls == []  # 전송 시도 자체가 없다
    assert send_repo._sends == []  # 예산 미소모


# ── 규칙 2: quiet hours [23:00, 07:00) ──


def test_quiet_hours_boundaries() -> None:
    """23:00 정각부터 금지, 07:00 정각부터 허용 — 반개구간을 경계값으로 고정."""
    assert in_quiet_hours(time(23, 0)) is True
    assert in_quiet_hours(time(22, 59)) is False
    assert in_quiet_hours(time(6, 59)) is True
    assert in_quiet_hours(time(7, 0)) is False
    assert in_quiet_hours(time(2, 30)) is True
    assert in_quiet_hours(time(12, 0)) is False


async def test_quiet_hours_blocks_send() -> None:
    result, send_repo, sender = await _send(
        _setting(), now=datetime(2026, 7, 21, 23, 0, tzinfo=KST)
    )

    assert result == PushResult(sent=False, reason="quiet_hours")
    assert sender.calls == []
    assert send_repo._sends == []


# ── 규칙 3: 같은 클래스 하루 1건 (KST 달력일) ──


async def test_same_class_same_day_is_deduped() -> None:
    setting = _setting()
    send_repo = FakeNotificationSendRepo()

    first, _, _ = await _send(setting, send_repo=send_repo)
    second, _, sender2 = await _send(setting, now=NOW + timedelta(hours=1), send_repo=send_repo)

    assert first.sent is True
    assert second == PushResult(sent=False, reason="class_dedup")
    assert sender2.calls == []


async def test_same_class_next_day_is_allowed_no_ratchet() -> None:
    """어제 21:03 발송 → 오늘 21:00 발송 가능.

    rolling 24h 로 구현하면 여기서 차단돼 발송 시각이 매일 5분씩 밀린다(래칫) —
    달력일 dedup 을 고른 이유 그 자체 (ADR-0006 §3).
    """
    setting = _setting()
    send_repo = FakeNotificationSendRepo()

    first, _, _ = await _send(
        setting, now=NOW - timedelta(days=1) + timedelta(minutes=3), send_repo=send_repo
    )
    second, _, _ = await _send(setting, now=NOW, send_repo=send_repo)

    assert first.sent is True
    assert second.sent is True  # 23h57m 전 발송이지만 어제 날짜 — 차단하지 않는다


async def test_different_class_same_day_is_allowed() -> None:
    setting = _setting()
    send_repo = FakeNotificationSendRepo()

    await _send(setting, klass="pre_card", send_repo=send_repo)
    result, _, _ = await _send(setting, klass="evening_reflection", send_repo=send_repo)

    assert result.sent is True  # dedup 은 클래스별 — 합산 상한은 주간 예산이 담당


# ── 규칙 4: 주 ≤ 3건 (전 클래스 합산, rolling 7일) ──


async def test_weekly_budget_blocks_fourth_send() -> None:
    setting = _setting()
    send_repo = FakeNotificationSendRepo()

    # 서로 다른 날·다른 클래스로 3건 채운다 (dedup 에 안 걸리게).
    days = [NOW - timedelta(days=3), NOW - timedelta(days=2), NOW - timedelta(days=1)]
    classes = ["morning_brief", "pre_card", "evening_reflection"]
    for day, klass in zip(days, classes, strict=True):
        r, _, _ = await _send(setting, now=day, klass=klass, send_repo=send_repo)
        assert r.sent is True

    fourth, _, sender4 = await _send(setting, now=NOW, send_repo=send_repo)

    assert fourth == PushResult(sent=False, reason="weekly_budget")
    assert sender4.calls == []
    assert len(send_repo._sends) == PUSH_WEEKLY_BUDGET  # 차단 시도는 기록되지 않는다


async def test_weekly_budget_counts_all_classes_combined() -> None:
    """pre_card 만으로도 예산이 찬다 — '클래스별 3건' 오독 방지 (AGENTS.md §1 합산 상한)."""
    setting = _setting()
    send_repo = FakeNotificationSendRepo()

    for d in range(3, 0, -1):
        r, _, _ = await _send(
            setting, now=NOW - timedelta(days=d), klass="pre_card", send_repo=send_repo
        )
        assert r.sent is True

    blocked, _, _ = await _send(setting, klass="evening_reflection", send_repo=send_repo)
    assert blocked == PushResult(sent=False, reason="weekly_budget")


async def test_weekly_budget_window_rolls_off() -> None:
    """8일 전 발송은 카운트에서 빠진다 — rolling 7일."""
    setting = _setting()
    send_repo = FakeNotificationSendRepo()

    for d in (10, 9, 8):  # 시간순 — 거꾸로 보내면 미래 기록에 dedup 이 걸린다
        r, _, _ = await _send(
            setting, now=NOW - timedelta(days=d), klass="pre_card", send_repo=send_repo
        )
        assert r.sent is True

    result, _, _ = await _send(setting, send_repo=send_repo)
    assert result.sent is True


async def test_budget_is_per_user() -> None:
    """한 사용자가 예산을 다 써도 다른 사용자는 영향 없다."""
    heavy, light = _setting(), _setting()
    send_repo = FakeNotificationSendRepo()

    for d in (3, 2, 1):
        await _send(heavy, now=NOW - timedelta(days=d), klass="pre_card", send_repo=send_repo)

    blocked, _, _ = await _send(heavy, send_repo=send_repo)
    allowed, _, _ = await _send(light, send_repo=send_repo)

    assert blocked.sent is False
    assert allowed.sent is True


# ── 전송 결과 처리 ──


async def test_gone_clears_subscription_and_records_nothing() -> None:
    """404/410 = 구독 소멸 — 정리해서 다음 폴부터 재시도 낭비를 없앤다."""
    setting = _setting()
    result, send_repo, _ = await _send(setting, sender=FakeWebPushSender(outcome="gone"))

    assert result == PushResult(sent=False, reason="send_gone")
    assert setting.push_subscription is None
    assert send_repo._sends == []


async def test_error_keeps_subscription_and_records_nothing() -> None:
    """일시 오류는 구독을 지우지 않는다 — 다음 기회에 다시 시도."""
    setting = _setting()
    result, send_repo, _ = await _send(setting, sender=FakeWebPushSender(outcome="error"))

    assert result == PushResult(sent=False, reason="send_error")
    assert setting.push_subscription == _SUBSCRIPTION
    assert send_repo._sends == []


async def test_unconfigured_sender_degrades_quietly() -> None:
    """VAPID 미설정 환경(로컬 등) — 발송만 skip, 예산·구독 불변."""
    setting = _setting()
    result, send_repo, _ = await _send(setting, sender=FakeWebPushSender(outcome="unconfigured"))

    assert result == PushResult(sent=False, reason="sender_unconfigured")
    assert setting.push_subscription == _SUBSCRIPTION
    assert send_repo._sends == []
