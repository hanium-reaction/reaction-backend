"""잠정 목표 만료 cron 사전 실측 — SCHEDULER_ENABLED 켜기 전에 '무엇이 보관될지' 읽기 전용으로 센다.

배경(#178 · #20 DoD 2 선례): `expire_proposed_goals` cron(매일 04:00 KST)은 구현·테스트됐지만
`SCHEDULER_ENABLED` 가 기본 OFF 라 아직 라이브에서 돈 적이 없다. 켜는 순간 **첫 04:00 에
TTL(14일)을 넘긴 잠정(proposed) 목표가 일괄 보관(archived)** 되므로, 몇 건이 대상인지 먼저
실측하고 켜는 것이 안전하다(라이브 RDS 는 EC2 에서만 접근 가능 → workflow_dispatch).

**아무것도 쓰지 않는다** — SELECT 뿐. --apply 같은 옵션 자체가 없다.

정확성: 후보 판정은 `GoalRepo.expire_stale_proposed` 의 WHERE 와 **글자 단위로 같아야**
한다. 경계(`proposed_goal_stale_before`)는 그쪽 모듈에서 직접 import 하고, 나머지 조건은
미러다 — 두 쿼리의 WHERE 가 동일함을 `tests/test_preview_expire_proposed_goals.py` 가
compile 된 SQL 로 고정한다(어긋나면 CI 가 깨져서, 만료 쿼리를 고치고 프리뷰를 잊는 사고를
막는다).

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.preview_expire_proposed_goals
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.scheduler.expire_proposed_goals import proposed_goal_stale_before
from reaction_backend.schemas.common import now_kst, to_kst


def expire_candidates_stmt(before: datetime) -> Select[Any]:
    """`GoalRepo.expire_stale_proposed` 의 UPDATE 와 **같은 WHERE** 를 가진 SELECT.

    조건 순서까지 저쪽과 동일하게 유지할 것 — 동기화 테스트가 WHERE 문자열을 대조한다.
    """
    return select(Goal).where(
        Goal.status == "proposed",
        Goal.archived_at.is_(None),
        Goal.created_at < before,
    )


async def _preview(session: AsyncSession) -> None:
    now = now_kst()
    before = proposed_goal_stale_before(now)
    print(f"기준 시각: {now.isoformat()}  ·  잠정 목표 TTL 경계: {before.isoformat()}")
    print("(cron 은 이 경계보다 먼저 만들어진 proposed 목표를 보관한다 — 그 이후는 절대 안 건드림)")
    print()

    goals = list((await session.execute(expire_candidates_stmt(before))).scalars().all())
    if not goals:
        print("만료 대상 0건 — 지금 켜면 첫 04:00 에 아무것도 보관되지 않는다.")
        return

    emails = dict(
        (
            await session.execute(
                select(User.id, User.email).where(User.id.in_({g.user_id for g in goals}))
            )
        ).all()
    )
    per_user = Counter(emails.get(g.user_id, str(g.user_id)[:8]) for g in goals)
    per_tier = Counter(g.goal_tier for g in goals)

    print(f"보관 대상 잠정 목표: {len(goals)}건")
    print()
    print("사용자별:")
    for email, n in per_user.most_common():
        print(f"  {email:40s} {n:3d}건")
    print("tier 별:")
    for tier, n in sorted(per_tier.items()):
        print(f"  {tier:10s} {n:3d}건")
    print()
    print("목표 목록 (제목 30자):")
    for g in sorted(goals, key=lambda g: g.created_at):
        print(f"  {g.created_at.date()}  {g.title[:30]:32s} {emails.get(g.user_id, '?')[:24]}")
    print()
    print(
        "⚠️ 원복 셀렉터 없음 — cron 보관(status='archived'+archived_at)은 사용자 삭제·supersede "
        "보관과 구별되지 않는다. 되돌리려면 위 목표 id 목록을 이 실행 출력에서 직접 보관해 둘 것."
    )


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[preview-expire-proposed-goals] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
