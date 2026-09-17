"""freebusy 조회와 그 결과가 계획에 반영되는지 (ADR-0009 D4 — busy 소스 배선).

이 파일이 못 박는 것:

- 자정을 넘는 일정은 **두 날짜로 쪼개진다** — 시작일에만 달면 다음 날 새벽이 비어 보인다.
- 응답 파싱: UTC → KST, `errors` 가 있으면 "일정 없음" 이 아니라 **실패**다.
- 실패는 계획을 죽이지 않는다. 다만 **연결한 사용자에게는** 경고로 알린다.
- 연결 안 됨(대다수)에는 아무 말도 하지 않는다 — 매번 권유하면 알림 피로가 된다.
- 만료 임박이면 갱신하고, `invalid_grant` 면 연결을 회수한다(일시적 실패는 안 끊는다).

실 Google 왕복은 하지 않는다 — HTTP 층만 stub 한다.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from reaction_backend.integrations.google_calendar import freebusy, oauth
from reaction_backend.orchestrator.goal_structuring import TimeInterval
from reaction_backend.schemas.common import KST


def _iv(start: str, end: str) -> TimeInterval:
    return TimeInterval(datetime.fromisoformat(start), datetime.fromisoformat(end))


# ── 응답 파싱 ────────────────────────────────────────────────────────────


def test_parses_utc_into_kst() -> None:
    """Google 은 UTC(Z)로 준다. KST 로 안 바꾸면 9시간 어긋난 자리에 계획이 잡힌다."""
    intervals = freebusy._parse(
        {
            "calendars": {
                "primary": {
                    "busy": [{"start": "2026-09-01T01:00:00Z", "end": "2026-09-01T03:00:00Z"}]
                }
            }
        }
    )

    assert len(intervals) == 1
    assert intervals[0].start == datetime(2026, 9, 1, 10, 0, tzinfo=KST)
    assert intervals[0].end == datetime(2026, 9, 1, 12, 0, tzinfo=KST)


def test_calendar_errors_are_a_failure_not_an_empty_day() -> None:
    """`errors` 를 빈 목록으로 흘리면 '일정 없음' 과 구분되지 않는다 — 그 위에 계획이 잡힌다."""
    with pytest.raises(ValueError):
        freebusy._parse(
            {"calendars": {"primary": {"errors": [{"reason": "notFound"}], "busy": []}}}
        )


def test_zero_length_busy_is_dropped() -> None:
    """길이 0 구간은 free 계산에서 의미가 없다."""
    intervals = freebusy._parse(
        {
            "calendars": {
                "primary": {
                    "busy": [{"start": "2026-09-01T01:00:00Z", "end": "2026-09-01T01:00:00Z"}]
                }
            }
        }
    )
    assert intervals == []


# ── 날짜별 분해 ──────────────────────────────────────────────────────────


def test_overnight_event_is_split_across_days() -> None:
    """23:00~01:00 을 시작일에만 달면 **다음 날 새벽이 비어 보여** 그 위에 카드가 잡힌다."""
    by_day = freebusy.split_by_day([_iv("2026-09-01T23:00:00+09:00", "2026-09-02T01:00:00+09:00")])

    assert set(by_day) == {date(2026, 9, 1), date(2026, 9, 2)}
    first = by_day[date(2026, 9, 1)][0].interval
    second = by_day[date(2026, 9, 2)][0].interval
    assert first.end == datetime(2026, 9, 2, 0, 0, tzinfo=KST)
    assert second.start == datetime(2026, 9, 2, 0, 0, tzinfo=KST)
    assert second.end == datetime(2026, 9, 2, 1, 0, tzinfo=KST)


def test_multi_day_event_covers_every_day() -> None:
    """3일짜리 여행 일정 — 가운데 날이 통째로 busy 여야 한다."""
    by_day = freebusy.split_by_day([_iv("2026-09-01T10:00:00+09:00", "2026-09-03T18:00:00+09:00")])

    assert set(by_day) == {date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)}
    middle = by_day[date(2026, 9, 2)][0].interval
    assert middle.start.hour == 0
    assert middle.end == datetime(2026, 9, 3, 0, 0, tzinfo=KST)


def test_busy_blocks_are_labelled_as_calendar() -> None:
    """source 라벨이 있어야 나중에 '어느 소스가 이 시간을 막았나' 를 설명할 수 있다."""
    by_day = freebusy.split_by_day([_iv("2026-09-01T10:00:00+09:00", "2026-09-01T11:00:00+09:00")])
    block = by_day[date(2026, 9, 1)][0]
    assert block.source == "calendar"


# ── 실패 처리 ────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeSession:
    """commit/flush 호출을 기록한다 — freebusy 는 flush 까지만 해야 한다."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def commit(self) -> None:
        self.calls.append("commit")

    async def flush(self) -> None:
        self.calls.append("flush")


