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
    """`_access_token` 만 우회하면 되므로 세션은 실제로 쓰이지 않는다."""

    async def commit(self) -> None:  # pragma: no cover
        return None


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

    token = await freebusy._access_token(_FakeSession(), user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert token is None
    assert revoked == ["yes"]


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
