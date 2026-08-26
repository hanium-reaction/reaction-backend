"""User repository — DB upsert / 조회 (Issue #16).

규칙:
- `email` 이 1차 식별 키 (Google OAuth). 신규는 `onboarding_state=WELCOME` (DB server_default).
- 기존 user 는 `name` · `last_active_at` 만 갱신, `onboarding_state` · `tone_mode` 는 보존.
- hard delete 금지 (AGENTS.md §2). 본 repo 는 delete 미제공.
- commit 은 호출자 책임 — 라우터에서 `await session.commit()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db


@dataclass(slots=True)
class GoogleProfile:
    """upsert 입력 — Google id_token 검증 결과에서 추출."""

    email: str
    name: str


class UserRepo:
    """User 영속화. FastAPI Depends 로 주입 (`get_user_repo`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        stmt = select(User).where(User.id == user_id, User.archived_at.is_(None))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email, User.archived_at.is_(None))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active(self) -> list[User]:
        """모든 활성 사용자 (cron sweep 용, #24) — onboarding ACTIVE + 익명화/삭제 안 됨."""
        stmt = select(User).where(
            User.archived_at.is_(None),
            User.is_anonymized.is_(False),
            User.onboarding_state == "ACTIVE",
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_inactive_for_anonymization(self, *, before: datetime) -> list[User]:
        """`last_active_at` 이 `before` 이전인 **아직 익명화 안 된** 사용자 (#24 90일 cron).

        `list_active()` 와 정반대 대상이라 필터를 공유하지 않는다 — 그쪽은 "지금 쓰는
        사람"(ACTIVE + 익명화 안 됨)을 찾고, 이건 "떠난 사람"을 찾는다. `onboarding_state`
        는 **안 본다**: 온보딩 중에 이탈한 계정이야말로 90일 뒤에 남아 있으면 안 되는
        데이터다.

        `anonymized_at IS NULL` 이 멱등 가드다(#24 본문). soft-delete(`archived_at`) 된
        계정은 삭제 경로(`POST /settings/delete-account`, #321)가 이미 `anonymized_at` 을
        세우므로 자연히 빠진다 — 옛 경로로 지워져 플래그가 없는 행이 있어도 마스킹은
        멱등이라 해가 없다.
        """
        stmt = select(User).where(
            User.last_active_at < before,
            User.anonymized_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_signed_up(self) -> int:
        """가입 인원 상한(#324) 판정용 — onboarding 진행도와 무관하게 **가입한 전체**를 센다.

        `list_active()`(ACTIVE 만)와 달리 온보딩 중인 사용자도 "이미 자리를 차지한
        가입자"로 센다 — 자리 30개는 온보딩 완료 여부가 아니라 계정 존재 여부로
        소진된다. soft-delete(`archived_at`) 된 사용자는 빼서 나간 자리를 되돌려준다.
        """
        stmt = select(func.count()).select_from(User).where(User.archived_at.is_(None))
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def upsert_from_google(self, profile: GoogleProfile) -> User:
        """email 기준 upsert.

        - 신규: WELCOME 상태로 생성 (`onboarding_state` 는 DB server_default).
        - 기존: `name` · `last_active_at` 만 갱신, `onboarding_state` 등 보존.
        """
        existing = await self.get_by_email(profile.email)
        now = datetime.now(UTC)
        if existing is not None:
            existing.name = profile.name
            existing.last_active_at = now
            await self._session.flush()
            return existing
        user = User(
            email=profile.email,
            name=profile.name,
            last_active_at=now,
        )
        self._session.add(user)
        await self._session.flush()
        await self._session.refresh(user)
        return user

    async def set_tone_mode(self, user: User, tone_mode: str) -> User:
        """톤 모드 변경 (S23 설정, Issue #23).

        사용자 명시 설정 변경 — onboarding 상태 전이는 없다. commit 은 호출자 책임.
        """
        user.tone_mode = tone_mode
        await self._session.flush()
        return user

    async def advance_onboarding(
        self,
        user: User,
        expected_from: str | tuple[str, ...],
        to: str,
    ) -> bool:
        """안전한 onboarding 상태 전이 (Issue #17).

        현재 상태가 `expected_from` 집합에 있을 때만 `to` 로 전이한다.
        이미 더 진행된 상태(예: ACTIVE)면 no-op — 같은 endpoint 두 번 호출해도 멱등.

        Returns:
            전이가 일어났는지 (true=advanced, false=no-op).
        """
        expected = (expected_from,) if isinstance(expected_from, str) else expected_from
        if user.onboarding_state in expected:
            user.onboarding_state = to
            await self._session.flush()
            return True
        return False


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_user_repo(session: SessionDep) -> UserRepo:
    return UserRepo(session)
