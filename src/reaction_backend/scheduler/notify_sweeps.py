"""알림 cron sweep 2종 — 저녁 회고 알림 · pre_card 알림 (Issue #20).

발송 판단은 전부 `safety/push_gate.py` 를 거친다(주 ≤3건 · 23~07 금지 · 클래스 dedup ·
사용자 advisory lock). 여기는 **누구에게 · 언제 · 무슨 내용**만 정한다.

트랜잭션 규약 (ADR-0006 §8):
- **사용자(블록) 단위 commit** — 실발송은 트랜잭션 밖 부수효과라, 배치 말미 일괄 commit
  은 한 번의 실패로 그 폴의 발송 이력 전원을 잃고 다음 폴이 같은 날 재발송하게 만든다
  (클래스 dedup 잠금 붕괴). 건당 commit 으로 유실 창을 '발송~commit 사이 크래시 1명 분'
  (dual-write 의 불가피한 최소 창)으로 좁힌다. 게이트의 advisory lock 도 이 commit 이
  풀어준다.
- **except 에서 rollback** — DB 예외로 세션이 aborted 로 남으면 이후 사용자 전원이
  PendingRollbackError 로 죽어 실패 격리가 허상이 된다.

저녁 회고 알림 (19~23시 5분 폴):
- 사용자별 `evening_reflection_time` 이후 첫 폴에서 발송 — "`now >= 유효시각` 이고 오늘
  아직 안 보냄"(level-triggered). 재시작으로 폴을 놓쳐도 다음 폴이 주워 담고, 중복은
  게이트의 KST 달력일 dedup 이 막는다.
- 유효시각 = `min(설정시각, 22:55)` — 설정은 19~23시 저장이 가능한데(계약 §15) 22:56
  이후 설정은 클램프 없이는 첫 통과 폴이 23시대(quiet hours)에 떨어져 **영영 발송되지
  않는다**. 22:55(quiet 전 마지막 폴)가 그날의 마지막 기회다 (ADR-0006 §7).
- **회고할 카드가 있을 때만** — 빈 알림은 소음이고 주 3건 예산에서 진짜 기회를 밀어낸다
  (ADR-0006 §4). 창 경계는 회고 화면·만료 cron 과 동일한 `pending_reflection_since`.
- **일요일은 문구·딥링크가 갈라진다** — 같은 클래스·같은 발송 조건에 주간 리포트를
  "얹는다"(ADR-0008 §4, §8 "F"). 새 알림 클래스도, 새 발송 조건도 추가하지 않는다.

pre_card 알림 (종일 5분 폴):
- `[now+2분, now+7분)` 에 시작하는 `scheduled` 블록 — "카드 2분 전" (architecture.md §6).
  `started` 는 이미 착수한 카드라 제외. 클래스 dedup 이 하루 1건으로 자른다.

morning_brief 알림 — T2 재관여 (06~10시 5분 폴, 근거 대장 §6.2):
- 오늘이 `recovery_attempts.re_engagement_anchor_at` 인 채택 PARK/CARRY_OVER 가 있는
  사용자에게만 — 새 알림 클래스를 만들지 않고 `morning_brief` 슬롯을 재사용한다. 일반
  "좋은 아침" 인사가 아니다: 대상이 없으면 그날은 아예 안 보낸다(빈 알림 금지 원칙은
  evening_reflection 과 동일). 여러 건이 겹치면 하나로 묶어 보낸다(클래스 dedup 이 하루
  1건이라 어차피 두 번은 못 간다).
- 유효시각 = `max(설정시각, 07:00)` — evening 의 22:55 클램프와 대칭인 반대쪽 버그:
  `morning_brief_time` 은 06:00~10:00 저장이 가능한데(계약 §15) 06:00~06:59 설정은
  클램프 없이는 그 사용자의 첫 통과 폴이 quiet hours([23:00,07:00)) 안이라 **영영
  발송되지 않는다**.

`clock`: 게이트에 넘길 발송 시각. 기본 `now_kst` 를 **사용자마다 새로** 읽는다 — 폴
시작 시각(now_kst_dt)으로 고정하면 발송이 밀리는 동안(느린 endpoint 등) 벽시계가 23시를
넘어도 quiet hours 를 통과하고 sent_at 도 거짓이 된다. 테스트는 고정 시계를 주입한다.

문구 톤: "Be on your side, not on your case" (AGENTS.md §1) — 재촉·죄책감 표현 금지.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from reaction_backend.db.models.notification_send import NOTIFICATION_ID_PREFIX
from reaction_backend.safety import push_gate
from reaction_backend.scheduler.expire_reflections import pending_reflection_since
from reaction_backend.schemas.common import now_kst

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
    from reaction_backend.integrations.web_push import WebPushSender
    from reaction_backend.repositories.action_item_repo import ActionItemRepo
    from reaction_backend.repositories.execution_repo import ExecutionRepo
    from reaction_backend.repositories.notification_repo import NotificationRepo
    from reaction_backend.repositories.notification_send_repo import NotificationSendRepo
    from reaction_backend.repositories.recovery_repo import RecoveryRepo
    from reaction_backend.repositories.user_repo import UserRepo

_log = logging.getLogger(__name__)

# "카드 2분 전" (architecture.md §6). 5분 폴 간격과 짝 — 실제 리드타임은 2~7분.
PRE_CARD_LEAD = timedelta(minutes=2)
NOTIFY_POLL_INTERVAL = timedelta(minutes=5)

# quiet hours([23:00,07:00)) 전의 마지막 발송 기회 — 5분 폴 격자의 22:55.
# 이보다 늦은 evening 설정(22:56~23:00)은 이 시각으로 클램프해 발송한다.
EVENING_LAST_SEND = time(22, 55)

# quiet hours 가 끝나는 07:00 — 이보다 이른 morning 설정(06:00~06:59)은 이 시각으로
# 클램프해 발송한다(모듈 docstring 의 T2 항목 참고).
MORNING_BRIEF_EARLIEST_SEND = time(7, 0)


@dataclass(slots=True)
class NotifySweepResult:
    """알림 sweep 결과 — sent 는 실발송, skipped 는 게이트/조건에 걸러진 수."""

    total: int
    sent: int
    skipped: int
    failed: int


def _evening_payload(
    notification_id: UUID, pending_count: int, *, is_sunday: bool
) -> dict[str, Any]:
    """일요일엔 같은 회고 알림에 주간 리포트를 얹는다 (ADR-0008 §4, §8 "F").

    새 알림 클래스를 만들지 않는다 — 발송 조건("회고할 카드가 있을 때만", 클래스 dedup,
    주 ≤3건 예산)은 그대로고 **문구·딥링크만** 요일에 따라 갈라진다. 리포트 자체의 숫자는
    여기서 계산하지 않는다 — 실제 내용은 `GET /reviews/weekly`(딥링크 도착지)가 보여주고,
    push body 는 짧은 예고만 한다(사용자별 만다라 조회를 이 sweep 루프에 추가로 얹지 않음).

    `id`(문자열) — 이 알림의 `notification_sends` PK 를 미리 실어 보낸다(근거 대장 §6.1).
    FE 가 push `notificationclick` 에서 이 값을 그대로 되돌려주면 서버가 `opened_at` 을
    채울 수 있다 — 아직 그 콜백 자체는 없다.
    """
    if is_sunday:
        return {
            "id": f"{NOTIFICATION_ID_PREFIX}{notification_id}",
            "class": "evening_reflection",
            "title": "오늘의 회고 + 이번 주 리포트",
            "body": f"돌아볼 카드 {pending_count}장과 이번 주 만다라 리포트가 준비됐어요.",
            "url": "/reviews/weekly",
        }
    return {
        "id": f"{NOTIFICATION_ID_PREFIX}{notification_id}",
        "class": "evening_reflection",
        "title": "오늘의 회고 시간이에요",
        "body": f"돌아볼 카드가 {pending_count}장 있어요.",
        "url": "/reflection",
    }


def _pre_card_payload(notification_id: UUID, title: str, start_hhmm: str) -> dict[str, Any]:
    return {
        "id": f"{NOTIFICATION_ID_PREFIX}{notification_id}",
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
    clock: Callable[[], datetime] | None = None,
) -> NotifySweepResult:
    """19~23시 5분 폴 — 유효시각(≤22:55) 지난 사용자에게 회고 알림 (있을 때만, 하루 1건)."""
    read_clock = clock or now_kst
    users = await user_repo.list_active()
    sent = skipped = failed = 0
    for user in users:
        try:
            setting = await notif_repo.get_by_user(user.id)
            # 행이 없으면 구독한 적도 없는 사용자 (구독이 행에 담긴다) — 만들지 않는다.
            if setting is None or setting.push_subscription is None:
                skipped += 1
                continue
            effective = min(setting.evening_reflection_time, EVENING_LAST_SEND)
            if now_kst_dt.time() < effective:
                skipped += 1
                continue
            pending = await execution_repo.list_pending_reflection(
                user.id, since=pending_reflection_since(now_kst_dt.date())
            )
            if not pending:
                skipped += 1
                continue
            notification_id = uuid4()
            result = await push_gate.send_push(
                setting=setting,
                notification_class="evening_reflection",
                notification_id=notification_id,
                payload=_evening_payload(
                    notification_id, len(pending), is_sunday=now_kst_dt.weekday() == 6
                ),
                now=read_clock(),
                send_repo=send_repo,
                sender=sender,
                # 특정 카드 하나가 아니라 회고 대기 카드 전체에 대한 알림 — target 없음.
                target_action_item_id=None,
            )
            sent += 1 if result.sent else 0
            skipped += 0 if result.sent else 1
            # 사용자 단위 commit — 이력 즉시 내구화 + advisory lock 해제 (모듈 docstring).
            await session.commit()
        except Exception:  # noqa: BLE001 — 한 사용자 실패가 배치를 멈추지 않게
            failed += 1
            _log.exception("evening_reflection notify failed for user %s", user.id)
            await session.rollback()  # aborted 세션이 다음 사용자를 전멸시키지 않게
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
    clock: Callable[[], datetime] | None = None,
) -> NotifySweepResult:
    """5분 폴 — 2~7분 뒤 시작하는 scheduled 블록의 주인에게 pre_card 알림 (opt-in)."""
    read_clock = clock or now_kst
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
            notification_id = uuid4()
            result = await push_gate.send_push(
                setting=setting,
                notification_class="pre_card",
                notification_id=notification_id,
                payload=_pre_card_payload(notification_id, title, start_hhmm),
                now=read_clock(),
                send_repo=send_repo,
                sender=sender,
                target_action_item_id=block.action_item_id,
            )
            sent += 1 if result.sent else 0
            skipped += 0 if result.sent else 1
            await session.commit()  # 블록 단위 commit — 모듈 docstring
        except Exception:  # noqa: BLE001
            failed += 1
            _log.exception("pre_card notify failed for block %s", block.id)
            await session.rollback()
    if sent or failed:
        _log.info(
            "pre_card notify: total=%d sent=%d skipped=%d failed=%d",
            len(blocks),
            sent,
            skipped,
            failed,
        )
    return NotifySweepResult(total=len(blocks), sent=sent, skipped=skipped, failed=failed)


def _morning_brief_payload(
    notification_id: UUID, due_count: int, *, title: str | None
) -> dict[str, Any]:
    body = (
        f"'{title}', 다시 보러 갈까요?"
        if due_count == 1 and title is not None
        else f"밀어뒀던 카드 {due_count}개를 다시 보러 갈까요?"
    )
    return {
        "id": f"{NOTIFICATION_ID_PREFIX}{notification_id}",
        "class": "morning_brief",
        "title": "새로운 하루, 다시 시작해볼까요?",
        "body": body,
        "url": "/today",
    }


async def _due_re_engagement_title(
    due: list[RecoveryAttempt],
    *,
    user_id: UUID,
    recovery_repo: RecoveryRepo,
    action_repo: ActionItemRepo,
) -> tuple[str | None, UUID | None]:
    """알림 문구에 실을 카드 제목 — CARRY_OVER 는 새 카드, PARK 는 원본 카드에서 가져온다.

    PARK 는 `_GROUP_TO_SOURCE` 밖이라 `resulting_action_item_id` 가 없다(새 카드를 안
    만든다, routes/recovery.py) — 이때만 원본(`execution_event.action_item`)으로 폴백.
    여러 건이 겹치면 첫 건만 제목으로 쓰고 나머지는 개수로 뭉뚱그린다(`_morning_brief_payload`).
    """
    first = due[0]
    if first.resulting_action_item_id is not None:
        resulting = await action_repo.get_by_id(user_id, first.resulting_action_item_id)
        if resulting is not None:
            return resulting.title, resulting.id
    execution = await recovery_repo.get_execution(user_id, first.execution_id)
    if execution is not None:
        original = await action_repo.get_by_id(user_id, execution.action_item_id)
        if original is not None:
            return original.title, None
    return None, None


async def run_morning_brief_notify_sweep(
    now_kst_dt: datetime,
    *,
    user_repo: UserRepo,
    notif_repo: NotificationRepo,
    recovery_repo: RecoveryRepo,
    action_repo: ActionItemRepo,
    send_repo: NotificationSendRepo,
    sender: WebPushSender,
    session: AsyncSession,
    clock: Callable[[], datetime] | None = None,
) -> NotifySweepResult:
    """06~10시 5분 폴 — 오늘이 anchor 인 PARK/CARRY_OVER 재관여 대상에게 발송(T2, 모듈 docstring)."""
    read_clock = clock or now_kst
    users = await user_repo.list_active()
    sent = skipped = failed = 0
    for user in users:
        try:
            setting = await notif_repo.get_by_user(user.id)
            if setting is None or setting.push_subscription is None:
                skipped += 1
                continue
            effective = max(setting.morning_brief_time, MORNING_BRIEF_EARLIEST_SEND)
            if now_kst_dt.time() < effective:
                skipped += 1
                continue
            due = await recovery_repo.list_due_re_engagement(user.id, now_kst_dt.date())
            if not due:
                skipped += 1
                continue
            title, target_action_item_id = await _due_re_engagement_title(
                due, user_id=user.id, recovery_repo=recovery_repo, action_repo=action_repo
            )
            notification_id = uuid4()
            result = await push_gate.send_push(
                setting=setting,
                notification_class="morning_brief",
                notification_id=notification_id,
                payload=_morning_brief_payload(notification_id, len(due), title=title),
                now=read_clock(),
                send_repo=send_repo,
                sender=sender,
                target_action_item_id=target_action_item_id,
            )
            sent += 1 if result.sent else 0
            skipped += 0 if result.sent else 1
            await session.commit()  # 사용자 단위 commit — 모듈 docstring
        except Exception:  # noqa: BLE001
            failed += 1
            _log.exception("morning_brief notify failed for user %s", user.id)
            await session.rollback()
    if sent or failed:
        _log.info(
            "morning_brief notify: total=%d sent=%d skipped=%d failed=%d",
            len(users),
            sent,
            skipped,
            failed,
        )
    return NotifySweepResult(total=len(users), sent=sent, skipped=skipped, failed=failed)
