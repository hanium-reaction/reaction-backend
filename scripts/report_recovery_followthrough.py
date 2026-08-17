"""회복 수락률 vs 완주율 갭 — 읽기 전용 실측 (회복 재설계 L1-5 예비 실행).

배경: `weekly_review.py::compute_weekly_kpis` 의 `resilience_rate` 는 실패 중 회복 카드를
**수락**한 비율이다. 수락은 [수락] 버튼을 누른 것이지 실제로 다시 했다는 뜻이 아니다.
가장 쉬운 카드(DOWNSCOPE)·가장 부담 없는 카드(PARK)만 밀어도 오르는 지표라, 대표 KPI 로
쓰면 시스템이 "완주시키기"가 아니라 "누르게 만들기"를 최적화하게 된다.

이 스크립트는 그 갭이 **실제로 존재하는지, 얼마나 되는지**를 지금 있는 데이터로 잰다.
`docs/research/recovery-evidence-base.md` §7 / `docs/experiments/experiment-plan-v1.md`
§5 M1·M2·M3 의 예비 실행이며, 그 문서들의 지표 정의를 그대로 따른다.

완주(followthrough)의 정의는 그룹마다 다르다 — `api/routes/recovery.py::_GROUP_TO_SOURCE`
가 **DOWNSCOPE 와 CARRY_OVER 만** 파생 `action_item` 을 만들기 때문이다:
- DOWNSCOPE / CARRY_OVER: 파생 카드(`resulting_action_item_id`)의 실행이
  `done`/`over_done` 으로 끝났는가 (RECOVERY_SUCCESS_STATUSES)
- RESCHEDULE / PARK: `recovery_attempts.recovery_result == 'completed'`
  (그 그룹은 파생 카드가 없어 이 컬럼이 유일한 완주 신호다)
단일 정의(예: "파생 카드 완주"만)로 재면 RESCHEDULE/PARK 수락은 **항상 실패로 계산**된다
— 수락률 편향을 고치려다 반대 편향을 만드는 함정이라 그룹별 정의를 여기서 명문화한다.

**분모의 한계 (정직하게 밝힘)**: `recovery_attempts.first_viewed_at` 컬럼이 아직 없어서
"카드가 실제로 노출됐는가"를 잴 수 없다. 여기서는 **"카드가 생성됐는가"**(=해당
execution_id 에 recovery_attempts 행이 하나라도 있는가)를 분모로 쓴다 — ITT 분모를
과소가 아니라 과대 방향으로 잡는 보수적 선택이다.

**아무것도 쓰지 않는다** — SELECT 뿐. `--apply` 같은 옵션 자체가 없다
(선례: `preview_card_target_date_backfill.py`).

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.report_recovery_followthrough
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.recovery_attempt import (
    ADOPTED_DECISION_VALUES,
    RECOVERY_SUCCESS_STATUSES,
    RecoveryAttempt,
)
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.schemas.common import now_kst, to_kst

# 파생 카드를 만드는 그룹만 "파생 카드 완주"로 잰다 — 출처: routes/recovery.py::_GROUP_TO_SOURCE.
# 그 매핑이 바뀌면 여기도 같이 바뀌어야 한다(독자 재정의 아님, 관측된 레포 사실의 미러).
_CARD_BEARING_GROUPS = ("DOWNSCOPE", "CARRY_OVER")


class AttemptRow(NamedTuple):
    """RecoveryAttempt 에서 집계에 필요한 컬럼만 뽑은 것."""

    execution_id: UUID
    user_id: UUID
    option_group: str
    user_decision: str
    recovery_result: str
    resulting_action_item_id: UUID | None


async def _fetch_attempts(session: AsyncSession) -> list[AttemptRow]:
    stmt = select(
        RecoveryAttempt.execution_id,
        RecoveryAttempt.user_id,
        RecoveryAttempt.recovery_option_group,
        RecoveryAttempt.user_decision,
        RecoveryAttempt.recovery_result,
        RecoveryAttempt.resulting_action_item_id,
    )
    rows = (await session.execute(stmt)).all()
    return [AttemptRow(*r) for r in rows]


async def _fetch_derived_done_ids(session: AsyncSession, action_item_ids: set[UUID]) -> set[UUID]:
    """파생 카드(action_item) 중 실행이 done/over_done 으로 끝난 것들의 id 집합."""
    if not action_item_ids:
        return set()
    stmt = (
        select(ExecutionEvent.action_item_id)
        .where(
            ExecutionEvent.action_item_id.in_(action_item_ids),
            ExecutionEvent.completion_status.in_(RECOVERY_SUCCESS_STATUSES),
        )
        .distinct()
    )
    return set((await session.execute(stmt)).scalars().all())


def _accepted_group(rows: list[AttemptRow]) -> str | None:
    """이 실행에서 수락(ADOPTED)된 카드의 그룹. 없으면 None."""
    for r in rows:
        if r.user_decision in ADOPTED_DECISION_VALUES:
            return r.option_group
    return None


def _is_followthrough(rows: list[AttemptRow], derived_done_ids: set[UUID]) -> bool:
    """이 실행이 회복을 실제로 완주했는가 — 그룹별 정의 (모듈 docstring 참조)."""
    for r in rows:
        if r.user_decision not in ADOPTED_DECISION_VALUES:
            continue
        if r.option_group in _CARD_BEARING_GROUPS:
            if r.resulting_action_item_id is not None and r.resulting_action_item_id in derived_done_ids:
                return True
        else:
            if r.recovery_result == "completed":
                return True
    return False


async def _preview(session: AsyncSession) -> None:
    print(f"기준 시각: {now_kst().isoformat()}")
    print(
        "분모: recovery_attempts 행이 1건 이상 있는 실행(execution_id) — 카드가 "
        "'생성'된 것. 'first_viewed_at' 컬럼이 없어 '노출'까지는 못 잰다(과대추정 방향)."
    )
    print()

    attempts = await _fetch_attempts(session)
    if not attempts:
        print("recovery_attempts 0건 — 잴 데이터가 없다.")
        return

    by_execution: dict[UUID, list[AttemptRow]] = defaultdict(list)
    for a in attempts:
        by_execution[a.execution_id].append(a)

    card_bearing_action_ids = {
        r.resulting_action_item_id
        for rows in by_execution.values()
        for r in rows
        if r.option_group in _CARD_BEARING_GROUPS and r.resulting_action_item_id is not None
    }
    derived_done_ids = await _fetch_derived_done_ids(session, card_bearing_action_ids)

    total = len(by_execution)
    accepted_n = 0
    followthrough_n = 0
    by_group_total: Counter[str] = Counter()
    by_group_followthrough: Counter[str] = Counter()

    for rows in by_execution.values():
        group = _accepted_group(rows)
        if group is not None:
            accepted_n += 1
            by_group_total[group] += 1
        ft = _is_followthrough(rows, derived_done_ids)
        if ft:
            followthrough_n += 1
            if group is not None:
                by_group_followthrough[group] += 1

    acceptance_rate = accepted_n / total
    followthrough_rate = followthrough_n / total
    gap_pp = (acceptance_rate - followthrough_rate) * 100

    print(f"■ 분모(카드가 생성된 실패 실행) = {total}건")
    print(
        f"■ 수락률(recovery_acceptance_rate)   = {accepted_n:4d} / {total} = {acceptance_rate:.1%}"
    )
    print(
        f"■ 완주율(recovery_followthrough_rate) = {followthrough_n:4d} / {total} = {followthrough_rate:.1%}"
    )
    print(f"■ 갭(drop_after_accept) = {gap_pp:+.1f}%p  ← 수락은 했는데 안 한 몫")
    print()

    print("■ 그룹별 (수락 중 완주 비율 — 그룹마다 완주 정의가 다름을 잊지 말 것)")
    for g in sorted(by_group_total, key=lambda g: -by_group_total[g]):
        n = by_group_total[g]
        f = by_group_followthrough[g]
        rate = f / n if n else 0.0
        note = "" if g in _CARD_BEARING_GROUPS else "  (파생 카드 없음 — recovery_result 로만 판정)"
        print(f"  {g:12s} 수락 {n:3d}건 → 완주 {f:3d}건 ({rate:.1%}){note}")
    print()

    print(
        "※ 이 스크립트는 아무것도 쓰지 않았다. 지표 정의는 "
        "docs/experiments/experiment-plan-v1.md §5 (M1/M2/M3) 를 따른다."
    )


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[report-recovery-followthrough] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
