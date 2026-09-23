"""Web Push 발송 게이트 — 잠금 3규칙의 **유일한** enforce 지점 (Issue #20, ADR-0006).

모든 푸시는 이 함수를 거친다. cron 이 직접 `WebPushSender` 를 호출하면 안 되는 이유:
ADR-0005 §7 이 "주 ≤3건 / 23~07 금지는 알림 큐(=단일 게이트) 단계에서 enforce" 라고
못 박았다 — enforce 지점이 흩어지면 새 발송 경로가 생길 때마다 규칙이 새는 구멍이 된다.

검사 순서 (구체적 사유 → 일반적 사유):
1. 구독 없음        → `no_subscription`  (권한 거부 사용자 — 인앱 노출은 FE 폴백, #16)
2. 23~07시 금지     → `quiet_hours`      ([23:00, 07:00) — api-contract §15)
3. **사용자 advisory lock** — 이력을 읽기 전에 잡는다. evening·pre_card cron 이 같은
   5분 틱에 병행하므로, 직렬화 없이는 둘 다 커밋 전 count 를 읽고 동시 발송해 주 3건을
   초과한다 (TOCTOU — ADR-0006 §8). 락은 트랜잭션 종료(sweep 의 per-user commit) 시 해제.
4. 같은 클래스 오늘 이미 → `class_dedup`  (KST 달력일 기준 — 아래 참고)
5. 주 ≤ 3건        → `weekly_budget`    (전 클래스 합산, rolling 7일 — ADR-0006 §2)
6. 발송 → 성공 시에만 이력 기록. `gone`(404/410)이면 죽은 구독을 정리.
   발송에는 클래스별 TTL·Urgency 를 싣는다(`push_delivery_options`) — TTL 0 이면 push 서비스가
   절전·오프라인 기기 몫을 버리는데도 우리는 발송으로 기록해 주 예산을 써 버린다.

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
from uuid import UUID

from reaction_backend.db.models.notification_send import NOTIFICATION_CLASSES
from reaction_backend.schemas.common import KST

if TYPE_CHECKING:
    from reaction_backend.db.models.notification_setting import NotificationSetting
    from reaction_backend.integrations.web_push import PushUrgency, WebPushSender
    from reaction_backend.repositories.notification_send_repo import NotificationSendRepo

_log = logging.getLogger(__name__)

# 주 ≤ 3건 (AGENTS.md §1 잠금) — 사용자별 · 전 클래스 합산 · rolling 7일 (ADR-0006 §2).
PUSH_WEEKLY_BUDGET = 3
PUSH_BUDGET_WINDOW = timedelta(days=7)

# 23~07시 자동 푸시 금지 (api-contract §15) — [23:00, 07:00) 반개구간.
QUIET_START_HOUR = 23
QUIET_END_HOUR = 7

# 클래스별 전달 유효 시간(초, RFC 8030 TTL) — 이 시간 안에 기기가 깨어나면 받는다.
# - pre_card: 시작 2~7분 전 알림이라 카드가 시작되면 의미가 없다 → 7분. 놓치면 안 되니 high.
# - evening_reflection: 회고는 그날 밤 안에만 의미가 있다 → quiet hours 시작(23시)까지
#   (19시 발송도 덮도록 4시간, 아래에서 23시로 자른다).
# - morning_brief: 오늘의 재관여 안내 → 오전 중(3시간).
# 모든 TTL 은 23시(quiet hours 시작)를 넘지 않게 자른다 — 늦게 깨어난 기기에 한밤중 알림이
# 뜨지 않게. 최소 60초는 남긴다(0 은 "즉시 못 전하면 버림"이라 다시 원래 문제가 된다).
_CLASS_TTL_SECONDS: dict[str, int] = {
    "pre_card": 7 * 60,
    "evening_reflection": 4 * 60 * 60,
    "morning_brief": 3 * 60 * 60,
}
_CLASS_URGENCY: dict[str, PushUrgency] = {
    "pre_card": "high",
    "evening_reflection": "normal",
    "morning_brief": "normal",
}
_MIN_TTL_SECONDS = 60

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


def push_delivery_options(notification_class: str, now: datetime) -> tuple[int, PushUrgency]:
    """(TTL 초, Urgency) — 클래스별 값, TTL 은 오늘 23:00 KST(quiet hours 시작)까지로 자른다."""
    kst_now = now.astimezone(KST)
    quiet_start = datetime.combine(kst_now.date(), time(QUIET_START_HOUR), tzinfo=KST)
    ttl = _CLASS_TTL_SECONDS[notification_class]
    until_quiet = int((quiet_start - kst_now).total_seconds())
    if until_quiet > 0:
        ttl = min(ttl, until_quiet)
    return max(ttl, _MIN_TTL_SECONDS), _CLASS_URGENCY[notification_class]


def _kst_midnight(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(KST).date(), time.min, tzinfo=KST)


async def send_push(
    *,
    setting: NotificationSetting,
    notification_class: str,
    notification_id: UUID,
    payload: dict[str, Any],
    now: datetime,
    send_repo: NotificationSendRepo,
    sender: WebPushSender,
    target_action_item_id: UUID | None = None,
) -> PushResult:
    """정책 검사 → 발송 → 성공 시 이력 기록. commit 은 호출자(sweep) 책임.

    `setting` 행 자체를 받는 이유: 구독 소멸(`gone`) 시 여기서 구독을 정리해야
    다음 폴마다 죽은 endpoint 에 재시도하는 낭비가 없다.

    `notification_id` 는 여기서 새로 만들지 않는다 — **호출자가 `payload` 를 만들기 전에
    이미 생성해 그 안에 실어 보낸 값과 같아야 한다**(근거 대장 §6.1, `notification_send.py`
    모듈 docstring). 그래야 나중에 FE 가 "이 알림을 열었다"고 되돌려줄 id 로 이 발송
    이력 행을 찾을 수 있다.
    """
    if notification_class not in NOTIFICATION_CLASSES:
        raise ValueError(f"허용되지 않은 알림 클래스: {notification_class!r}")

    user_id = setting.user_id

    subscription = setting.push_subscription
    if subscription is None:
        return PushResult(sent=False, reason="no_subscription")

    if in_quiet_hours(now.astimezone(KST).timetz()):
        return PushResult(sent=False, reason="quiet_hours")

    # 이력 조회 전에 사용자 단위 직렬화 — 아래 두 검사(read)와 record(write) 사이에
    # 다른 cron/인스턴스가 끼어들면 잠금 상한이 뚫린다.
    await send_repo.lock_user(user_id)

    if await send_repo.class_sent_since(
        user_id, notification_class=notification_class, since=_kst_midnight(now)
    ):
        return PushResult(sent=False, reason="class_dedup")

    sent_this_week = await send_repo.count_sent_since(user_id, since=now - PUSH_BUDGET_WINDOW)
    if sent_this_week >= PUSH_WEEKLY_BUDGET:
        return PushResult(sent=False, reason="weekly_budget")

    ttl, urgency = push_delivery_options(notification_class, now)
    outcome = await sender.send(subscription, payload, ttl=ttl, urgency=urgency)
    if outcome == "ok":
        await send_repo.record(
            id=notification_id,
            user_id=user_id,
            notification_class=notification_class,
            sent_at=now,
            target_action_item_id=target_action_item_id,
        )
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
