"""Google Calendar 연결 — OAuth 코드 교환·토큰 보관·해제 (#17 해제, ADR-0009 D4).

이 파일이 못 박는 것:

- 기능 스위치가 꺼져 있으면 예전처럼 501 (자격증명 셋업 전 배포 안전핀).
- 토큰은 **평문으로 컬럼에 닿지 않는다** (AGENTS §2).
- 재연결은 새 행이 아니라 기존 행 갱신 — `user_id` 유니크 + soft delete 라 INSERT 는 깨진다.
- 갱신 응답에 refresh_token 이 없어도 **기존 값을 잃지 않는다** (Google 은 최초 동의 때만 준다).
- 해제는 멱등 — 연결이 없어도 204. 우리 DB 를 먼저 확정하고 원격 회수는 그 뒤에.
- 스코프는 freebusy 하나 (제목·장소를 읽지 않는다).

실 Google 왕복은 하지 않는다 — `oauth.exchange_code`/`revoke` 를 stub 한다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.db.models.calendar_connection import CalendarConnection
from reaction_backend.db.models.user import User
from reaction_backend.integrations.google_calendar import oauth, token_store
from reaction_backend.safety.encryption import decrypt_oauth_token

pytestmark = pytest.mark.usefixtures("real_db_session")


def _bundle(
    access: str = "access-1", refresh: str | None = "refresh-1", scopes: str | None = None
) -> oauth.TokenBundle:
    return oauth.TokenBundle(
        access_token=access,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token=refresh,
        scopes=scopes or oauth.CALENDAR_SCOPE,
    )


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"cal+{user_id}@test.local", name="캘린더 테스트"))
    await session.flush()
    return user_id


# ── 토큰 보관 ────────────────────────────────────────────────────────────


async def test_tokens_are_never_stored_in_plaintext(real_db_session: AsyncSession) -> None:
    """컬럼에 평문이 있으면 안 된다 — `*_encrypted` 규약 (AGENTS §2)."""
    user_id = await _seed_user(real_db_session)

    await token_store.save(real_db_session, user_id=user_id, bundle=_bundle())

    row = (
        await real_db_session.execute(
            select(CalendarConnection).where(CalendarConnection.user_id == user_id)
        )
    ).scalar_one()
    assert row.access_token_encrypted != "access-1"
    assert row.refresh_token_encrypted != "refresh-1"
    # 그리고 되읽으면 원문이 나와야 한다 (암호문이 쓰레기면 여기서 깨진다)
    assert decrypt_oauth_token(row.access_token_encrypted) == "access-1"
    assert token_store.refresh_token_of(row) == "refresh-1"


async def test_reconnecting_updates_the_existing_row(real_db_session: AsyncSession) -> None:
    """재연결은 새 행이 아니다 — user_id 유니크 + soft delete 라 INSERT 면 깨진다."""
    user_id = await _seed_user(real_db_session)
    first = await token_store.save(real_db_session, user_id=user_id, bundle=_bundle())
    await token_store.mark_revoked(real_db_session, first)

    second = await token_store.save(
        real_db_session, user_id=user_id, bundle=_bundle(access="access-2", refresh="refresh-2")
    )

    assert second.id == first.id
    assert second.revoked_at is None, "재연결이 연결을 되살리지 않았다"
    rows = (
        (
            await real_db_session.execute(
                select(CalendarConnection).where(CalendarConnection.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_refresh_without_new_refresh_token_keeps_the_old_one(
    real_db_session: AsyncSession,
) -> None:
    """Google 은 refresh_token 을 최초 동의 때만 준다.

    갱신 응답의 None 을 그대로 저장하면 다음 갱신이 불가능해져 **연결이 조용히 죽는다.**
    """
    user_id = await _seed_user(real_db_session)
    await token_store.save(real_db_session, user_id=user_id, bundle=_bundle())

    updated = await token_store.save(
        real_db_session, user_id=user_id, bundle=_bundle(access="access-2", refresh=None)
    )

    assert token_store.access_token_of(updated) == "access-2"
    assert token_store.refresh_token_of(updated) == "refresh-1", "기존 refresh token 을 잃었다"


async def test_revoked_connection_is_not_active(real_db_session: AsyncSession) -> None:
    """해제는 soft delete — 행은 남고 `get_active` 에서만 빠진다 (AGENTS §2 hard delete 금지)."""
    user_id = await _seed_user(real_db_session)
    connection = await token_store.save(real_db_session, user_id=user_id, bundle=_bundle())

    await token_store.mark_revoked(real_db_session, connection)

    assert await token_store.get_active(real_db_session, user_id=user_id) is None
    still_there = (
        await real_db_session.execute(
            select(CalendarConnection).where(CalendarConnection.user_id == user_id)
        )
    ).scalar_one()
    assert still_there.revoked_at is not None


async def test_mark_revoked_is_idempotent(real_db_session: AsyncSession) -> None:
    """두 번 해제해도 최초 해제 시각을 덮어쓰지 않는다."""
    user_id = await _seed_user(real_db_session)
    connection = await token_store.save(real_db_session, user_id=user_id, bundle=_bundle())

    await token_store.mark_revoked(real_db_session, connection)
    first_revoked_at = connection.revoked_at
    await token_store.mark_revoked(real_db_session, connection)

    assert connection.revoked_at == first_revoked_at


# ── 스코프 ───────────────────────────────────────────────────────────────


def test_scope_is_freebusy_only() -> None:
    """제목·장소를 읽는 `calendar.readonly` 로 넓히면 ADR 을 먼저 고쳐야 한다 (ADR-0009 D4).

    스코프가 조용히 넓어지는 것은 개인정보 범위가 조용히 넓어지는 것과 같다.
    """
    assert oauth.CALENDAR_SCOPE == "https://www.googleapis.com/auth/calendar.freebusy"
    assert "readonly" not in oauth.CALENDAR_SCOPE
    assert "events" not in oauth.CALENDAR_SCOPE


# ── 라우터 ───────────────────────────────────────────────────────────────


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "google_calendar_enabled", True, raising=False)
    monkeypatch.setattr(settings, "google_oauth_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "google_oauth_client_secret", "secret", raising=False)


def test_connect_is_501_while_the_switch_is_off(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """자격증명 셋업 전 배포돼도 사용자가 깨진 동의 화면을 만나지 않게 하는 안전핀."""
    settings = get_settings()
    monkeypatch.setattr(settings, "google_calendar_enabled", False, raising=False)

    response = client.post("/calendar/connect", json={"code": "x"})

    assert response.status_code == 501
    assert response.json()["code"] == "COMMON_NOT_IMPLEMENTED"


def test_connect_is_501_when_credentials_are_missing(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """스위치만 켜고 client_secret 을 안 넣은 배포 — 켜졌다고 믿게 두면 안 된다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "google_calendar_enabled", True, raising=False)
    monkeypatch.setattr(settings, "google_oauth_client_secret", "", raising=False)

    response = client.post("/calendar/connect", json={"code": "x"})

    assert response.status_code == 501


