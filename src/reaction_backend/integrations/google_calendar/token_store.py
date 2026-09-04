"""`calendar_connections` 읽기/쓰기 — 토큰은 항상 암호화 상태로 보관 (AGENTS §2).

평문 토큰이 컬럼에 닿지 않게, 이 모듈 **밖으로는 복호화된 access token 만** 나가고
저장은 전부 `encrypt_oauth_token` 을 거친다. 라우터는 암호문을 볼 일이 없다.

연결은 사용자당 하나(`user_id` 유니크). 재연결은 새 행이 아니라 **기존 행 갱신**이다 —
soft delete 관례상 `revoked_at` 이 찍힌 행이 남아 있으므로, 새로 INSERT 하면 유니크
제약에 걸린다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.calendar_connection import CalendarConnection
from reaction_backend.integrations.google_calendar.oauth import TokenBundle
from reaction_backend.safety.encryption import decrypt_oauth_token, encrypt_oauth_token


async def get_active(session: AsyncSession, *, user_id: uuid.UUID) -> CalendarConnection | None:
    """이 사용자의 **살아 있는** 연결 (`revoked_at IS NULL`). 없으면 None."""
    stmt = select(CalendarConnection).where(
        CalendarConnection.user_id == user_id,
        CalendarConnection.provider == "google",
        CalendarConnection.revoked_at.is_(None),
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_any(session: AsyncSession, *, user_id: uuid.UUID) -> CalendarConnection | None:
    """회수된 연결까지 포함 — 재연결이 기존 행을 되살리기 위해 쓴다."""
    stmt = select(CalendarConnection).where(
        CalendarConnection.user_id == user_id,
        CalendarConnection.provider == "google",
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def save(
    session: AsyncSession, *, user_id: uuid.UUID, bundle: TokenBundle
) -> CalendarConnection:
    """최초 연결·재연결 — 있으면 갱신, 없으면 생성. commit 은 호출자 책임.

    `bundle.refresh_token` 이 None 이면 **기존 값을 유지한다.** Google 은 refresh token 을
    최초 동의 때만 주므로, None 을 그대로 쓰면 재연결이 연결을 망가뜨린다.
    """
    existing = await _get_any(session, user_id=user_id)
    if existing is None:
        if bundle.refresh_token is None:  # pragma: no cover — exchange_code 가 먼저 막는다
            raise ValueError("최초 연결에는 refresh_token 이 필요하다")
        connection = CalendarConnection(
            user_id=user_id,
            provider="google",
            access_token_encrypted=encrypt_oauth_token(bundle.access_token),
            refresh_token_encrypted=encrypt_oauth_token(bundle.refresh_token),
            expires_at=bundle.expires_at,
            scopes=bundle.scopes,
        )
        session.add(connection)
        await session.flush()
        return connection

    existing.access_token_encrypted = encrypt_oauth_token(bundle.access_token)
    if bundle.refresh_token is not None:
        existing.refresh_token_encrypted = encrypt_oauth_token(bundle.refresh_token)
    existing.expires_at = bundle.expires_at
    existing.scopes = bundle.scopes
    existing.revoked_at = None  # 재연결 — 되살린다
    await session.flush()
    return existing


async def mark_revoked(session: AsyncSession, connection: CalendarConnection) -> None:
    """연결 해제 = soft delete (`revoked_at`). hard delete 금지 (AGENTS §2).

    토큰 암호문은 남긴다 — 사용자가 다시 연결하면 같은 행을 되살리고, 감사 흔적으로도
    "언제 연결했다가 언제 끊었는지"가 남아야 한다. 이미 회수된 연결에 다시 불러도 안전하다.
    """
    if connection.revoked_at is None:
        connection.revoked_at = datetime.now(UTC)


def access_token_of(connection: CalendarConnection) -> str:
    return decrypt_oauth_token(connection.access_token_encrypted)


def refresh_token_of(connection: CalendarConnection) -> str:
    return decrypt_oauth_token(connection.refresh_token_encrypted)
