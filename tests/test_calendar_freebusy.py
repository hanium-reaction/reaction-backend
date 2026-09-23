"""freebusy 조회와 그 결과가 계획에 반영되는지 (ADR-0009 D4 — busy 소스 배선).

이 파일이 못 박는 것:

- 자정을 넘는 일정은 **두 날짜로 쪼개진다** — 시작일에만 달면 다음 날 새벽이 비어 보인다.
- 응답 파싱: UTC → KST, `errors` 가 있으면 "일정 없음" 이 아니라 **실패**다.
- 실패는 계획을 죽이지 않는다. 다만 **연결한 사용자에게는** 경고로 알린다.
- 연결 안 됨(대다수)에는 아무 말도 하지 않는다 — 매번 권유하면 알림 피로가 된다.
- 만료 임박이면 갱신하고, `invalid_grant` 면 연결을 회수한다(일시적 실패는 안 끊는다).
- Google 쪽에서 끊긴 연결은 조용한 `not_connected` 가 아니라 `reconnect_required` 다.
- 기능 스위치가 꺼져 있으면 어느 경로로도 Google 을 읽지 않는다.

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
    async def _token(session: Any, *, user_id: uuid.UUID, **_: Any) -> str:
        return "access"

    def _query(
        access_token: str, start: datetime, end: datetime, timeout: tuple[float, float]
    ) -> Any:
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(freebusy.oauth, "is_enabled", lambda: True)
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

    async def _no_token(session: Any, *, user_id: uuid.UUID, **_: Any) -> None:
        return None

    monkeypatch.setattr(freebusy.oauth, "is_enabled", lambda: True)
    monkeypatch.setattr(freebusy, "_access_token", _no_token)

    result = await freebusy.fetch_busy(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        start=datetime(2026, 9, 1, tzinfo=KST),
        end=datetime(2026, 9, 2, tzinfo=KST),
    )

    assert result.status == "not_connected"
    assert result.connected_but_failed is False
    assert result.reconnect_required is False, "연결한 적 없는 사용자에게 재연결을 권했다"


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
    revoked: list[bool] = []

    async def _active(session: Any, *, user_id: uuid.UUID) -> Any:
        return connection

    async def _refresh(token: str, *, known_scopes: str) -> Any:
        raise oauth.OAuthError("invalid_grant", retryable=False)

    async def _mark(session: Any, conn: Any, *, by_google: bool = False) -> None:
        revoked.append(by_google)

    monkeypatch.setattr(freebusy.token_store, "get_active", _active)
    monkeypatch.setattr(freebusy.token_store, "refresh_token_of", lambda c: "r")
    monkeypatch.setattr(freebusy.token_store, "mark_revoked", _mark)
    monkeypatch.setattr(freebusy.oauth, "refresh_access_token", _refresh)

    session = _FakeSession()
    with pytest.raises(freebusy._TokenUnavailable) as exc:
        await freebusy._access_token(session, user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert exc.value.reconnect_required is True
    # "Google 쪽에서 끊김" 표식 — 사용자가 앱에서 해제한 것과 구분돼야 재연결을 안내할 수 있다.
    assert revoked == [True]
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

    async def _mark(session: Any, conn: Any, **_: Any) -> None:
        revoked.append("yes")

    monkeypatch.setattr(freebusy.token_store, "get_active", _active)
    monkeypatch.setattr(freebusy.token_store, "refresh_token_of", lambda c: "r")
    monkeypatch.setattr(freebusy.token_store, "mark_revoked", _mark)
    monkeypatch.setattr(freebusy.oauth, "refresh_access_token", _refresh)

    with pytest.raises(freebusy._TokenUnavailable) as exc:
        await freebusy._access_token(_FakeSession(), user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert exc.value.reconnect_required is False
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


# ── 화면 조회 캐시 (오늘·주간) ─────────────────────────────────────────────


def _range() -> tuple[datetime, datetime]:
    start = datetime(2026, 9, 17, tzinfo=KST)
    return start, start + timedelta(days=1)


def _counting_fetch(
    monkeypatch: pytest.MonkeyPatch, status: str, intervals: list[TimeInterval] | None = None
) -> list[float]:
    """`fetch_busy` 대역 — 부를 때마다 hard_timeout 을 기록한다."""
    calls: list[float] = []

    async def _fetch(
        session: Any,
        *,
        user_id: uuid.UUID,
        start: datetime,
        end: datetime,
        hard_timeout: float,
        **_: Any,
    ) -> freebusy.FreeBusyResult:
        calls.append(hard_timeout)
        return freebusy.FreeBusyResult(status, intervals or [])  # type: ignore[arg-type]

    monkeypatch.setattr(freebusy, "fetch_busy", _fetch)
    return calls


async def test_screen_lookup_is_cached_and_uses_the_short_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """화면 전환마다 Google 을 치지 않는다 — 그리고 사용자가 기다리니 2초 상한이다."""
    calls = _counting_fetch(monkeypatch, "ok")
    user_id = uuid.uuid4()
    start, end = _range()

    first, first_at = await freebusy.fetch_busy_for_screen(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=user_id,
        start=start,
        end=end,
    )
    second, second_at = await freebusy.fetch_busy_for_screen(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=user_id,
        start=start,
        end=end,
    )

    assert calls == [freebusy.SCREEN_HARD_TIMEOUT]
    assert first is second and first_at == second_at  # 캐시 적중 — 확인 시각도 그때 것


async def test_screen_cache_expires_and_can_be_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counting_fetch(monkeypatch, "ok")
    user_id = uuid.uuid4()
    start, end = _range()
    session: Any = _FakeSession()

    await freebusy.fetch_busy_for_screen(session, user_id=user_id, start=start, end=end)
    freebusy.clear_screen_cache(user_id)  # 연결·해제 직후 라우터가 하는 일
    await freebusy.fetch_busy_for_screen(session, user_id=user_id, start=start, end=end)
    monkeypatch.setattr(freebusy, "SCREEN_CACHE_TTL", timedelta(0))
    await freebusy.fetch_busy_for_screen(session, user_id=user_id, start=start, end=end)

    assert len(calls) == 3


async def test_screen_failures_are_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google 이 잠깐 느렸다고 5분 동안 캘린더 없는 화면을 보여줄 이유가 없다."""
    calls = _counting_fetch(monkeypatch, "failed")
    user_id = uuid.uuid4()
    start, end = _range()
    session: Any = _FakeSession()

    await freebusy.fetch_busy_for_screen(session, user_id=user_id, start=start, end=end)
    await freebusy.fetch_busy_for_screen(session, user_id=user_id, start=start, end=end)

    assert len(calls) == 2


