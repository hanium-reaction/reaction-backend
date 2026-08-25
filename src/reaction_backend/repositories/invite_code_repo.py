"""InviteCode repository — 가입 게이트 코드 검증/소비 + 운영 발급 (#324).

규칙:
- `code` 는 대문자로 정규화해 저장·조회한다(모델 docstring 참고).
- `get_by_code`(조회)와 `mark_used`(소비)를 분리한 이유 — 호출자(`routes/auth.py`)가
  코드 유효성을 **user INSERT 보다 먼저** 확인하고 싶어서다: 코드가 무효/이미 소진이면
  user 를 아예 만들지 않고 바로 422/409 를 던진다(플러시했다 롤백하는 대신). 소비는
  user 가 생긴 **뒤** `used_by_user_id` 를 채워야 하므로 자연히 두 단계가 된다.
- 두 메서드 모두 호출자가 이미 `_signup_lock`(전역 advisory lock, `routes/auth.py`)으로
  동시 가입을 직렬화해 둔 트랜잭션 안에서만 부른다는 전제 — 그 보장이 없으면 같은 코드가
  두 트랜잭션에서 동시에 "미사용"으로 읽혀 두 번 소비될 수 있다.
- commit 은 호출자 책임(라우터/스크립트).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.invite_code import InviteCode
from reaction_backend.db.session import get_db


def normalize_code(raw: str) -> str:
    """대소문자·앞뒤 공백 차이로 인한 '틀림' 오탐 방지 — 저장/조회 양쪽에서 항상 이 함수를 거친다."""
    return raw.strip().upper()


class InviteCodeRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_code(self, raw_code: str) -> InviteCode | None:
        stmt = select(InviteCode).where(InviteCode.code == normalize_code(raw_code))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_used(self, row: InviteCode, *, used_by_user_id: UUID) -> None:
        """`row.used_at is None` 인 걸 호출자가 이미 확인했다는 전제로 소진 스탬프만 찍는다."""
        row.used_at = datetime.now(UTC)
        row.used_by_user_id = used_by_user_id
        await self._session.flush()

    async def create(self, raw_code: str, *, note: str | None = None) -> InviteCode:
        """운영 발급 (`scripts/manage_invite_codes.py`). 중복 코드는 DB unique 제약이 막는다."""
        row = InviteCode(code=normalize_code(raw_code), note=note)
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_all(self) -> list[InviteCode]:
        """발급·소진 현황 조회 (`scripts/manage_invite_codes.py list`) — 생성순."""
        stmt = select(InviteCode).order_by(InviteCode.created_at)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_invite_code_repo(session: SessionDep) -> InviteCodeRepo:
    return InviteCodeRepo(session)