async def _fetch_with(monkeypatch: pytest.MonkeyPatch, response: Any) -> freebusy.FreeBusyResult:
    async def _token(session: Any, *, user_id: uuid.UUID) -> str:
        return "access"

    def _query(access_token: str, start: datetime, end: datetime) -> Any:
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(freebusy, "_access_token", _token)
    monkeypatch.setattr(freebusy, "_query", _query)
    return await freebusy.fetch_busy(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        start=datetime(2026, 9, 1, tzinfo=KST),
        end=datetime(2026, 9, 2, tzinfo=KST),
    )


async def test_http_error_is_failed_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """403(스코프 회수 등)을 빈 목록으로 흘리면 계획이 남의 일정 위에 잡힌다."""
    result = await _fetch_with(monkeypatch, _FakeResponse(403, {}))

    assert result.status == "failed"
    assert result.connected_but_failed is True


async def test_failure_reason_is_in_the_log_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """운영 로그 형식은 `%(message)s` 까지만 찍는다 — `extra=` 로 넘긴 사유는 사라졌다.

    그래서 사유를 메시지 안에 둔다. 스테이징 journald 에서 이 한 줄이 유일한 단서다.
    """
    caplog.set_level(logging.INFO, logger="reaction_backend.integrations.google_calendar.freebusy")

    await _fetch_with(monkeypatch, _FakeResponse(403, {}))

    assert "calendar_freebusy_failed reason=http_403" in [r.getMessage() for r in caplog.records]


async def test_timeout_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    result = await _fetch_with(monkeypatch, requests.Timeout("slow"))
    assert result.status == "failed"


