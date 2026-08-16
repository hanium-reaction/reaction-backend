"""잠정(proposed) 목표 만료 cron job — 승격되지 않은 목표를 보관 처리 (#178).

`proposed` 는 계획을 승인하지 않은 잠정 목표 상태다(#176). 도입 당시 정리를 **다음
인터뷰의 supersede** 라는 사건에만 달아둬서, 인터뷰를 한 번 하고 돌아오지 않은 사용자에겐
탈출구가 없었다 — 목표 화면에 '계획 전' 배지로 영원히 남고, 눌러도 분해 트리가 없어
아무 일도 일어나지 않는다. 게다가 인터뷰의 heaviest 목표는 `goal_tier="focus"` 로 쓰이는데
목록은 tier 로 묶고 카운트는 proposed 를 빼므로, "Focus 최대 3" 규칙이 화면에서 깨져
보이는데 사용자가 되돌릴 방법이 없다.

이 레포의 다른 과도 상태는 전부 시간 탈출구를 갖는다 — `plan_drafts` 72h
(`expire_drafts.py`), 미회고 카드 3일(`expire_reflections.py`). 같은 패턴을 따른다.

**사용자 알림은 없다.** ADR-0005 §7.8 "만료 자체는 자동, 사용자 알림은 X (베이스라인
§4.1.6 감정 거리 존중)" 선례를 그대로 따른다. 알림을 붙이려면 AGENTS §1 이 잠근 알림
3클래스(morning_brief / pre_card / evening_reflection)·주 ≤3건에 4번째를 더해야 하므로
§8 팀 합의 사항이 된다.

⚠️ 보관된 행은 사용자가 지운 목표(`GoalRepo.soft_delete`)·supersede 로 보관된 목표와
**구별되지 않는다** — 셋 다 `status='archived'` + `archived_at` 만 쓴다. 구분 표식을
두려면 컬럼 추가 = 마이그레이션 = §8 이라, supersede 가 이미 안고 있는 제약을 그대로
받아들이고 대신 건수를 INFO 로 남겨 사고 시 원복 범위를 산정할 수 있게 한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from reaction_backend.repositories.goal_repo import GoalRepo
from reaction_backend.schemas.common import now_kst

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

# 잠정 목표 TTL — 계획 초안 만료(72h)보다 넉넉히 길게 (#178 본문 제안값).
PROPOSED_GOAL_TTL_DAYS = 14


def proposed_goal_stale_before(now: datetime) -> datetime:
    """만료 경계 — **단일 소스**.

    프리뷰 스크립트(`scripts/preview_expire_proposed_goals.py`)가 이 함수를 그대로
    import 한다. 양쪽이 각자 계산하면 한쪽만 바뀌었을 때 프리뷰가 보여준 것과 실제
    적용 대상이 어긋난다 — `pending_reflection_since` 와 같은 이유다.
    """
    return now - timedelta(days=PROPOSED_GOAL_TTL_DAYS)


async def run_expire_stale_proposed_goals(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    repo: GoalRepo | None = None,
) -> int:
    """TTL 을 넘긴 잠정 목표를 일괄 보관하고 commit. 반환: 보관된 목표 수.

    `repo` 는 테스트 주입용(기본은 세션 기반 `GoalRepo`). `now` 미지정 시 `now_kst()`.
    **idempotent** — 이미 보관/활성/완료인 목표는 건드리지 않는다 (AGENTS §2).
    """
    repo = repo or GoalRepo(session)
    now_dt = now or now_kst()
    archived = await repo.expire_stale_proposed(
        before=proposed_goal_stale_before(now_dt),
        archived_at=now_dt,
    )
    await session.commit()
    # 사용자 목표를 보관하는 job — 사고 시 원복 범위 산정을 위해 건수를 남긴다.
    if archived:
        _log.info("expire_proposed_goals: %d goals archived", archived)
    return archived