def test_connect_maps_oauth_failure_to_422(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """만료·재사용된 code — Google 의 error 문자열을 사용자에게 노출하지 않는다."""
    _enable(monkeypatch)

    async def _fail(code: str, *, redirect_uri: str | None = None) -> oauth.TokenBundle:
        raise oauth.OAuthError("invalid_grant", retryable=False)

    monkeypatch.setattr(oauth, "exchange_code", _fail)

    response = client.post("/calendar/connect", json={"code": "expired"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "COMMON_VALIDATION_ERROR"
    assert "invalid_grant" not in body["message"]


def test_disconnect_without_a_connection_is_204(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """해제는 멱등 — 두 번 눌렀다고 404 를 주면 '이미 끊김' 이 실패로 보인다."""
    _enable(monkeypatch)

    response = client.delete("/calendar/connect")

    assert response.status_code == 204


# ── OAuth 응답 파싱 ──────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_5xx_is_retryable_but_4xx_is_not() -> None:
    """일시적 장애로 사용자에게 재연결을 요구하면 안 된다 — 그 구분이 `retryable` 이다."""
    with pytest.raises(oauth.OAuthError) as server_error:
        oauth._raise_for_error(_FakeResponse(503, {}))
    assert server_error.value.retryable is True

    with pytest.raises(oauth.OAuthError) as client_error:
        oauth._raise_for_error(_FakeResponse(400, {"error": "invalid_grant"}))
    assert client_error.value.retryable is False
    assert client_error.value.reason == "invalid_grant"


def test_missing_expires_in_falls_back_to_a_short_ttl() -> None:
    """길게 잡아 만료된 토큰을 쓰는 것보다 한 번 더 갱신하는 편이 안전하다."""
    before = datetime.now(UTC)

    bundle = oauth._bundle_from({"access_token": "a"}, fallback_scopes=oauth.CALENDAR_SCOPE)

    assert bundle.expires_at <= before + timedelta(seconds=301)
    assert bundle.refresh_token is None  # 없으면 None — 호출자가 기존 값을 유지한다


def test_response_without_access_token_is_an_error() -> None:
    """200 이어도 토큰이 없으면 연결이 아니다 — 반쯤 된 연결을 저장하지 않는다."""
    with pytest.raises(oauth.OAuthError):
        oauth._bundle_from({"scope": oauth.CALENDAR_SCOPE}, fallback_scopes="")


async def test_exchange_rejects_a_grant_without_a_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """refresh_token 없이 저장하면 **하루 뒤에 조용히 죽는** 연결이 된다.

    동의 URL 에 `access_type=offline` 이 빠졌을 때 실제로 이런 응답이 온다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "google_oauth_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "google_oauth_client_secret", "secret", raising=False)

    async def _no_refresh(url: str, data: dict[str, str]) -> Any:
        return _FakeResponse(200, {"access_token": "a", "expires_in": 3600})

    monkeypatch.setattr(oauth, "_post_async", _no_refresh)

    with pytest.raises(oauth.OAuthError) as exc:
        await oauth.exchange_code("code")
    assert exc.value.reason == "no_refresh_token"


async def test_revoke_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """원격 회수가 실패해도 사용자는 연결을 해제할 수 있어야 한다."""

    async def _boom(url: str, data: dict[str, str]) -> Any:
        raise oauth.OAuthError("network", retryable=True)

    monkeypatch.setattr(oauth, "_post_async", _boom)

    await oauth.revoke("token")  # 예외 없음


def test_disconnect_revokes_remotely_after_committing_locally(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """해제는 두 가지를 **이 순서로** 한다: 우리 DB 확정 → Google 권한 회수.

    순서를 뒤집으면 Google 은 끊겼는데 우리는 연결됐다고 믿는 상태가 생긴다. 그리고
    원격 회수를 빠뜨리면 사용자가 앱에서 끊어도 Google 계정에는 권한이 남는다 —
    "연결 해제" 가 절반만 되는 셈이다.
    """
    _enable(monkeypatch)
    calls: list[str] = []

    class _Conn:
        provider = "google"
        revoked_at = None
        scopes = oauth.CALENDAR_SCOPE

    async def _active(session: Any, *, user_id: uuid.UUID) -> Any:
        return _Conn()

    def _refresh_of(connection: Any) -> str:
        return "refresh-live"

    async def _mark(session: Any, connection: Any) -> None:
        calls.append("mark_revoked")

    async def _revoke(token: str) -> None:
        calls.append(f"revoke:{token}")

    monkeypatch.setattr(token_store, "get_active", _active)
    monkeypatch.setattr(token_store, "refresh_token_of", _refresh_of)
    monkeypatch.setattr(token_store, "mark_revoked", _mark)
    monkeypatch.setattr(oauth, "revoke", _revoke)

    response = client.delete("/calendar/connect")

    assert response.status_code == 204
    assert calls == ["mark_revoked", "revoke:refresh-live"], calls
