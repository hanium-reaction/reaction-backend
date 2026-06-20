"""Demo smoke check — 시드된 데모 계정이 중간발표 시나리오를 만족하는지 검증.

데모 시나리오 v1.0 의 전제("어제 실패한 GROUP BY -> 오늘 재도전 -> 복구") 데이터가
DB 에 실제로 존재하는지 읽기 전용으로 점검한다. 리허설/배포 전 1회 돌려 데이터 누락을
조기에 잡는 용도.

실행 (db_seed_demo 이후):
  uv run python -m scripts.demo_smoke

하나라도 실패하면 exit 1.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models import (
    ActionItem,
    DailyBrief,
    ExecutionEvent,
    ExecutionFailureTag,
    Goal,
    User,
)
from reaction_backend.db.session import get_sessionmaker

DEMO_EMAIL = "demo@reaction.local"
KST = timezone(timedelta(hours=9))
FAILURE_TAG_CODE = "AMBIGUITY"


class _Checker:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        mark = "OK  " if ok else "FAIL"
        if not ok:
            self.failed += 1
        print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))


async def _run(session: AsyncSession) -> int:
    c = _Checker()
    today = datetime.now(KST).date()
    yesterday = today - timedelta(days=1)

    user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
    c.check("demo user 존재", user is not None, DEMO_EMAIL)
    if user is None:
        print(
            "\n시드를 먼저 실행하세요: uv run python -m scripts.db_seed_demo",
            file=sys.stderr,
        )
        return 1

    c.check("onboarding ACTIVE", user.onboarding_state == "ACTIVE", str(user.onboarding_state))

    goals = (await session.scalars(select(Goal).where(Goal.user_id == user.id))).all()
    c.check("focus 목표 1개 이상", any(g.goal_tier == "focus" for g in goals), f"{len(goals)}개")

    actions = (await session.scalars(select(ActionItem).where(ActionItem.user_id == user.id))).all()
    yday_failed = [a for a in actions if a.target_date == yesterday and a.status == "failed"]
    today_planned = [a for a in actions if a.target_date == today and a.status == "planned"]
    c.check("어제 실패 액션 1개 이상", len(yday_failed) >= 1, f"{len(yday_failed)}개")
    c.check("오늘 계획 액션 3개 이상", len(today_planned) >= 3, f"{len(today_planned)}개")

    if yday_failed:
        action_ids = [a.id for a in yday_failed]
        execs = (
            await session.scalars(
                select(ExecutionEvent).where(ExecutionEvent.action_item_id.in_(action_ids))
            )
        ).all()
        failed_execs = [e for e in execs if e.completion_status == "failed"]
        c.check("어제 실패 ExecutionEvent 존재", len(failed_execs) >= 1, f"{len(failed_execs)}개")

        if failed_execs:
            exec_ids = [e.id for e in failed_execs]
            tags = (
                await session.scalars(
                    select(ExecutionFailureTag).where(
                        ExecutionFailureTag.execution_id.in_(exec_ids)
                    )
                )
            ).all()
            tag_codes = [t.tag_code for t in tags]
            c.check(
                f"실패 사유 태그 '{FAILURE_TAG_CODE}'",
                FAILURE_TAG_CODE in tag_codes,
                str(tag_codes),
            )

    brief = await session.scalar(
        select(DailyBrief).where(DailyBrief.user_id == user.id, DailyBrief.brief_date == today)
    )
    c.check("오늘 Morning Brief 존재", brief is not None)
    if brief is not None:
        big_rock_ok = brief.big_rock_action_item_id in {a.id for a in today_planned}
        c.check("brief big_rock = 오늘 계획 카드", big_rock_ok)

    print()
    if c.failed:
        print(f"[SMOKE] {c.failed}개 항목 실패 - 데모 데이터 점검 필요.", file=sys.stderr)
        return 1
    print("[SMOKE] 전 항목 통과 - 데모 시나리오 데이터 준비 완료.")
    return 0


async def smoke() -> int:
    factory = get_sessionmaker()
    async with factory() as session:
        return await _run(session)


def main() -> int:
    return asyncio.run(smoke())


if __name__ == "__main__":
    sys.exit(main())
