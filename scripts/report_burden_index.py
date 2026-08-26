"""부담 지표 (`burden_index`) — 읽기 전용 실측 (근거 대장 §7.2, E6 — Cheng et al. 2025).

**E6**: 아첨(sycophancy)을 억제하면(=이 앱처럼 완주를 요구하는 행동 지표를 밀면) 만족도·
재사용 의향이 떨어진다. 행동 지표만 보면 우리가 만든 해악을 못 본다 — 그래서 §7.2 는
`burden_index`(카드 거절률 + 회고 미응답률 + 알림 해제) 를 **해악 감시** 축으로 같이
추적하라고 명시한다(실험 계획서 §5 M11).

**⚠️ 3성분 중 2성분만 계측 가능 — 정직하게 부분치만 낸다.**
"알림 해제" 성분은 계측 인프라가 없다: `notification_settings` 는 **현재 상태의 스냅샷**
뿐이고 변경 이력 테이블이 없다. 그리고 남은 두 컬럼 다 "해제"의 신호로 쓰기엔 오염돼
있다 — `preCardEnabled` 는 **기본값이 false(옵트인)**이라 false 가 "껐다"인지 "애초에 켠
적이 없다"인지 구분이 안 된다. `push_subscription IS NULL` 도 사용자가 직접 해제한 것과
브라우저 재설치 등으로 구독이 죽어 `push_gate.send_push` 가 자동 정리한 것(`safety/
push_gate.py` "gone" 처리)을 구분할 수 없다. 셋 다 "이벤트"가 아니라 "현재 상태"라 사후
집계로는 원리적으로 못 가른다 — 실험 계획서 §5 의 "계산 불가 → 미측정으로 표기" 관행을
그대로 따라 이 성분은 **뺀다**(억지로 값을 만들어 3성분 평균인 척하지 않는다).

**카드 거절률**: 최근 7일 내 `recovery_decided_at` 인 결정 중 `rejected`/`skipped` 비율.

**회고 미응답률**: 최근 7일 내 회고 가능해진(`reflectable_from()`, 회고 창의 단일
기준식 — `execution_repo.py`) 실행 중, 그 카드가 `system_failure_reason=
'reflection_skipped'`(3일 누적 무응답 자동 만료, `expire_reflections.py`)로 끝난 비율.
현재 `completion_status` 로 필터링하지 않는다 — 체크인으로 상태가 바뀐 카드까지
포함해야 "그 시점에 회고 대상이었던 전체"가 되고, 아니면 성공적으로 응답한 카드가
분모에서 빠져 무응답률이 구조적으로 과대평가된다.

**아무것도 쓰지 않는다** — SELECT 뿐. `--apply` 같은 옵션 자체가 없다.

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.report_burden_index
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.repositories.execution_repo import reflectable_from
from reaction_backend.schemas.common import now_kst, to_kst

if TYPE_CHECKING:
    from datetime import datetime

_WINDOW = timedelta(days=7)
_REJECTED_DECISIONS = ("rejected", "skipped")
_REFLECTION_SKIPPED = "reflection_skipped"


async def _fetch_recent_decisions(session: AsyncSession, *, since: datetime) -> list[str]:
    """최근 창 안에서 결정된 recovery_attempts 의 user_decision 목록 (pending 제외)."""
    stmt = select(RecoveryAttempt.user_decision).where(
        RecoveryAttempt.recovery_decided_at.is_not(None),
        RecoveryAttempt.recovery_decided_at >= since,
    )
    return list((await session.execute(stmt)).scalars().all())


async def _fetch_recent_reflectable(session: AsyncSession, *, since: datetime) -> list[str | None]:
    """최근 창 안에 회고 가능해진 실행들의 action_item.system_failure_reason 목록.

    reflectable_from() >= since 로 "그 시점에 회고 대상권에 들어온" 실행 전체를 잡는다 —
    이후 체크인으로 completion_status 가 바뀌었든, 무응답으로 만료됐든 전부 포함.
    """
    stmt = (
        select(ActionItem.system_failure_reason)
        .join(ExecutionEvent, ExecutionEvent.action_item_id == ActionItem.id)
        .where(reflectable_from() >= since)
    )
    return list((await session.execute(stmt)).scalars().all())


def _rejection_rate(decisions: list[str]) -> tuple[int, int, float]:
    total = len(decisions)
    rejected = sum(1 for d in decisions if d in _REJECTED_DECISIONS)
    return rejected, total, (rejected / total if total else 0.0)


def _reflection_non_response_rate(reasons: list[str | None]) -> tuple[int, int, float]:
    total = len(reasons)
    skipped = sum(1 for r in reasons if r == _REFLECTION_SKIPPED)
    return skipped, total, (skipped / total if total else 0.0)


async def _preview(session: AsyncSession) -> None:
    now = now_kst()
    since = now - _WINDOW
    print(f"기준 시각: {now.isoformat()} (최근 7일 = {since.date()} ~ {now.date()})")
    print()

    decisions = await _fetch_recent_decisions(session, since=since)
    reasons = await _fetch_recent_reflectable(session, since=since)

    if not decisions and not reasons:
        print("최근 7일 내 결정·회고 대상 실행이 0건 — 잴 데이터가 없다.")
        return

    if decisions:
        rejected_n, decisions_total, rejection_rate = _rejection_rate(decisions)
        print(f"■ 카드 거절률 분모(최근 7일 결정) = {decisions_total}건")
        print(f"■ 카드 거절률 = {rejected_n:4d} / {decisions_total} = {rejection_rate:.1%}")
    else:
        print("■ 카드 거절률: 최근 7일 결정 0건 — 잴 데이터가 없다.")
    print()

    if reasons:
        skipped_n, reflectable_total, non_response_rate = _reflection_non_response_rate(reasons)
        print(f"■ 회고 미응답률 분모(최근 7일 회고 가능 실행) = {reflectable_total}건")
        print(f"■ 회고 미응답률 = {skipped_n:4d} / {reflectable_total} = {non_response_rate:.1%}")
    else:
        print("■ 회고 미응답률: 최근 7일 회고 가능 실행 0건 — 잴 데이터가 없다.")
    print()

    print(
        "⚠️ 알림 해제 성분은 계측 불가(모듈 docstring 참고) — 위 두 성분은 burden_index 의 "
        "**부분치**일 뿐, 3성분 합성 지표 자체는 아직 계산할 수 없다."
    )
    print()
    print(
        "※ 이 스크립트는 아무것도 쓰지 않았다. 지표 정의는 "
        "docs/experiments/experiment-plan-v1.md §5 M11 을 따른다."
    )


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[report-burden-index] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
