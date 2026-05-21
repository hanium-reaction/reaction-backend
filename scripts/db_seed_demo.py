"""Demo data seed — local/dev 만.

마스터 seed (failure tags / recovery strategies) 는 alembic 마이그레이션이 처리.
이 스크립트는 demo user 1명 + 최소 도메인 데이터 (S03 Confirm 직후 상태 모방).

실행:
  uv run python -m scripts.db_seed_demo

idempotent: 같은 email 의 demo user 가 이미 있으면 skip.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from reaction_backend.config import get_settings
from reaction_backend.db.models import (
    BehavioralProfile,
    InteractionStyle,
    NotificationSetting,
    User,
)
from reaction_backend.db.session import get_sessionmaker

DEMO_EMAIL = "demo@reaction.local"


async def seed() -> int:
    settings = get_settings()
    if settings.app_env == "prod":
        print("REFUSED: app_env=prod 에서는 demo seed 금지.", file=sys.stderr)
        return 2

    factory = get_sessionmaker()
    async with factory() as session:
        existing = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if existing is not None:
            print(f"demo user 이미 존재 (id={existing.id}). skip.")
            return 0

        user = User(
            email=DEMO_EMAIL,
            onboarding_state="ACTIVE",
            tone_mode="gentle",
            timezone="Asia/Seoul",
            is_beta=True,
        )
        session.add(user)
        await session.flush()  # user.id 확정

        session.add(
            BehavioralProfile(
                user_id=user.id,
                attention_span=30,
                energy_cycle="morning",
                time_chunk_preference=30,
                success_buffer=0.2,
            )
        )
        session.add(
            InteractionStyle(
                user_id=user.id,
                suggestion_style="neutral",
                recovery_tone="gentle",
                explanation_depth="medium",
                reminder_frequency="medium",
            )
        )
        session.add(NotificationSetting(user_id=user.id))

        await session.commit()

        print(f"[OK] demo user 생성: {DEMO_EMAIL} (id={user.id})")
        return 0


def main() -> int:
    return asyncio.run(seed())


if __name__ == "__main__":
    sys.exit(main())
