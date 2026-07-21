"""Web Push 발송 게이트 — 잠금 3규칙의 **유일한** enforce 지점 (Issue #20, ADR-0006).

모든 푸시는 이 함수를 거친다. cron 이 직접 `WebPushSender` 를 호출하면 안 되는 이유:
ADR-0005 §7 이 "주 ≤3건 / 23~07 금지는 알림 큐(=단일 게이트) 단계에서 enforce" 라고
못 박았다 — enforce 지점이 흩어지면 새 발송 경로가 생길 때마다 규칙이 새는 구멍이 된다.

검사 순서 (구체적 사유 → 일반적 사유):
1. 구독 없음        → `no_subscription`  (권한 거부 사용자 — 인앱 노출은 FE 폴백, #16)
2. 23~07시 금지     → `quiet_hours`      ([23:00, 07:00) — api-contract §15)
3. 같은 클래스 오늘 이미 → `class_dedup`  (KST 달력일 기준 — 아래 참고)
4. 주 ≤ 3건        → `weekly_budget`    (전 클래스 합산, rolling 7일 — ADR-0006 §2)
5. 발송 → 성공 시에만 이력 기록. `gone`(404/410)이면 죽은 구독을 정리.

"같은 클래스 24h 중복 금지"(architecture.md §3)를 rolling 24h 가 아니라 **KST 달력일**로
구현한 이유: 매일 같은 시각 부근에 도는 cron 은 rolling 24h 아래에서 발송 시각이 매일
5분씩 뒤로 밀린다(어제 21:03 발송 → 오늘 21:00 폴은 23h57m < 24h 로 차단 → 21:05 발송
→ 내일은 21:10…). 달력일 기준은 규칙의 의도("하루 두 번 보내지 마라")를 지키면서 이
래칫이 없다. 잠금 문구 재해석은 ADR-0006 §3 에 박제.

예산은 **실발송만** 소모한다 — 게이트에 막힌 시도가 카운트되면 한 건도 못 받은
사용자의 주 예산이 바닥나는 모순이 생긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal

from reaction_backend.db.models.notification_send import NOTIFICATION_CLASSES
from reaction_backend.schemas.common import KST

if TYPE_CHECKING:
    from reaction_backend.db.models.notification_setting import NotificationSetting
    from reaction_backend.integrations.web_push import WebPushSender
    from reaction_backend.repositories.notification_send_repo import NotificationSendRepo

_log = logging.getLogger(__name__)

# 주 ≤ 3건 (AGENTS.md §1 잠금) — 사용자별 · 전 클래스 합산 · rolling 7일 (ADR-0006 §2).
PUSH_WEEKLY_BUDGET = 3
PUSH_BUDGET_WINDOW = timedelta(days=7)

# 23~07시 자동 푸시 금지 (api-contract §15) — [23:00, 07:00) 반개구간.
QUIET_START_HOUR = 23
QUIET_END_HOUR = 7

PushBlockReason = Literal[
    "no_subscription",
    "quiet_hours",
    "class_dedup",
    "weekly_budget",
    "send_gone",
    "send_error",
    "sender_unconfigured",
]


@dataclass(slots=True)
class PushResult:
    """게이트 판정 — sent=False 면 reason 에 어디서 막혔는지 남는다 (관측용)."""

    sent: bool
    reason: Literal["sent"] | PushBlockReason


def in_quiet_hours(t: time) -> bool:
    """[23:00, 07:00) — 23:00 정각은 금지, 07:00 정각은 허용.

    경계 주의: `eveningReflectionTime` 은 19~23시 설정이 가능해서 23:00 설정은 이 구간과
    맞닿는다 — 23:00 으로 설정한 사용자의 회고 알림은 발송되지 않는다 (api-contract §15 명시).
    """
    return t.hour >= QUIET_START_HOUR or t.hour < QUIET_END_HOUR


def _kst_midnight(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(KST).date(), time.min, tzinfo=KST)


async def send_push(
    *,
    setting: NotificationSetting,
    notification_class: str,
    payload: dict[str, Any],
    now: datetime,
    send_repo: NotificationSendRepo,
    sender: WebPushSender,
) -> PushResult:
    """정책 검사 → 발송 → 성공 시 이력 기록. commit 은 호출자(sweep) 책임.

    `setting` 행 자체를 받는 이유: 구독 소멸(`gone`) 시 여기서 구독을 정리해야
    다음 폴마다 죽은 endpoint 에 재시도하는 낭비가 없다.
    """
    if notification_class not in NOTIFICATION_CLASSES:
        raise ValueError(f"허용되지 않은 알림 클래스: {notification_class!r}")

    user_id = setting.user_id

    subscription = setting.push_subscription
    if subscription is None:
        return PushResult(sent=False, reason="no_subscription")

    if in_quiet_hours(now.astimezone(KST).timetz()):
        return PushResult(sent=False, reason="quiet_hours")

    if await send_repo.class_sent_since(
        user_id, notification_class=notification_class, since=_kst_midnight(now)
    ):
        return PushResult(sent=False, reason="class_dedup")

    sent_this_week = await send_repo.count_sent_since(user_id, since=now - PUSH_BUDGET_WINDOW)
    if sent_this_week >= PUSH_WEEKLY_BUDGET:
        return PushResult(sent=False, reason="weekly_budget")

    outcome = await sender.send(subscription, payload)
    if outcome == "ok":
        await send_repo.record(user_id=user_id, notification_class=notification_class, sent_at=now)
        _log.info("push sent: class=%s user=%s", notification_class, user_id)
        return PushResult(sent=True, reason="sent")
    if outcome == "gone":
        # 푸시 서비스가 구독을 폐기했다(브라우저 재설치 등) — 죽은 구독은 정리한다.
        setting.push_subscription = None
        _log.info("push subscription gone → cleared: user=%s", user_id)
        return PushResult(sent=False, reason="send_gone")
    if outcome == "unconfigured":
        return PushResult(sent=False, reason="sender_unconfigured")
    return PushResult(sent=False, reason="send_error")
