"""Read-only descriptive P0 metrics. Run against an explicitly selected database."""

import asyncio
import json

from sqlalchemy import select

from reaction_backend.db.models.reentry_attempt import ReentryAttempt
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.domain.reentry_metrics import Observation, summarize
from reaction_backend.schemas.common import now_kst


async def main() -> None:
    async with get_sessionmaker()() as session:
        records = (await session.execute(select(ReentryAttempt))).scalars().all()
        rows: list[Observation] = [
            {
                "user_id": str(r.user_id),
                "attempt_id": str(r.id),
                "entry_kind": r.entry_kind,
                "created_at": r.created_at,
                "viewed_at": r.viewed_at,
                "started_at": r.started_at,
                "step_completed_at": r.step_completed_at,
            }
            for r in records
        ]
        print(json.dumps(summarize(rows, as_of=now_kst())))


if __name__ == "__main__":
    asyncio.run(main())
