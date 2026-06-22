"""Plan Draft 만료 cron job — First Plan HITL Draft 72h 만료 (#62, ADR-0005 §7.8).

72h 미응답(미수락) `plan_drafts` 를 status='expired' 로 일괄 전이한다. **idempotent** —
이미 approved/expired 인 행은 건드리지 않으므로 다회 실행해도 안전(AGENTS §2).

⚠️ 본 모듈은 **job 함수**다. 실제 스케줄 트리거(매 6시간 등록)는 Issue #24 운영준비에서
APScheduler/Arq 로 `run_expire_stale_drafts` 를 등록한다 (scheduler/README.md 시간표).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from reaction_backend.repositories.plan_draft_repo import PlanDraftRepo
from reaction_backend.schemas.common import now_kst

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def run_expire_stale_drafts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    repo: PlanDraftRepo | None = None,
) -> int:
    """만료된 Draft 를 일괄 expired 전이하고 commit. 반환: 전이된 행 수.

    `repo` 는 테스트 주입용(기본은 세션 기반 `PlanDraftRepo`). `now` 미지정 시 `now_kst()`.
    """
    repo = repo or PlanDraftRepo(session)
    expired = await repo.expire_stale(now=now or now_kst())
    await session.commit()
    return expired
