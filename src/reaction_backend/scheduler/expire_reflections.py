"""회고 누적 창 만료 cron job — 3일 초과 미회고 카드 자동 만료 (Issue #20).

DevBaseline §1.4 잠금 결정(AGENTS.md §1): "누적 정책: 미회고 카드 최대 3일, 그 이후
`system_failure_reason='reflection_skipped'` 자동 만료."

만료 대상 = `GET /reflection/pending` 창의 **정확한 여집합**. 양쪽 다 `execution_repo` 의
`_reflectable_from()`(= 계획 시각과 실제 착수 시각 중 **나중**)을 기준으로, pending 은
`>= pending_reflection_since(오늘)` 을 보여주고 이 cron 은 그 반대편(`<`)을 만료시킨다 —
즉 **사용자가 아직 회고할 수 있는 카드는 절대 건드리지 않는다**. 카드는 만료 전에 저녁 회고
기회를 정확히 3회 갖는다 (X일 카드 → X·X+1·X+2 의 21:00 노출 → X+3 04:00 만료). 이슈 원문의
"4일 이상 된"(1-indexed 일차)과 잠금 결정의 "최대 3일"(회고 기회 횟수)은 같은 경계의 다른
셈법이다.

두 쪽이 **같은 기준식**을 써야 하는 이유(리뷰 지적): 각자 다른 컬럼을 보면 여집합이 깨져
어느 집합에도 안 드는 카드가 생긴다. 지난 블록을 뒤늦게 [▶시작] 하면 `plan_start_at` 은
이미 창 밖이라 회고 화면엔 **한 번도 안 뜨는데** 만료는 착수 시각 기준으로 일어나, 회고
기회를 0회 받고 카드가 조용히 사라졌다.

⚠️ `execution_events.completion_status` 는 **건드리지 않는다**(in_progress 유지).
`review_repo.collect_execution_stats` 에 archived 필터가 없어, 만료 카드의 실행을 주간 KPI
에서 격리하는 유일한 장치가 `weekly_review._TERMINAL_STATUSES` 의 in_progress 제외이기
때문이다. 'failed' 로 종결하면 사용자가 말한 적 없는 실패를 날조해 adherence·resilience 를
동시에 오염시킨다 (AGENTS.md §2 Resilience 지표 전제 + §1 톤 잠금 위반).

원본 `action_item.status` 도 건드리지 않는다 (AGENTS.md §2). 만료는 `archived_at` +
`system_failure_reason` 으로만 표현한다.

만료 시 사용자 알림은 없다 — ADR-0005 §7.8 "만료 자체는 자동, 사용자 알림은 X
(베이스라인 §4.1.6 감정 거리 존중). 다음 진입 시 부드럽게 안내."
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from reaction_backend.repositories.execution_repo import ExecutionRepo
from reaction_backend.schemas.common import KST, now_kst

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

# 회고 누적 창 — 오늘+어제+그제 (DevBaseline §1.4 잠금).
PENDING_WINDOW_DAYS = 3


def pending_reflection_since(today: date) -> datetime:
    """회고 누적 창의 시작 경계 (KST 자정).

    **단일 소스** — `GET /reflection/pending` 은 회고 가능 시각이 `>= ` 이 값인 실행을
    노출하고, 만료 cron 은 `< ` 인 실행의 카드를 만료시킨다(정확한 여집합). 두 쪽이 각자
    계산하면 한쪽만 바뀌었을 때 회고 가능한 카드를 지우거나(데이터 손실) 영영 안 지워진다.

    ⚠️ 경계와 짝을 이루는 **기준식**은 `ExecutionRepo._reflectable_from()`
    (= `greatest(plan_start_at, actual_start_at)`) 이다. 이 함수는 경계'값'만 정하고,
    무엇을 그 값과 비교하는지는 저쪽이 정한다 — 양쪽 다 같은 식을 써야 여집합이 성립한다.

    라우터가 cron 모듈의 창 경계 함수를 재사용하는 것은 `week_start_of` 와 같은 패턴
    (`scheduler/weekly_review_precompute.py` ← `api/routes/review.py`).
    """
    return datetime.combine(today - timedelta(days=PENDING_WINDOW_DAYS - 1), time.min, tzinfo=KST)


async def run_expire_unreflected_cards(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    repo: ExecutionRepo | None = None,
) -> int:
    """회고 창을 벗어난 미체크 카드를 일괄 만료하고 commit. 반환: 만료된 카드 수.

    `repo` 는 테스트 주입용(기본은 세션 기반 `ExecutionRepo`). `now` 미지정 시 `now_kst()`.
    **idempotent** — 이미 보관된 카드는 건드리지 않으므로 다회 실행해도 안전(AGENTS §2).
    """
    repo = repo or ExecutionRepo(session)
    now_dt = now or now_kst()
    expired = await repo.expire_unreflected(
        before=pending_reflection_since(now_dt.date()),
        archived_at=now_dt,
    )
    await session.commit()
    # 사용자 카드를 보관하는 job — 사고 시 원복 범위 산정을 위해 건수를 남긴다.
    # 원복 셀렉터: system_failure_reason='reflection_skipped' (hard delete 아님).
    if expired:
        _log.info("expire_unreflected: %d cards expired (reflection_skipped)", expired)
    return expired
