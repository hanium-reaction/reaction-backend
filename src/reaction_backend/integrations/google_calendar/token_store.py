"""`calendar_connections` 읽기/쓰기 — 토큰은 항상 암호화 상태로 보관 (AGENTS §2).

평문 토큰이 컬럼에 닿지 않게, 이 모듈 **밖으로는 복호화된 access token 만** 나가고
저장은 전부 `encrypt_oauth_token` 을 거친다. 라우터는 암호문을 볼 일이 없다.

연결은 사용자당 하나(`user_id` 유니크). 재연결은 새 행이 아니라 **기존 행 갱신**이다 —
soft delete 관례상 `revoked_at` 이 찍힌 행이 남아 있으므로, 새로 INSERT 하면 유니크
제약에 걸린다.

## "Google 쪽에서 끊겼다" 표식

회수된 연결에는 두 종류가 있다 — 사용자가 앱에서 **해제**한 것(조용히 둔다)과, 토큰 갱신이
`invalid_grant` 로 떨어져 **Google 쪽에서 끊긴** 것(재연결을 안내해야 한다). 둘을 가를
컬럼이 없고 마이그레이션 없이 고치려고, 후자는 `expires_at` 을 `_REVOKED_BY_GOOGLE`
(1970-01-01)로 못 박는다. 회수된 행의 `expires_at` 은 어차피 의미가 없고(토큰이 죽었다),
실제 토큰 만료 시각이 1970년일 수는 없으니 해제한 행과 섞이지 않는다. 재연결(`save`)이
`expires_at` 을 덮어쓰면 표식은 저절로 사라진다. 이 규칙 이전에 갱신 실패로 회수된 행은
표식이 없어 예전처럼 조용하다 — 새 표식을 잘못 켜는 쪽으로는 틀리지 않는다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.calendar_connection import CalendarConnection
from reaction_backend.integrations.google_calendar.oauth import TokenBundle
from reaction_backend.safety.encryption import decrypt_oauth_token, encrypt_oauth_token

#: Google 쪽에서 끊긴 연결의 `expires_at` — 모듈 독스트링 "Google 쪽에서 끊겼다" 표식.
_REVOKED_BY_GOOGLE: Final = datetime(1970, 1, 1, tzinfo=UTC)


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


async def mark_revoked(
    session: AsyncSession, connection: CalendarConnection, *, by_google: bool = False
) -> None:
    """연결 해제 = soft delete (`revoked_at`). hard delete 금지 (AGENTS §2).

    토큰 암호문은 남긴다 — 사용자가 다시 연결하면 같은 행을 되살리고, 감사 흔적으로도
    "언제 연결했다가 언제 끊었는지"가 남아야 한다. 이미 회수된 연결에 다시 불러도 안전하다.

    `by_google` — 사용자가 해제한 게 아니라 갱신이 `invalid_grant` 로 떨어졌다. 재연결 안내
    표식을 남긴다(모듈 독스트링).
    """
    if connection.revoked_at is None:
        connection.revoked_at = datetime.now(UTC)
        if by_google:
            connection.expires_at = _REVOKED_BY_GOOGLE


def revoked_by_google(connection: CalendarConnection) -> bool:
    """회수됐는데 그게 Google 쪽에서 끊긴 것인가 — 사용자에게 재연결을 안내할 상태."""
    return connection.revoked_at is not None and connection.expires_at == _REVOKED_BY_GOOGLE


async def needs_reconnect(session: AsyncSession, *, user_id: uuid.UUID) -> bool:
    """살아 있는 연결은 없고, 마지막 연결이 **Google 쪽에서** 끊겼는가.

    연결한 적 없는 사용자·앱에서 해제한 사용자는 False — 그들에게는 아무 말도 하지 않는다.
    """
    connection = await _get_any(session, user_id=user_id)
    return connection is not None and revoked_by_google(connection)


async def dismiss_reconnect(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    """Google 쪽에서 끊긴 연결을 사용자가 '해제' 로 정리했다 — 재연결 안내를 거둔다.

    끊긴 걸 알고도 다시 연결하지 않기로 한 사용자에게 계획마다 같은 안내를 반복하면 잔소리다.
    표식만 지운다(`expires_at` 을 회수 시각으로) — 행과 `revoked_at` 은 그대로 남는다.
    commit 은 호출자 몫.
    """
    connection = await _get_any(session, user_id=user_id)
    if connection is None or connection.revoked_at is None or not revoked_by_google(connection):
        return
    connection.expires_at = connection.revoked_at
    await session.flush()


def access_token_of(connection: CalendarConnection) -> str:
    return decrypt_oauth_token(connection.access_token_encrypted)


def refresh_token_of(connection: CalendarConnection) -> str:
    return decrypt_oauth_token(connection.refresh_token_encrypted)