async def test_no_connection_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """연결 안 한 사용자가 대다수다 — 이건 경고할 일이 아니다."""

    async def _no_token(session: Any, *, user_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(freebusy, "_access_token", _no_token)

    result = await freebusy.fetch_busy(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        start=datetime(2026, 9, 1, tzinfo=KST),
        end=datetime(2026, 9, 2, tzinfo=KST),
    )

    assert result.status == "not_connected"
    assert result.connected_but_failed is False


# ── 토큰 갱신 ────────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, expires_at: datetime) -> None:
        self.expires_at = expires_at
        self.scopes = oauth.CALENDAR_SCOPE
        self.revoked_at: datetime | None = None


async def test_permanent_refresh_failure_revokes_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`invalid_grant` = 사용자가 Google 에서 권한을 뺐다 — 다음 진입에 재연결을 안내해야 한다."""
    connection = _Conn(datetime.now(UTC) - timedelta(minutes=5))
    revoked: list[str] = []

    async def _active(session: Any, *, user_id: uuid.UUID) -> Any:
        return connection

    async def _refresh(token: str, *, known_scopes: str) -> Any:
        raise oauth.OAuthError("invalid_grant", retryable=False)

    async def _mark(session: Any, conn: Any) -> None:
        revoked.append("yes")

    monkeypatch.setattr(freebusy.token_store, "get_active", _active)
    monkeypatch.setattr(freebusy.token_store, "refresh_token_of", lambda c: "r")
    monkeypatch.setattr(freebusy.token_store, "mark_revoked", _mark)
    monkeypatch.setattr(freebusy.oauth, "refresh_access_token", _refresh)

    session = _FakeSession()
    token = await freebusy._access_token(session, user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert token is None
    assert revoked == ["yes"]
    # 회수 표시는 호출자의 commit 에 실린다 — 여기서 commit 하면 계획 생성의 lock 이 풀린다.
    assert session.calls == ["flush"]


async def test_temporary_refresh_failure_keeps_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """네트워크가 흔들렸다고 연결을 끊으면 사용자가 이유 없이 재연결을 요구받는다."""
    connection = _Conn(datetime.now(UTC) - timedelta(minutes=5))
    revoked: list[str] = []

    async def _active(session: Any, *, user_id: uuid.UUID) -> Any:
        return connection

    async def _refresh(token: str, *, known_scopes: str) -> Any:
        raise oauth.OAuthError("network", retryable=True)

    async def _mark(session: Any, conn: Any) -> None:
        revoked.append("yes")

    monkeypatch.setattr(freebusy.token_store, "get_active", _active)
    monkeypatch.setattr(freebusy.token_store, "refresh_token_of", lambda c: "r")
    monkeypatch.setattr(freebusy.token_store, "mark_revoked", _mark)
    monkeypatch.setattr(freebusy.oauth, "refresh_access_token", _refresh)

    token = await freebusy._access_token(_FakeSession(), user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert token is None
    assert revoked == [], "일시적 실패로 연결을 끊었다"


async def test_successful_refresh_is_saved_without_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """갱신 성공 경로도 commit 하지 않는다.

    계획 생성·재계획은 `pg_advisory_xact_lock` 을 쥔 채 이걸 부른다. 예전엔 여기서 commit
    해서, 토큰이 만료되는 한 시간마다 한 번씩 **계획 생성 도중에 lock 이 풀렸다.**
    """
    connection = _Conn(datetime.now(UTC) - timedelta(minutes=5))
    saved: list[str] = []

    async def _active(session: Any, *, user_id: uuid.UUID) -> Any:
        return connection

    async def _refresh(token: str, *, known_scopes: str) -> Any:
        return oauth.TokenBundle(
            access_token="fresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            refresh_token=None,
            scopes=known_scopes,
        )

    async def _save(session: Any, *, user_id: uuid.UUID, bundle: Any) -> Any:
        saved.append(bundle.access_token)
        return connection

    monkeypatch.setattr(freebusy.token_store, "get_active", _active)
    monkeypatch.setattr(freebusy.token_store, "refresh_token_of", lambda c: "r")
    monkeypatch.setattr(freebusy.token_store, "save", _save)
    monkeypatch.setattr(freebusy.oauth, "refresh_access_token", _refresh)

    session = _FakeSession()
    token = await freebusy._access_token(session, user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert token == "fresh"
    assert saved == ["fresh"]
    assert "commit" not in session.calls


def test_freebusy_route_commits_what_the_lookup_changed(
    client: Any, monkeypatch: pytest.MonkeyPatch, fake_sessions: list[Any]
) -> None:
    """lock 이 없는 조회 라우트는 스스로 commit 한다 — 안 하면 갱신한 토큰이 요청과 함께 사라진다."""
    from reaction_backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "google_calendar_enabled", True, raising=False)
    monkeypatch.setattr(settings, "google_oauth_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "google_oauth_client_secret", "secret", raising=False)

    async def _fetch(session: Any, *, user_id: uuid.UUID, start: datetime, end: datetime) -> Any:
        return freebusy.FreeBusyResult("ok", [])

    monkeypatch.setattr(freebusy, "fetch_busy", _fetch)

    response = client.get("/calendar/freebusy", params={"from": "2026-09-14", "to": "2026-09-20"})

    assert response.status_code == 200
    assert response.json() == {"busy": []}
    assert sum(s.commit_count for s in fake_sessions) == 1
