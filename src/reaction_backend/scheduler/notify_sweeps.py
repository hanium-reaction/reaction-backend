"""알림 cron sweep 2종 — 저녁 회고 알림 · pre_card 알림 (Issue #20).

발송 판단은 전부 `safety/push_gate.py` 를 거친다(주 ≤3건 · 23~07 금지 · 클래스 dedup).
여기는 **누구에게 · 언제 · 무슨 내용**만 정한다. 한 사용자/블록 실패가 배치를 멈추지
않도록 개별 try/except — `sweeps.py` 패턴.

저녁 회고 알림 (21:00 계열, 5분 폴):
- 사용자별 `evening_reflection_time` (19~23시) 이후 첫 폴에서 발송. "정확히 그 시각"이
  아니라 "`now >= 설정시각` 이고 오늘 아직 안 보냄" — 재시작으로 폴을 놓쳐도 다음 폴이
  주워 담고, 중복은 게이트의 KST 달력일 dedup 이 막는다.
- **회고할 카드가 있을 때만** 보낸다 — 돌아볼 게 없는데 부르는 알림은 소음이고, 주 3건
  예산에서 진짜 회고 기회를 밀어낸다 (ADR-0006 §4). 창 경계는 회고 화면·만료 cron 과
  동일한 `pending_reflection_since` (단일 소스).

pre_card 알림 (5분 폴):
- `[now+2분, now+7분)` 에 시작하는 `scheduled` 블록 — "카드 2분 전" (architecture.md §6,
  5분 폴 아래에서 리드타임은 2~7분). `started` 는 이미 착수한 카드라 제외.
- 클래스 dedup 이 하루 1건으로 자르므로 같은 날 두 번째 카드부터는 발송되지 않는다.

문구 톤: "Be on your side, not on your case" (AGENTS.md §1) — 재촉·죄책감 표현 금지.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from reaction_backend.safety import push_gate
from reaction_backend.scheduler.expire_reflections import pending_reflection_since

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from reaction_backend.integrations.web_push import WebPushSender
    from reaction_backend.repositories.execution_repo import ExecutionRepo
    from reaction_backend.repositories.notification_repo import NotificationRepo
    from reaction_backend.repositories.notification_send_repo import NotificationSendRepo
    from reaction_backend.repositories.user_repo import UserRepo

_log = logging.getLogger(__name__)

# "카드 2분 전" (architecture.md §6). 5분 폴 간격과 짝 — 실제 리드타임은 2~7분.
PRE_CARD_LEAD = timedelta(minutes=2)
NOTIFY_POLL_INTERVAL = timedelta(minutes=5)


@dataclass(slots=True)
class NotifySweepResult:
    """알림 sweep 결과 — sent 는 실발송, skipped 는 게이트/조건에 걸러진 수."""

    total: int
    sent: int
    skipped: int
    failed: int


def _evening_payload(pending_count: int) -> dict[str, Any]:
    return {
        "class": "evening_reflection",
        "title": "오늘의 회고 시간이에요",
        "body": f"돌아볼 카드가 {pending_count}장 있어요.",
        "url": "/reflection",
    }


def _pre_card_payload(title: str, start_hhmm: str) -> dict[str, Any]:
    return {
        "class": "pre_card",
        "title": "곧 시작할 카드가 있어요",
        "body": f"{start_hhmm} · {title}",
        "url": "/today",
    }


async def run_evening_reflection_notify_sweep(
    now_kst_dt: datetime,
    *,
    user_repo: UserRepo,
    notif_repo: NotificationRepo,
    execution_repo: ExecutionRepo,
    send_repo: NotificationSendRepo,
    sender: WebPushSender,
    session: AsyncSession,
) -> NotifySweepResult:
    """19~23시 5분 폴 — 설정 시각 지난 사용자에게 회고 알림 (있을 때만, 하루 1건)."""
    users = await user_repo.list_active()
    sent = skipped = failed = 0
    for user in users:
        try:
            setting = await notif_repo.get_by_user(user.id)
            # 행이 없으면 구독한 적도 없는 사용자 (구독이 행에 담긴다) — 만들지 않는다.
            if setting is None or setting.push_subscription is None:
                skipped += 1
                continue
            if now_kst_dt.time() < setting.evening_reflection_time:
                skipped += 1
                continue
            pending = await execution_repo.list_pending_reflection(
                user.id, since=pending_reflection_since(now_kst_dt.date())
            )
            if not pending:
                skipped += 1
                continue
            result = await push_gate.send_push(
                setting=setting,
                notification_class="evening_reflection",
                payload=_evening_payload(len(pending)),
                now=now_kst_dt,
                send_repo=send_repo,
                sender=sender,
            )
            sent += 1 if result.sent else 0
            skipped += 0 if result.sent else 1
        except Exception:  # noqa: BLE001 — 한 사용자 실패가 배치를 멈추지 않게
            failed += 1
            _log.exception("evening_reflection notify failed for user %s", user.id)
    await session.commit()
    if sent or failed:
        _log.info(
            "evening_reflection notify: total=%d sent=%d skipped=%d failed=%d",
            len(users),
            sent,
            skipped,
            failed,
        )
    return NotifySweepResult(total=len(users), sent=sent, skipped=skipped, failed=failed)


async def run_pre_card_notify_sweep(
    now_kst_dt: datetime,
    *,
    execution_repo: ExecutionRepo,
    notif_repo: NotificationRepo,
    send_repo: NotificationSendRepo,
    sender: WebPushSender,
    session: AsyncSession,
) -> NotifySweepResult:
    """5분 폴 — 2~7분 뒤 시작하는 scheduled 블록의 주인에게 pre_card 알림 (opt-in)."""
    window_start = now_kst_dt + PRE_CARD_LEAD
    blocks = await execution_repo.list_blocks_starting_between(
        start=window_start, end=window_start + NOTIFY_POLL_INTERVAL
    )
    sent = skipped = failed = 0
    for block in blocks:
        try:
            setting = await notif_repo.get_by_user(block.user_id)
            if setting is None or not setting.pre_card_enabled:
                skipped += 1  # opt-in 기본 false (api-contract §15)
                continue
            title = block.action_item.title
            start_hhmm = block.start_at.astimezone(now_kst_dt.tzinfo).strftime("%H:%M")
            result = await push_gate.send_push(
                setting=setting,
                notification_class="pre_card",
                payload=_pre_card_payload(title, start_hhmm),
                now=now_kst_dt,
                send_repo=send_repo,
                sender=sender,
            )
            sent += 1 if result.sent else 0
            skipped += 0 if result.sent else 1
        except Exception:  # noqa: BLE001
            failed += 1
            _log.exception("pre_card notify failed for block %s", block.id)
    await session.commit()
    if sent or failed:
        _log.info(
            "pre_card notify: total=%d sent=%d skipped=%d failed=%d",
            len(blocks),
            sent,
            skipped,
            failed,
        )
    return NotifySweepResult(total=len(blocks), sent=sent, skipped=skipped, failed=failed)
