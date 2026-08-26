"""재관여율 (`re_engagement_rate`) — 읽기 전용 실측 (근거 대장 §7.2, A3 — Wrosch 2003).

**A3**: 이탈(disengagement)과 재관여(re-engagement)는 서로 다른 역량이다 — 회복 카드를
수락했다고 재관여가 보장되지 않는다. `recovery_followthrough_rate`(M2, 파생 카드 자체의
완주)와는 **다른** 질문이다: PARK 는 애초에 파생 카드를 안 만들고(`_GROUP_TO_SOURCE` 밖),
CARRY_OVER 조차 "그 파생 카드"가 아니라 "그 목표로 다시 돌아왔는가"(같은 goal 계보 어디든
완주)를 넓게 잰다 — 특정 카드 하나의 성패가 아니라 그 목표 자체에 대한 재관여 신호.

**정의**: 채택(accepted/edited)된 PARK/CARRY_OVER 중 재관여 앵커(`re_engagement_anchor_at`,
S8 #336)가 **이미 도래한**(≤ 지금) 건 중, 앵커 이후 7일 내 같은 goal 계보 카드가
done/over_done 으로 완주된 비율.

"앵커 도래"만 분모에 넣는 이유: 아직 앵커가 안 온 건은 재관여 여부를 아직 판정할 수 없다
(미래) — 넣으면 "아직 기회가 없었던 것"이 전부 미완주로 새어 들어가 지표가 구조적으로
낮아진다. `re_engagement_anchor_at IS NULL`(S8 이전 결정)도 같은 이유로 제외한다 — 그 행은
앵커 자체가 없어 "도래"를 판정할 기준이 없다(`report_recovery_followthrough.py` 의
`recovery_decided_at` 근사는 M2 전용 — 이 지표는 실제 설계된 앵커만 본다).

goal 계보가 없는(습관/인박스/수동 출처) 원본 카드는 재관여를 판정할 "계보" 자체가 없어
보수적으로 미완주 처리한다(`report_recovery_followthrough.py` 의 PARK-no-goal 처리와 같은
관례) — 몇 건을 그렇게 처리했는지 항상 출력한다.

**아무것도 쓰지 않는다** — SELECT 뿐. `--apply` 같은 옵션 자체가 없다.

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.report_re_engagement
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.recovery_attempt import (
    ADOPTED_DECISION_VALUES,
    RECOVERY_SUCCESS_STATUSES,
    RecoveryAttempt,
)
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.schemas.common import now_kst, to_kst

if TYPE_CHECKING:
    from datetime import datetime

_RE_ENGAGEMENT_GROUPS = ("PARK", "CARRY_OVER")

# 근거 대장 §7.2 "앵커 이후 7일 내" — report_recovery_followthrough.py 의 _PARK_WINDOW 와 동일.
_RE_ENGAGEMENT_WINDOW = timedelta(days=7)


class AnchoredAttempt(NamedTuple):
    anchor_at: datetime
    original_goal_id: UUID | None


async def _fetch_anchor_arrived_attempts(
    session: AsyncSession, now: datetime
) -> list[AnchoredAttempt]:
    """채택된 PARK/CARRY_OVER 중 앵커가 이미 도래한 것 — 원본 카드의 goal_id 포함."""
    stmt = (
        select(RecoveryAttempt.re_engagement_anchor_at, ActionItem.goal_id)
        .join(ExecutionEvent, ExecutionEvent.id == RecoveryAttempt.execution_id)
        .join(ActionItem, ActionItem.id == ExecutionEvent.action_item_id)
        .where(
            RecoveryAttempt.recovery_option_group.in_(_RE_ENGAGEMENT_GROUPS),
            RecoveryAttempt.user_decision.in_(ADOPTED_DECISION_VALUES),
            RecoveryAttempt.re_engagement_anchor_at.is_not(None),
            RecoveryAttempt.re_engagement_anchor_at <= now,
        )
    )
    rows = (await session.execute(stmt)).all()
    return [
        AnchoredAttempt(anchor_at=anchor_at, original_goal_id=goal_id)
        for anchor_at, goal_id in rows
    ]


async def _fetch_goal_sibling_success_starts(
    session: AsyncSession, goal_ids: set[UUID]
) -> dict[UUID, list[datetime]]:
    """goal_id → 그 계보의 done/over_done 실행들의 plan_start_at 목록."""
    if not goal_ids:
        return {}
    stmt = (
        select(ActionItem.goal_id, ExecutionEvent.plan_start_at)
        .join(ExecutionEvent, ExecutionEvent.action_item_id == ActionItem.id)
        .where(
            ActionItem.goal_id.in_(goal_ids),
            ExecutionEvent.completion_status.in_(RECOVERY_SUCCESS_STATUSES),
        )
    )
    out: dict[UUID, list[datetime]] = defaultdict(list)
    for goal_id, plan_start_at in (await session.execute(stmt)).all():
        out[goal_id].append(plan_start_at)
    return out


def _re_engaged(
    anchor_at: datetime,
    goal_id: UUID | None,
    success_starts_by_goal: dict[UUID, list[datetime]],
) -> bool:
    """앵커 이후 재관여 창 안에 같은 goal 계보 카드가 완주했는가."""
    if goal_id is None:
        return False
    deadline = anchor_at + _RE_ENGAGEMENT_WINDOW
    return any(anchor_at < ts <= deadline for ts in success_starts_by_goal.get(goal_id, []))


async def _preview(session: AsyncSession) -> None:
    now = now_kst()
    print(f"기준 시각: {now.isoformat()}")
    print(
        "분모: 채택된 PARK/CARRY_OVER 중 재관여 앵커가 이미 도래한 건. "
        "분자: 앵커 이후 7일 내 같은 goal 계보 카드가 done/over_done."
    )
    print()

    attempts = await _fetch_anchor_arrived_attempts(session, now)
    if not attempts:
        print("앵커가 도래한 PARK/CARRY_OVER 채택 0건 — 잴 데이터가 없다.")
        return

    goal_ids = {a.original_goal_id for a in attempts if a.original_goal_id is not None}
    no_goal_n = sum(1 for a in attempts if a.original_goal_id is None)
    success_by_goal = await _fetch_goal_sibling_success_starts(session, goal_ids)

    total = len(attempts)
    re_engaged_n = sum(
        1 for a in attempts if _re_engaged(a.anchor_at, a.original_goal_id, success_by_goal)
    )
    rate = re_engaged_n / total

    print(f"■ 분모(앵커 도래, 채택된 PARK/CARRY_OVER) = {total}건")
    print(f"■ re_engagement_rate = {re_engaged_n:4d} / {total} = {rate:.1%}")
    if no_goal_n:
        print(
            f"  ⚠️ 이 중 {no_goal_n}건은 원본 카드에 goal 계보가 없어 재관여 판정 불가"
            "(습관/인박스/수동 출처) — 보수적으로 미재관여 처리됨"
        )
    print()

    print(
        "※ 이 스크립트는 아무것도 쓰지 않았다. 지표 정의는 "
        "docs/experiments/experiment-plan-v1.md §5 M5 를 따른다."
    )


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[report-re-engagement] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
