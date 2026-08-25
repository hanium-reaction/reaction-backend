"""회복 수락률 vs 완주율 갭 — 읽기 전용 실측 (회복 재설계 L1-5 예비 실행).

배경: `weekly_review.py::compute_weekly_kpis` 의 `resilience_rate` 는 실패 중 회복 카드를
**수락**한 비율이다. 수락은 [수락] 버튼을 누른 것이지 실제로 다시 했다는 뜻이 아니다.
가장 쉬운 카드(DOWNSCOPE)·가장 부담 없는 카드(PARK)만 밀어도 오르는 지표라, 대표 KPI 로
쓰면 시스템이 "완주시키기"가 아니라 "누르게 만들기"를 최적화하게 된다.

이 스크립트는 그 갭이 **실제로 존재하는지, 얼마나 되는지**를 지금 있는 데이터로 잰다.
`docs/research/recovery-evidence-base.md` §7 / `docs/experiments/experiment-plan-v1.md`
§5 M1·M2·M3 의 예비 실행이며, 그 문서들의 지표 정의를 그대로 따른다.

완주(followthrough)의 정의는 그룹마다 다르다 — `api/routes/recovery.py::_GROUP_TO_SOURCE`
가 **DOWNSCOPE 와 CARRY_OVER 만** 파생 `action_item` 을 만들기 때문이다 (근거 대장 §7.2):
- DOWNSCOPE / CARRY_OVER: 파생 카드(`resulting_action_item_id`)의 실행이
  `done`/`over_done` 으로 끝났는가 (RECOVERY_SUCCESS_STATUSES)
- RESCHEDULE: 원본 카드는 그대로 남고(`action_item.status` 불변, AGENTS.md §2) S15 주간
  편집기로 그 카드의 블록을 옮겨 재실행하는 것이 실제 경로다(`routes/recovery.py`
  `_validated_target` 주석 — "조정은 문구가 아니라 시간이고 그 경로는 S15"). 따라서 완주
  신호는 **같은 action_item 의, 회복 결정 이후 첫 성공 실행**이다.
- PARK: **같은 goal 계보**(원본 카드의 `goal_id`) 카드가 앵커 이후 7일 내 완주했는가.
  앵커는 `re_engagement_anchor_at`(S8, #336)을 쓰고, 그 컬럼이 아직 없던(NULL) 결정
  건은 이전과 같이 `recovery_decided_at`(결정 시각)으로 근사한다 — 옛 데이터를 조용히
  버리지 않으면서, 새 데이터부터는 설계된 앵커를 그대로 쓴다.

⚠️ **`recovery_attempts.recovery_result == 'completed'` 를 쓰지 않는다 — 구조적으로 항상
'pending' 이기 때문이다.** 그 컬럼을 채우는 유일한 생산자 `RecoveryRepo.complete_for_action`
과 포기 처리 `abandon_stale` 은 둘 다 `resulting_action_item_id` 로만 매칭하는데, 그 컬럼은
RESCHEDULE/PARK 에서 절대 채워지지 않는다(파생 카드가 없으므로). 즉 이 두 그룹의
`recovery_result` 는 영구 'pending' 이다 — 이 스크립트가 그 컬럼을 완주 신호로 썼던
이전 버전은 RESCHEDULE/PARK 수락을 **항상 실패로 계산**하는 반대 편향을 만들고 있었다.

단일 정의(예: "파생 카드 완주"만)로 재도 같은 함정에 빠진다 — 그룹별 정의를 여기서
명문화하는 이유다.

**분모 (ITT)**: 카드가 **노출된** 실행(execution_id) — 그 실행의 `recovery_attempts` 중
`first_viewed_at` 이 채워진 행이 하나라도 있는가. 이 컬럼은 마이그레이션 `09fa61fbf06f`
(실험 계획서 §1 선행조건 P6)로 신설됐고, `routes/recovery.py` 가 카드를 응답으로 내보낼 때
`RecoveryRepo.stamp_first_viewed` 로 최초 1회만 채운다.

**남은 한계 (정직하게 밝힘)**: `first_viewed_at` 은 **서버가 응답을 만든 시각**이지
클라이언트가 실제로 받아 렌더링한 시각이 아니다(`stamp_first_viewed` docstring). FE 노출
계측이 붙기 전까지 이 리포트가 말할 수 있는 건 "노출 **시도**"까지다 — 여전히 과소가 아니라
**과대** 방향의 보수적 분모다.

⚠️ **지금은 이 필터가 숫자를 바꾸지 않는다.** 유일한 생산 경로
(`generate_recovery_proposals`)가 `create_attempt` 직후 **같은 트랜잭션에서** 스탬프하므로
실무상 `first_viewed_at` ≈ `created_at` 이다. 그런데도 읽는 쪽을 컬럼에 맞춰 두는 이유는
둘이다 — ① 마이그레이션 이전에 만들어진 행이 있다면 NULL 이라 분모에서 빠지는 게 맞고,
② 카드를 만들어만 두고 바로 안 내보내는 경로(배치 선생성 등)가 나중에 생기면, 그때 이
리포트가 **조용히 틀리는 대신 자동으로 옳아진다.** 제외된 건수는 항상 출력한다(무언의
절삭 금지).

**아무것도 쓰지 않는다** — SELECT 뿐. `--apply` 같은 옵션 자체가 없다
(선례: `preview_card_target_date_backfill.py`).

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.report_recovery_followthrough
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
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

# 파생 카드를 만드는 그룹만 "파생 카드 완주"로 잰다 — 출처: routes/recovery.py::_GROUP_TO_SOURCE.
# 그 매핑이 바뀌면 여기도 같이 바뀌어야 한다(독자 재정의 아님, 관측된 레포 사실의 미러).
_CARD_BEARING_GROUPS = ("DOWNSCOPE", "CARRY_OVER")

# PARK 완주 판정 창 — 근거 대장 §7.2 "앵커 이후 7일 내".
_PARK_WINDOW = timedelta(days=7)


class AttemptRow(NamedTuple):
    """RecoveryAttempt 에서 집계에 필요한 컬럼만 뽑은 것 (원본 카드 메타 포함)."""

    execution_id: UUID
    user_id: UUID
    option_group: str
    user_decision: str
    resulting_action_item_id: UUID | None
    recovery_decided_at: datetime | None
    # 원본(실패한) action_item — ExecutionEvent 조인. RESCHEDULE 완주 판정의 대상.
    original_action_item_id: UUID
    # 원본 action_item 의 goal_id — 없으면(습관/인박스/수동) PARK 계보 판정 불가.
    original_goal_id: UUID | None
    # 카드가 응답으로 나간 시각(P6). NULL = 한 번도 안 나갔다 → ITT 분모에서 제외.
    first_viewed_at: datetime | None
    # S8(#336) 이후 채워지는 진짜 재관여 앵커 — PARK 완주 판정의 창 시작점.
    # NULL 이면(S8 이전에 결정된 행) `recovery_decided_at` 로 근사한다(`_is_followthrough`).
    re_engagement_anchor_at: datetime | None


async def _fetch_attempts(session: AsyncSession) -> list[AttemptRow]:
    stmt = (
        select(
            RecoveryAttempt.execution_id,
            RecoveryAttempt.user_id,
            RecoveryAttempt.recovery_option_group,
            RecoveryAttempt.user_decision,
            RecoveryAttempt.resulting_action_item_id,
            RecoveryAttempt.recovery_decided_at,
            ExecutionEvent.action_item_id,
            ActionItem.goal_id,
            RecoveryAttempt.first_viewed_at,
            RecoveryAttempt.re_engagement_anchor_at,
        )
        .join(ExecutionEvent, ExecutionEvent.id == RecoveryAttempt.execution_id)
        .join(ActionItem, ActionItem.id == ExecutionEvent.action_item_id)
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


async def _fetch_success_plan_starts(
    session: AsyncSession, action_item_ids: set[UUID]
) -> dict[UUID, list[datetime]]:
    """action_item_id → 성공(done/over_done) 실행들의 plan_start_at 목록.

    RESCHEDULE(원본 카드 재실행)과 PARK(goal 계보 카드 완주) 완주 판정의 공용 재료 —
    "언제" 성공했는지가 있어야 결정 시각·앵커 창과 비교할 수 있다.
    """
    if not action_item_ids:
        return {}
    stmt = select(ExecutionEvent.action_item_id, ExecutionEvent.plan_start_at).where(
        ExecutionEvent.action_item_id.in_(action_item_ids),
        ExecutionEvent.completion_status.in_(RECOVERY_SUCCESS_STATUSES),
    )
    out: dict[UUID, list[datetime]] = defaultdict(list)
    for action_item_id, plan_start_at in (await session.execute(stmt)).all():
        out[action_item_id].append(plan_start_at)
    return out


async def _fetch_goal_sibling_action_ids(
    session: AsyncSession, goal_ids: set[UUID]
) -> dict[UUID, set[UUID]]:
    """goal_id → 그 goal 계보 전체의 action_item_id 집합 (PARK "같은 goal 계보" 판정용)."""
    if not goal_ids:
        return {}
    stmt = select(ActionItem.goal_id, ActionItem.id).where(ActionItem.goal_id.in_(goal_ids))
    out: dict[UUID, set[UUID]] = defaultdict(set)
    for goal_id, action_id in (await session.execute(stmt)).all():
        out[goal_id].add(action_id)
    return out


def _was_exposed(rows: list[AttemptRow]) -> bool:
    """이 실행의 카드가 한 번이라도 사용자에게 나갔는가 — ITT 분모의 게이트.

    한 실행의 카드 2~4장은 **같은 응답으로 함께** 나가므로(`generate_recovery_proposals`),
    한 장이라도 스탬프돼 있으면 그 실행은 노출된 것이다. 전부 NULL 인 실행만 분모에서
    빠진다 — 카드가 만들어지기만 하고 사용자가 볼 기회 자체가 없었던 실행을 "회복 기회를
    줬는데 안 했다"로 세면 완주율이 구조적으로 과소평가된다.
    """
    return any(r.first_viewed_at is not None for r in rows)


def _accepted_group(rows: list[AttemptRow]) -> str | None:
    """이 실행에서 수락(ADOPTED)된 카드의 그룹. 없으면 None."""
    for r in rows:
        if r.user_decision in ADOPTED_DECISION_VALUES:
            return r.option_group
    return None


def _reschedule_followthrough(
    decided_at: datetime | None,
    original_action_item_id: UUID,
    success_plan_starts: dict[UUID, list[datetime]],
) -> bool:
    """원본 카드가 회복 결정 이후 다시 성공했는가 (파생 카드가 없는 RESCHEDULE 의 완주 신호)."""
    if decided_at is None:
        return False
    return any(ts > decided_at for ts in success_plan_starts.get(original_action_item_id, []))


def _park_followthrough(
    anchor_at: datetime | None,
    original_goal_id: UUID | None,
    success_plan_starts_by_goal: dict[UUID, list[datetime]],
) -> bool:
    """앵커 이후 7일 내 같은 goal 계보 카드가 완주했는가.

    `anchor_at` 은 호출부(`_is_followthrough`)가 이미 `re_engagement_anchor_at ??
    recovery_decided_at` 로 정리해서 넘긴다 — S8(#336) 이전에 결정된 행은 여전히
    결정 시각으로 근사한다(그 컬럼이 그때는 없었으므로).

    goal 이 없는 원본 카드(습관/인박스/수동 출처)는 "계보" 자체가 없어 판정 불가 —
    완주로 잘못 세지 않도록 보수적으로 False.
    """
    if anchor_at is None or original_goal_id is None:
        return False
    deadline = anchor_at + _PARK_WINDOW
    return any(
        anchor_at < ts <= deadline for ts in success_plan_starts_by_goal.get(original_goal_id, [])
    )


def _is_followthrough(
    rows: list[AttemptRow],
    *,
    derived_done_ids: set[UUID],
    reschedule_success: dict[UUID, list[datetime]],
    park_success_by_goal: dict[UUID, list[datetime]],
) -> bool:
    """이 실행이 회복을 실제로 완주했는가 — 그룹별 정의 (모듈 docstring 참조)."""
    for r in rows:
        if r.user_decision not in ADOPTED_DECISION_VALUES:
            continue
        if r.option_group in _CARD_BEARING_GROUPS:
            if (
                r.resulting_action_item_id is not None
                and r.resulting_action_item_id in derived_done_ids
            ):
                return True
        elif r.option_group == "RESCHEDULE":
            if _reschedule_followthrough(
                r.recovery_decided_at, r.original_action_item_id, reschedule_success
            ):
                return True
        elif r.option_group == "PARK" and _park_followthrough(
            r.re_engagement_anchor_at or r.recovery_decided_at,
            r.original_goal_id,
            park_success_by_goal,
        ):
            return True
    return False


async def _preview(session: AsyncSession) -> None:
    print(f"기준 시각: {now_kst().isoformat()}")
    print(
        "분모: 카드가 '노출'된 실행(execution_id) — first_viewed_at 이 채워진 "
        "recovery_attempts 가 1건 이상. 서버가 응답을 만든 시각이라 FE 렌더링까지는 "
        "못 잰다(여전히 과대추정 방향)."
    )
    print()

    attempts = await _fetch_attempts(session)
    if not attempts:
        print("recovery_attempts 0건 — 잴 데이터가 없다.")
        return

    by_execution: dict[UUID, list[AttemptRow]] = defaultdict(list)
    for a in attempts:
        by_execution[a.execution_id].append(a)

    # ITT 분모 게이트 — 노출된 적 없는 실행은 제외하되, 몇 건을 뺐는지 반드시 밝힌다.
    unexposed = [eid for eid, rows in by_execution.items() if not _was_exposed(rows)]
    for eid in unexposed:
        del by_execution[eid]
    print(f"분모에서 제외(노출 기록 없음): {len(unexposed)}건")
    if not by_execution:
        print("노출된 회복 카드 0건 — 잴 데이터가 없다.")
        return
    print()

    card_bearing_action_ids = {
        r.resulting_action_item_id
        for rows in by_execution.values()
        for r in rows
        if r.option_group in _CARD_BEARING_GROUPS and r.resulting_action_item_id is not None
    }
    derived_done_ids = await _fetch_derived_done_ids(session, card_bearing_action_ids)

    reschedule_action_ids = {
        r.original_action_item_id
        for rows in by_execution.values()
        for r in rows
        if r.option_group == "RESCHEDULE" and r.user_decision in ADOPTED_DECISION_VALUES
    }
    park_goal_ids = {
        r.original_goal_id
        for rows in by_execution.values()
        for r in rows
        if r.option_group == "PARK"
        and r.user_decision in ADOPTED_DECISION_VALUES
        and r.original_goal_id is not None
    }
    park_no_goal_n = sum(
        1
        for rows in by_execution.values()
        for r in rows
        if r.option_group == "PARK"
        and r.user_decision in ADOPTED_DECISION_VALUES
        and r.original_goal_id is None
    )

    goal_siblings = await _fetch_goal_sibling_action_ids(session, park_goal_ids)
    success_lookup_ids = reschedule_action_ids | {
        aid for siblings in goal_siblings.values() for aid in siblings
    }
    success_by_action = await _fetch_success_plan_starts(session, success_lookup_ids)
    park_success_by_goal: dict[UUID, list[datetime]] = defaultdict(list)
    for goal_id, sibling_ids in goal_siblings.items():
        for aid in sibling_ids:
            park_success_by_goal[goal_id].extend(success_by_action.get(aid, []))

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
        ft = _is_followthrough(
            rows,
            derived_done_ids=derived_done_ids,
            reschedule_success=success_by_action,
            park_success_by_goal=park_success_by_goal,
        )
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
    _group_note = {
        "DOWNSCOPE": "  (파생 카드 완주 기준)",
        "CARRY_OVER": "  (파생 카드 완주 기준)",
        "RESCHEDULE": "  (파생 카드 없음 — 원본 카드가 결정 이후 다시 성공했는가로 판정)",
        "PARK": "  (파생 카드 없음 — 결정 후 7일 내 같은 goal 계보 완주로 판정)",
    }
    for g in sorted(by_group_total, key=lambda g: -by_group_total[g]):
        n = by_group_total[g]
        f = by_group_followthrough[g]
        rate = f / n if n else 0.0
        note = _group_note.get(g, "")
        print(f"  {g:12s} 수락 {n:3d}건 → 완주 {f:3d}건 ({rate:.1%}){note}")
    if park_no_goal_n:
        print(
            f"  ⚠️ PARK 수락 중 {park_no_goal_n}건은 원본 카드에 goal 계보가 없어 "
            "완주 판정 불가(습관/인박스/수동 출처) — 보수적으로 미완주 처리됨"
        )
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