async def test_short_budget_also_shrinks_the_http_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """코루틴만 2초에 포기하고 스레드가 8초씩 남으면 화면 조회가 몰릴 때 스레드 풀이 막힌다."""
    seen: list[tuple[float, float]] = []

    async def _token(session: Any, *, user_id: uuid.UUID, **_: Any) -> str:
        return "access"

    def _query(
        access_token: str, start: datetime, end: datetime, timeout: tuple[float, float]
    ) -> Any:
        seen.append(timeout)
        return _FakeResponse(200, {"calendars": {"primary": {"busy": []}}})

    monkeypatch.setattr(freebusy.oauth, "is_enabled", lambda: True)
    monkeypatch.setattr(freebusy, "_access_token", _token)
    monkeypatch.setattr(freebusy, "_query", _query)
    start, end = _range()

    await freebusy.fetch_busy(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        start=start,
        end=end,
        hard_timeout=2.0,
    )

    assert seen == [(2.0, 2.0)]


async def test_screen_conflicts_do_not_call_google_while_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _counting_fetch(monkeypatch, "ok")
    monkeypatch.setattr(freebusy.oauth, "is_enabled", lambda: False)
    start, end = _range()

    result = await freebusy.screen_conflicts(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        start=start,
        end=end,
        blocks=[("a", "scheduled", start, end)],
        now=start,
    )

    assert (result.status, result.checked_at, result.keys) == ("not_connected", None, set())
    assert calls == []


async def test_screen_conflicts_report_overlapping_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    start, end = _range()
    busy = TimeInterval(start + timedelta(hours=10), start + timedelta(hours=11))
    _counting_fetch(monkeypatch, "ok", [busy])
    monkeypatch.setattr(freebusy.oauth, "is_enabled", lambda: True)
    blocks = [
        ("hit", "scheduled", start + timedelta(hours=10, minutes=30), start + timedelta(hours=12)),
        ("miss", "scheduled", start + timedelta(hours=13), start + timedelta(hours=14)),
    ]

    result = await freebusy.screen_conflicts(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        start=start,
        end=end,
        blocks=blocks,
        now=start,
    )

    assert result.status == "ok"
    assert result.checked_at is not None
    assert result.keys == {"hit"}


