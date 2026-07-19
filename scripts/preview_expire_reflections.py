"""만료 cron 사전 실측 — SCHEDULER_ENABLED 켜기 전에 '무엇이 지워질지' 읽기 전용으로 센다.

배경(#20 DoD 2 · #24): `expire_reflections` cron(매일 04:00 KST)은 구현·테스트됐지만
`SCHEDULER_ENABLED` 가 기본 OFF 라 아직 라이브에서 돈 적이 없다. 켜는 순간 **첫 04:00 에
회고 창(3일)을 벗어난 미체크 카드가 일괄 soft delete** 되므로, 몇 건이 대상인지 먼저
실측하고 켜는 것이 안전하다(라이브 RDS 는 EC2 에서만 접근 가능 → workflow_dispatch).

**아무것도 쓰지 않는다** — SELECT 뿐. --apply 같은 옵션 자체가 없다.

정확성: 후보 판정은 `ExecutionRepo.expire_unreflected` 의 WHERE 와 **글자 단위로 같아야**
한다. 창 기준식(`_reflectable_from`)과 경계(`pending_reflection_since`)는 그쪽 모듈에서
직접 import 하고, 나머지 조건은 미러다 — 두 쿼리의 WHERE 가 동일함을
`tests/test_preview_expire_reflections.py` 가 compile 된 SQL 로 고정한다(어긋나면 CI 가
깨져서, 만료 쿼리를 고치고 프리뷰를 잊는 사고를 막는다).

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.preview_expire_reflections
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models import ActionItem, ExecutionEvent, ScheduledBlock, User
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.repositories.execution_repo import _reflectable_from
from reaction_backend.scheduler.expire_reflections import pending_reflection_since
from reaction_backend.schemas.common import now_kst, to_kst


def expire_candidates_stmt(before: datetime) -> Select[Any]:
    """`ExecutionRepo.expire_unreflected` 의 UPDATE 와 **같은 WHERE** 를 가진 SELECT.

    조건 순서까지 저쪽과 동일하게 유지할 것 — 동기화 테스트가 WHERE 문자열을 대조한다.
    """
    unreflected = select(ExecutionEvent.action_item_id).where(
        ExecutionEvent.completion_status == "in_progress",
        _reflectable_from() < before,
    )
    has_live_block = (
        select(ScheduledBlock.id)
        .where(
            ScheduledBlock.action_item_id == ActionItem.id,
            ScheduledBlock.block_status.in_(("scheduled", "started")),
            ScheduledBlock.start_at >= before,
        )
        .exists()
    )
    return select(ActionItem).where(
        ActionItem.archived_at.is_(None),
        ActionItem.system_failure_reason.is_(None),
        ActionItem.id.in_(unreflected),
        ~has_live_block,
    )


async def _preview(session: AsyncSession) -> None:
    now = now_kst()
    before = pending_reflection_since(now.date())
    print(f"기준 시각: {now.isoformat()}  ·  회고 창 경계(since): {before.isoformat()}")
    print("(cron 은 이 경계보다 앞의 미체크 실행 카드를 만료한다 — 창 안은 절대 건드리지 않음)")
    print()

    cards = list((await session.execute(expire_candidates_stmt(before))).scalars().all())
    if not cards:
        print("만료 대상 0건 — 지금 켜면 첫 04:00 에 아무것도 지워지지 않는다.")
        return

    card_ids = [c.id for c in cards]
    emails = dict(
        (
            await session.execute(
                select(User.id, User.email).where(User.id.in_({c.user_id for c in cards}))
            )
        ).all()
    )
    open_blocks = list(
        (
            await session.execute(
                select(ScheduledBlock).where(
                    ScheduledBlock.action_item_id.in_(card_ids),
                    ScheduledBlock.block_status.in_(("scheduled", "started")),
                )
            )
        )
        .scalars()
        .all()
    )

    per_user = Counter(emails.get(c.user_id, str(c.user_id)[:8]) for c in cards)
    per_date = Counter(str(c.target_date) for c in cards)

    print(f"만료 대상 카드: {len(cards)}건  ·  함께 취소될 미종결 블록: {len(open_blocks)}건")
    print()
    print("사용자별:")
    for email, n in per_user.most_common():
        print(f"  {email:40s} {n:3d}건")
    print("날짜별(target_date):")
    for d, n in sorted(per_date.items()):
        print(f"  {d}  {n:3d}건")
    print()
    print("카드 목록 (제목 20자):")
    for c in sorted(cards, key=lambda c: (str(c.target_date), c.title)):
        print(f"  {c.target_date}  {c.title[:20]:22s} {emails.get(c.user_id, '?')[:24]}")
    print()
    print("원복 셀렉터(만약 켠 뒤 되돌리려면): system_failure_reason='reflection_skipped'")


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[preview-expire-reflections] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
