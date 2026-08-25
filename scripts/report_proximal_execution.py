"""알림 후 60분 내 카드 실행률 (`proximal_execution_rate_60m`) — 읽기 전용 실측.

근거 대장 §7.2 대체 지표 표의 마지막 줄: "알림 후 60분 내 해당 카드 실행 발생률 —
D3, 단 `notification_sends.target_action_item_id` 신설이 선행 조건". 그 컬럼은
#335 로 이미 신설됐다 — 이제 이 리포트가 가능하다.

**D3**(Bell et al. 2023): 알림 후 1시간 내 앱 오픈이 3.5배. 60분을 "근접"의 경계로 쓰는
직접 근거다. 이 리포트는 "앱 오픈"이 아니라 "그 알림이 가리킨 카드의 실행이 실제로
시작됐는가"를 잰다 — 우리에게 더 직접적인 행동 신호.

**범위**: `pre_card` 클래스만 본다 — `evening_reflection`/`morning_brief` 는 특정 카드
하나를 가리키지 않아(`target_action_item_id` 가 항상 NULL, `notify_sweeps.py` 참고)
이 지표의 정의(카드 실행) 자체가 성립하지 않는다. 회복으로 파생된 카드인지 원래
계획된 카드인지도 가리지 않는다 — `pre_card` 스윕이 애초에 그 둘을 구분해서 보내지
않기 때문에(모든 `scheduled` 블록에 동일하게 적용), 이 지표를 회복 전용으로 좁히면
분모 자체가 왜곡된다.

**분자/분모**:
- 분모: `notification_class='pre_card'` AND `target_action_item_id IS NOT NULL` 인 발송.
- 분자: 그 action_item 의 `ExecutionEvent.actual_start_at` 이
  `[sent_at, sent_at + 60분]` 안에 있는 것이 하나라도 있는가.

**한계 (정직하게 밝힘)**:
- **인과가 아니라 상관이다.** `opened_at` 클릭 추적(#335 로 컬럼은 있지만 채우는 FE
  콜백이 아직 없음)이 없어 "사용자가 그 알림을 실제로 봤는지"는 모른다 — 원래도
  그 시각에 시작할 계획이었을 수 있다(`pre_card` 자체가 시작 2~7분 전에 나가므로
  상관은 애초에 높게 나올 수밖에 없는 구조다). D3 원문도 관측 연구다.
- 같은 action_item 에 여러 실행(재시도 등)이 있으면 **하나라도** 창 안에 들면 분자로
  센다 — "그 알림 근처에 뭔가는 시작됐다"는 가장 관대한 판정.
- pre_card 는 opt-in(`preCardEnabled`) 이라 분모 자체가 옵트인한 사용자로 편향돼 있다.

**아무것도 쓰지 않는다** — SELECT 뿐. `--apply` 같은 옵션 자체가 없다.

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.report_proximal_execution
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.notification_send import NotificationSend
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.schemas.common import now_kst, to_kst

if TYPE_CHECKING:
    from datetime import datetime

# D3(Bell et al. 2023) — 알림 후 1시간 내 앱 오픈 3.5배. 근접의 경계.
PROXIMAL_WINDOW = timedelta(minutes=60)


class NotificationRow(NamedTuple):
    action_item_id: UUID
    sent_at: datetime


def _had_proximal_execution(
    sent_at: datetime, starts: list[datetime], *, window: timedelta = PROXIMAL_WINDOW
) -> bool:
    """`sent_at` 이후 `window` 안에 시작된 실행이 하나라도 있는가."""
    deadline = sent_at + window
    return any(sent_at <= ts <= deadline for ts in starts)


async def _fetch_pre_card_notifications(session: AsyncSession) -> list[NotificationRow]:
    stmt = select(NotificationSend.target_action_item_id, NotificationSend.sent_at).where(
        NotificationSend.notification_class == "pre_card",
        NotificationSend.target_action_item_id.is_not(None),
    )
    rows = (await session.execute(stmt)).all()
    return [NotificationRow(action_item_id=aid, sent_at=sent_at) for aid, sent_at in rows]


async def _fetch_actual_starts(
    session: AsyncSession, action_item_ids: set[UUID]
) -> dict[UUID, list[datetime]]:
    if not action_item_ids:
        return {}
    stmt = select(ExecutionEvent.action_item_id, ExecutionEvent.actual_start_at).where(
        ExecutionEvent.action_item_id.in_(action_item_ids),
        ExecutionEvent.actual_start_at.is_not(None),
    )
    out: dict[UUID, list[datetime]] = defaultdict(list)
    for action_item_id, actual_start_at in (await session.execute(stmt)).all():
        out[action_item_id].append(actual_start_at)
    return out


async def _preview(session: AsyncSession) -> None:
    print(f"기준 시각: {now_kst().isoformat()}")
    print(
        "분모: notification_class='pre_card' 이고 target_action_item_id 가 채워진 발송. "
        "분자: 그 카드의 실행이 발송 후 60분 안에 시작됐는가."
    )
    print()

    notifications = await _fetch_pre_card_notifications(session)
    if not notifications:
        print(
            "target_action_item_id 가 채워진 pre_card 발송 0건 — 잴 데이터가 없다"
            "(#335 배포 이후 발송분부터 채워진다)."
        )
        return

    action_item_ids = {n.action_item_id for n in notifications}
    starts_by_action = await _fetch_actual_starts(session, action_item_ids)

    total = len(notifications)
    proximal_n = sum(
        1
        for n in notifications
        if _had_proximal_execution(n.sent_at, starts_by_action.get(n.action_item_id, []))
    )
    rate = proximal_n / total

    print(f"■ 분모(pre_card 발송, target 있음) = {total}건")
    print(f"■ 근접 실행률(proximal_execution_rate_60m) = {proximal_n:4d} / {total} = {rate:.1%}")
    print()
    print(
        "※ 상관일 뿐 인과가 아니다 — pre_card 는 원래 시작 2~7분 전에 나가므로 상관은 "
        "구조적으로 높게 나온다. opened_at(클릭 추적)이 아직 안 채워져 있어 '알림을 봤기 "
        "때문에' 시작했는지는 이 리포트만으로는 알 수 없다."
    )
    print()
    print("※ 이 스크립트는 아무것도 쓰지 않았다. 지표 정의는 근거 대장 §7.2 를 따른다.")


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[report-proximal-execution] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