async def test_missing_server_credentials_never_revoke_a_user_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """secret 이 빠진 배포는 서버 문제다 — 그걸로 사용자 연결을 끊으면 설정을 되돌려도 복구 안 된다."""
    from reaction_backend.config import get_settings

    monkeypatch.setattr(get_settings(), "google_oauth_client_secret", "", raising=False)
    connection = _Conn(datetime.now(UTC) - timedelta(minutes=5))
    revoked: list[str] = []

    async def _active(session: Any, *, user_id: uuid.UUID) -> Any:
        return connection

    async def _mark(session: Any, conn: Any, **_: Any) -> None:
        revoked.append("yes")

    monkeypatch.setattr(freebusy.token_store, "get_active", _active)
    monkeypatch.setattr(freebusy.token_store, "refresh_token_of", lambda c: "r")
    monkeypatch.setattr(freebusy.token_store, "mark_revoked", _mark)

    with pytest.raises(freebusy._TokenUnavailable) as exc:
        await freebusy._access_token(_FakeSession(), user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert exc.value.reconnect_required is False
    assert revoked == []


# ── Google 쪽에서 끊긴 연결 · 일시적 갱신 실패 (calendar-1) ─────────────────


def _stub_expired_connection(
    monkeypatch: pytest.MonkeyPatch,
    refresh_error: oauth.OAuthError | None,
    *,
    active: bool = True,
    needs_reconnect: bool = False,
) -> list[bool]:
    """만료된 연결 + 갱신 결과를 고정한다. mark_revoked 호출(by_google 값)을 기록해 돌려준다."""
    connection = _Conn(datetime.now(UTC) - timedelta(minutes=5))
    marks: list[bool] = []

    async def _active(session: Any, *, user_id: uuid.UUID) -> Any:
        return connection if active else None

    async def _needs(session: Any, *, user_id: uuid.UUID) -> bool:
        return needs_reconnect

    async def _refresh(token: str, *, known_scopes: str) -> Any:
        if refresh_error is not None:
            raise refresh_error
        return oauth.TokenBundle(
            access_token="fresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            refresh_token=None,
            scopes=known_scopes,
        )

    async def _mark(session: Any, conn: Any, *, by_google: bool = False) -> None:
        marks.append(by_google)

    async def _save(session: Any, *, user_id: uuid.UUID, bundle: Any) -> Any:
        await session.flush()
        return connection

    monkeypatch.setattr(freebusy.oauth, "is_enabled", lambda: True)
    monkeypatch.setattr(freebusy.token_store, "get_active", _active)
    monkeypatch.setattr(freebusy.token_store, "needs_reconnect", _needs)
    monkeypatch.setattr(freebusy.token_store, "refresh_token_of", lambda c: "r")
    monkeypatch.setattr(freebusy.token_store, "mark_revoked", _mark)
    monkeypatch.setattr(freebusy.token_store, "save", _save)
    monkeypatch.setattr(freebusy.oauth, "refresh_access_token", _refresh)
    return marks


async def _fetch_day(session: Any | None = None) -> freebusy.FreeBusyResult:
    start = datetime(2026, 9, 1, tzinfo=KST)
    return await freebusy.fetch_busy(
        session or _FakeSession(),
        user_id=uuid.uuid4(),
        start=start,
        end=start + timedelta(days=1),
    )


async def test_temporary_refresh_failure_is_a_warning_not_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """토큰 갱신이 잠깐 실패해도 연결은 살아 있다 — '연결 안 됨' 으로 삼키면 경고가 안 뜬다.

    예전엔 `not_connected` 였다. 계획은 수업 위에 잡히는데 '캘린더를 못 읽었어요' 한 줄도 없었다.
    """
    marks = _stub_expired_connection(monkeypatch, oauth.OAuthError("timeout", retryable=True))

    result = await _fetch_day()

    assert result.status == "failed"
    assert result.reconnect_required is False
    assert marks == [], "일시적 실패로 연결을 끊었다"


async def test_revoked_grant_asks_to_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """`invalid_grant` 로 방금 회수한 연결 — 조용한 '연결 안 됨' 이 아니라 재연결 안내다."""
    marks = _stub_expired_connection(
        monkeypatch, oauth.OAuthError("invalid_grant", retryable=False)
    )

    result = await _fetch_day()

    assert (result.status, result.reconnect_required) == ("not_connected", True)
    assert marks == [True]


async def test_connection_cut_by_google_keeps_asking_until_the_user_acts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회수된 **뒤의** 조회도 재연결을 안내한다 — 회수가 화면 조회에서 일어났으면 계획 생성은
    그다음 호출이다. 거기서 조용하면 결국 아무도 말하지 않는 것과 같다.
    """
    _stub_expired_connection(monkeypatch, None, active=False, needs_reconnect=True)

    result = await _fetch_day()

    assert (result.status, result.reconnect_required) == ("not_connected", True)


async def test_plan_path_sees_reconnect_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """첫 계획·재계획은 `fetch_busy_by_day` 의 상태로 경고 문구를 고른다."""
    _stub_expired_connection(monkeypatch, None, active=False, needs_reconnect=True)

    by_day, status = await freebusy.fetch_busy_by_day(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        start_day=date(2026, 9, 1),
        end_day=date(2026, 9, 7),
    )

    assert (by_day, status) == ({}, "reconnect_required")


async def test_unreadable_stored_token_is_failed_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """키가 바뀌어 암호문을 못 풀어도 계획 생성이 500 으로 죽으면 안 된다 — 캘린더만 빠진다."""
    from reaction_backend.safety.encryption import EncryptionError

    _stub_expired_connection(monkeypatch, None)

    def _broken(connection: Any) -> str:
        raise EncryptionError("AES-GCM tag verification failed.")

    monkeypatch.setattr(freebusy.token_store, "refresh_token_of", _broken)

    result = await _fetch_day()

    assert result.status == "failed"


# ── 기능 스위치 (calendar-11) ─────────────────────────────────────────────


async def test_disabled_switch_stops_every_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """스위치를 끄면 계획·재계획·조회 경로도 Google 을 읽지 않는다 — 화면·브리프만 멈추던 구멍.

    토큰 조회조차 하지 않는다(갱신 왕복도 Google 호출이다).
    """
    touched: list[str] = []

    async def _token(session: Any, **_: Any) -> str:
        touched.append("token")
        return "access"

    monkeypatch.setattr(freebusy.oauth, "is_enabled", lambda: False)
    monkeypatch.setattr(freebusy, "_access_token", _token)

    by_day, status = await freebusy.fetch_busy_by_day(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        start_day=date(2026, 9, 1),
        end_day=date(2026, 9, 7),
    )

    assert (by_day, status) == ({}, "not_connected")
    assert touched == []


# ── 토큰 엔드포인트 4xx 분류 (calendar-2) ─────────────────────────────────


class _TokenResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (401, {"error": "invalid_client"}),
        (400, {"error": "unauthorized_client"}),
        (429, {"error": "rate_limit_exceeded"}),
        (403, ValueError("<html>forbidden</html>")),
    ],
)
async def test_server_side_token_errors_never_revoke_a_user_connection(
    monkeypatch: pytest.MonkeyPatch, status_code: int, payload: Any
) -> None:
    """secret 오타 하나(`invalid_client`)로 갱신한 **모든 사용자**의 연결이 끊기면 안 된다.

    설정을 고쳐도 회수된 연결은 되살아나지 않는다 — 사용자마다 이유도 모른 채 다시 연결해야 한다.
    """
    from reaction_backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "google_oauth_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "google_oauth_client_secret", "secret", raising=False)

    async def _post(url: str, data: dict[str, str]) -> Any:
        return _TokenResponse(status_code, payload)

    marks = _stub_expired_connection(monkeypatch, None)
    # 갱신은 진짜 oauth 경로로 — HTTP 층만 stub 한다.
    monkeypatch.setattr(freebusy.oauth, "refresh_access_token", _REAL_REFRESH)
    monkeypatch.setattr(oauth, "_post_async", _post)

    with pytest.raises(freebusy._TokenUnavailable) as exc:
        await freebusy._access_token(_FakeSession(), user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert exc.value.reconnect_required is False
    assert marks == [], f"{status_code} 로 사용자 연결을 회수했다"


async def test_invalid_grant_from_the_token_endpoint_still_revokes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reaction_backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "google_oauth_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "google_oauth_client_secret", "secret", raising=False)

    async def _post(url: str, data: dict[str, str]) -> Any:
        return _TokenResponse(400, {"error": "invalid_grant"})

    marks = _stub_expired_connection(monkeypatch, None)
    monkeypatch.setattr(freebusy.oauth, "refresh_access_token", _REAL_REFRESH)
    monkeypatch.setattr(oauth, "_post_async", _post)

    with pytest.raises(freebusy._TokenUnavailable) as exc:
        await freebusy._access_token(_FakeSession(), user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert exc.value.reconnect_required is True
    assert marks == [True]


_REAL_REFRESH = oauth.refresh_access_token


# ── 화면 조회는 토큰 행 잠금을 기다리지 않는다 (calendar-18) ──────────────────


class _LockedRowSession(_FakeSession):
    """flush(UPDATE) 가 lock_timeout 으로 떨어지는 세션 — 계획 생성이 같은 행을 쥔 상황."""

    def __init__(self) -> None:
        super().__init__()
        self.statements: list[str] = []

    async def flush(self) -> None:
        from sqlalchemy.exc import OperationalError

        self.calls.append("flush")

        class _LockNotAvailable(Exception):
            sqlstate = "55P03"

        raise OperationalError("UPDATE calendar_connections", {}, _LockNotAvailable())

    async def execute(self, statement: Any, params: Any = None) -> None:
        self.statements.append(str(statement))

    def begin_nested(self) -> Any:
        session = self

        class _Savepoint:
            async def __aenter__(self) -> None:
                session.statements.append("SAVEPOINT")

            async def __aexit__(self, *exc: object) -> bool:
                session.statements.append("ROLLBACK TO SAVEPOINT" if exc[0] else "RELEASE")
                return False

        return _Savepoint()


async def test_screen_refresh_does_not_wait_on_a_locked_token_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """계획 생성이 토큰 행을 UPDATE 한 채 LLM 을 기다리는 동안 오늘 화면이 같은 행을 쓰려 하면,
    예전엔 그 커밋까지(수십 초) 멈췄다. 화면은 짧게만 기다리고 저장을 건너뛴다 — 새 토큰은 쓴다.
    """
    _stub_expired_connection(monkeypatch, None)
    session = _LockedRowSession()

    token = await freebusy._access_token(
        session,  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        lock_timeout_ms=freebusy.SCREEN_TOKEN_LOCK_TIMEOUT_MS,
    )

    assert token == "fresh"
    assert session.statements[0] == "SAVEPOINT"
    assert "SET LOCAL lock_timeout = '1000ms'" in session.statements
    # 짧은 대기가 호출자의 뒤 쿼리에 새지 않게 되돌린다.
    assert session.statements[-1] == "SET LOCAL lock_timeout TO DEFAULT"


async def test_plan_path_still_waits_and_surfaces_lock_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """계획 경로는 예전 그대로다 — savepoint 도 lock_timeout 도 걸지 않는다."""
    from sqlalchemy.exc import OperationalError

    _stub_expired_connection(monkeypatch, None)
    session = _LockedRowSession()

    with pytest.raises(OperationalError):
        await freebusy._access_token(session, user_id=uuid.uuid4())  # type: ignore[arg-type]

    assert session.statements == []


async def test_screen_lookup_passes_the_short_lock_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def _fetch(session: Any, **kwargs: Any) -> freebusy.FreeBusyResult:
        seen.append(kwargs.get("lock_timeout_ms"))
        return freebusy.FreeBusyResult("ok", [])

    monkeypatch.setattr(freebusy, "fetch_busy", _fetch)
    start, end = _range()

    await freebusy.fetch_busy_for_screen(
        _FakeSession(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        start=start,
        end=end,
    )

    assert seen == [freebusy.SCREEN_TOKEN_LOCK_TIMEOUT_MS]


async def test_lock_timeout_savepoint_really_gives_up_on_postgres(
    real_db_session: Any,
) -> None:
    """실 Postgres 에서 `_write_connection` 이 잠금을 1초 안에 포기하고, 바깥 트랜잭션은 멀쩡하며,
    lock_timeout 이 뒤 쿼리에 새지 않는다. 다른 커넥션이 advisory lock 을 쥐게 해 행 잠금 대기를
    흉내낸다(lock_timeout 은 둘 다에 걸린다) — 커밋 없이 두 트랜잭션을 겹치는 가장 작은 방법.
    """
    import time as _time

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from reaction_backend.config import get_settings
    from reaction_backend.db.session import normalize_async_url

    key = 918_273_645
    engine = create_async_engine(
        normalize_async_url(get_settings().database_url), poolclass=NullPool
    )
    try:
        async with engine.connect() as holder:
            await holder.begin()
            await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})

            async def _blocked_write() -> None:
                await real_db_session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})

            began = _time.monotonic()
            await freebusy._write_connection(real_db_session, _blocked_write, lock_timeout_ms=300)
            elapsed = _time.monotonic() - began

            assert elapsed < 5, f"잠금을 포기하지 않고 기다렸다: {elapsed:.1f}s"
            # savepoint 만 롤백됐다 — 바깥 트랜잭션은 계속 쓸 수 있다.
            assert (await real_db_session.execute(text("SELECT 1"))).scalar_one() == 1
            timeout = (await real_db_session.execute(text("SHOW lock_timeout"))).scalar_one()
            assert timeout in ("0", "0ms"), f"짧은 lock_timeout 이 새어 나갔다: {timeout}"
            await holder.rollback()
    finally:
        await engine.dispose()
