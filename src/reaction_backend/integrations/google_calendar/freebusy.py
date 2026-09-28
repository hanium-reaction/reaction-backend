"""Google Calendar freebusy 조회 — 계획이 피해야 할 '남의 일정' (ADR-0009 D4).

돌려주는 건 **구간뿐**이다. 제목도 장소도 참석자도 오지 않는다 — `calendar.freebusy`
스코프가 그것만 주고, 스케줄러의 룰에 필요한 것도 그것뿐이다.

## 실패는 정상 경로다

캘린더를 못 읽었다고 **계획 생성이 실패하면 안 된다.** 연결이 없거나·토큰이 죽었거나·
Google 이 느리면 `None` 을 돌려주고, 호출자는 캘린더 없이 예전처럼 진행한다
(`web_fetch` 와 같은 관례).

다만 **연결한 사용자에게는 조용히 실패하면 안 된다.** "연결했는데 수업 위에 계획이
잡혔다" 는 사용자가 알아챌 수 없는 배신이다. 그래서 반환을 세 가지로 가른다 —
`not_connected`(정상, 조용히) / `ok` / `failed`(연결돼 있는데 못 읽음 → 호출자가 경고).

그리고 `not_connected` 중 **Google 쪽에서 끊긴 연결**(갱신이 `invalid_grant`)은
`reconnect_required` 로 따로 표시한다. 예전엔 이게 그냥 `not_connected` 였다 — 권한이
철회되거나 테스트 모드 refresh token 이 7일 만에 만료되면 그날부터 계획이 수업 위에
잡히는데 어떤 화면도 말하지 않았다. 토큰 갱신이 **일시적으로** 실패한 것(네트워크·5xx)도
예전엔 `not_connected` 였다 — 이제 `failed` 다(연결은 그대로 두고 경고).

## 캐시는 화면 조회에만

계획 생성은 **지평 전체를 한 번에** 조회하고(`fetch_busy_by_day`) 그 결과를 날짜별 dict 로
스케줄러에 넘긴다 — 한 번의 generate 가 API 를 한 번만 치므로 캐시가 막을 반복 호출이 없다.

화면(`GET /today/agenda`·`GET /plans/weekly`)은 다르다 — 이미 승인한 블록이 **나중에 생긴**
캘린더 일정과 겹치는지 보려고 화면을 열 때마다 읽는데, 화면 전환마다 Google 을 치면
느리고 낭비다. 그래서 `fetch_busy_for_screen` 만 사용자·구간별 5분 캐시와 2초 상한을 둔다.
프로세스 메모리 캐시라 워커마다 따로다(단일 인스턴스 배포 전제, scheduler 와 같다).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Literal

import requests
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.domain import calendar_conflict
from reaction_backend.integrations.google_calendar import oauth, token_store
from reaction_backend.orchestrator.goal_structuring import BusyBlock, TimeInterval
from reaction_backend.safety.encryption import EncryptionError
from reaction_backend.schemas.common import KST, to_kst

logger = logging.getLogger(__name__)

_FREEBUSY_URL: Final = "https://www.googleapis.com/calendar/v3/freeBusy"

# 계획 생성 안에서 도는 왕복이라 짧게 — LLM 분해가 이미 45초를 쓴다(#179).
_CONNECT_TIMEOUT: Final = 3.0
_READ_TIMEOUT: Final = 5.0
_HARD_TIMEOUT: Final = 10.0

#: 조회 상한 — Google 이 한 번에 돌려주는 범위. 4주 계획 지평보다 넉넉하다.
MAX_RANGE_DAYS: Final = 60

#: 화면 조회는 사용자가 기다린다 — 계획 생성(10s)보다 훨씬 짧게. 넘으면 캘린더 없이 그린다.
SCREEN_HARD_TIMEOUT: Final = 2.0
#: 화면 조회 캐시 수명 — 캘린더에 새 약속을 넣고 5분 안에는 화면에 반영된다.
SCREEN_CACHE_TTL: Final = timedelta(minutes=5)
#: 화면 조회가 토큰 행 잠금을 기다리는 상한(ms) — `_write_connection` 참조.
SCREEN_TOKEN_LOCK_TIMEOUT_MS: Final = 1000

Status = Literal["ok", "not_connected", "failed"]
#: 계획 생성·재계획이 받는 상태 — 화면용 `Status` 에 "Google 쪽에서 끊겼다" 가 하나 더 있다.
#: 화면(`CalendarCheck`)의 enum 은 그대로 두려고 계획 경로에만 드러낸다.
PlanCalendarStatus = Literal["ok", "not_connected", "failed", "reconnect_required"]

#: Google 쪽에서 끊긴 연결 — 첫 계획·재계획이 같은 문구를 쓴다(`CALENDAR_FAILED_WARNING` 짝).
#: 사용자가 뭘 잘못한 게 아니다(권한 철회·만료) — 탓하지 않고 다시 잇는 길만 알려준다.
CALENDAR_RECONNECT_WARNING: Final = (
    "Google 캘린더 연결이 끊겨서 이번 계획에는 캘린더 일정을 반영하지 못했어요. "
    "설정에서 다시 연결하면 다음 계획부터 반영돼요."
)


@dataclass(frozen=True)
class FreeBusyResult:
    """`status` 로 세 경우를 가른다 — 자세한 이유는 모듈 독스트링 참조."""

    status: Status
    intervals: list[TimeInterval]
    #: `not_connected` 인데 그게 **Google 쪽에서 끊겨서**다 — 사용자에게 재연결을 안내할 상태.
    reconnect_required: bool = False

    @property
    def connected_but_failed(self) -> bool:
        """연결돼 있는데 못 읽은 경우 — 호출자가 사용자에게 알려야 한다."""
        return self.status == "failed"


class _TokenUnavailable(Exception):
    """연결은 있었는데 access token 을 못 얻었다.

    `reconnect_required` 면 Google 이 권한이 사라졌다고 했다(`invalid_grant`, 방금 회수했거나
    이미 그렇게 회수된 연결). 아니면 일시적이거나 서버 쪽 문제다(갱신 왕복 실패·복호화 실패) —
    연결은 그대로 두고 이번 조회만 `failed` 다.
    """

    def __init__(self, *, reconnect_required: bool) -> None:
        super().__init__("reconnect_required" if reconnect_required else "unavailable")
        self.reconnect_required = reconnect_required


def _query(
    access_token: str,
    start: datetime,
    end: datetime,
    timeout: tuple[float, float] = (_CONNECT_TIMEOUT, _READ_TIMEOUT),
) -> requests.Response:
    return requests.post(
        _FREEBUSY_URL,
        json={
            "timeMin": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "timeMax": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            # primary 만 본다. 구독한 공휴일·남의 공유 캘린더까지 busy 로 잡으면
            # 하루가 통째로 사라진다.
            "items": [{"id": "primary"}],
        },
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )


def _parse(payload: dict[str, Any]) -> list[TimeInterval]:
    """`calendars.primary.busy[]` → KST TimeInterval.

    `errors` 가 있으면 그 캘린더는 못 읽은 것이다 — 빈 목록으로 두면 "일정 없음" 과
    구분되지 않으므로 호출자가 실패로 보게 예외를 올린다.
    """
    calendars = payload.get("calendars")
    if not isinstance(calendars, dict):
        raise ValueError("no calendars")
    primary = calendars.get("primary")
    if not isinstance(primary, dict):
        raise ValueError("no primary")
    if primary.get("errors"):
        raise ValueError(str(primary["errors"])[:120])

    intervals: list[TimeInterval] = []
    for entry in primary.get("busy") or []:
        if not isinstance(entry, dict):
            continue
        raw_start, raw_end = entry.get("start"), entry.get("end")
        if not isinstance(raw_start, str) or not isinstance(raw_end, str):
            continue
        start = to_kst(datetime.fromisoformat(raw_start.replace("Z", "+00:00")))
        end = to_kst(datetime.fromisoformat(raw_end.replace("Z", "+00:00")))
        if end > start:
            intervals.append(TimeInterval(start, end))
    return intervals


def _is_lock_timeout(exc: DBAPIError) -> bool:
    """lock_timeout(SQLSTATE 55P03) 인가 — `orchestrator._common._is_lock_timeout` 과 같은 판별.

    integrations 가 orchestrator 를 import 하면 방향이 거꾸로라(AGENTS §5) 세 줄을 따로 둔다.
    """
    origin = exc.orig if exc.orig is not None else exc
    text_ = f"{type(origin).__name__} {origin} {getattr(origin, 'sqlstate', '')}".lower()
    return "55p03" in text_ or "lock timeout" in text_ or "locknotavailable" in text_


async def _write_connection(
    session: AsyncSession,
    write: Callable[[], Awaitable[object]],
    *,
    lock_timeout_ms: int | None,
) -> None:
    """`calendar_connections` 행 쓰기(flush 까지). `lock_timeout_ms` 가 있으면 잠긴 행을 오래
    기다리지 않고 **쓰기만 건너뛴다.**

    계획 생성은 토큰을 갱신해 이 행을 UPDATE 한 채(미커밋 — `_access_token` ⚠️) LLM 검토까지
    수십 초짜리 트랜잭션을 연다. 그동안 오늘·주간 화면이 같은 행을 갱신하려 들면 그 커밋까지
    멈춰 스켈레톤만 돈다 — freebusy 의 2초 상한은 HTTP 왕복에만 걸려 있어 이 대기를 못 막는다.
    그래서 화면은 savepoint 안에서 짧은 `lock_timeout` 으로 쓰고, 잠겨 있으면 그 쓰기만 버린다.
    갱신한 토큰은 이번 요청에 그대로 쓰고, 저장은 다음 조회(그때는 풀린 행)가 다시 한다.
    """
    if lock_timeout_ms is None:
        await write()
        return
    try:
        async with session.begin_nested():
            # SET LOCAL 은 savepoint 가 롤백되면 같이 되돌아가지만, 성공하면 트랜잭션 끝까지
            # 남는다 — 아래 finally 에서 기본값으로 되돌려 호출자의 뒤 쿼리에 새지 않게 한다.
            await session.execute(text(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}ms'"))
            await write()
    except DBAPIError as exc:
        if not _is_lock_timeout(exc):
            raise
        logger.info("calendar_token_write_skipped reason=row_locked")
    finally:
        await session.execute(text("SET LOCAL lock_timeout TO DEFAULT"))


async def _access_token(
    session: AsyncSession, *, user_id: uuid.UUID, lock_timeout_ms: int | None = None
) -> str | None:
    """살아 있는 access token. 만료가 임박했으면 갱신하고 저장한다. 연결이 없으면 None.

    못 얻으면 `_TokenUnavailable` 이다. 갱신이 `invalid_grant` 로 실패하면 Google 쪽에서
    권한이 사라진 것이다(사용자가 계정에서 앱 권한을 뺐거나 refresh token 만료) — 연결을
    회수하면서 "Google 쪽에서 끊김" 표식을 남기고(`token_store.mark_revoked(by_google=True)`),
    그 뒤로도 사용자가 다시 연결하거나 해제할 때까지 `reconnect_required` 로 알린다.
    일시적 실패(네트워크·5xx·서버 설정)는 연결을 끊지 않는다. 그 구분이 `OAuthError.retryable`
    이다. 저장된 토큰을 복호화하지 못해도(키 교체 등) 500 이 아니라 일시적 실패로 본다.

    ⚠️ **commit 하지 않는다 — flush 까지만.** 계획 생성·재계획은 트랜잭션 단위 advisory
    lock(`user_agent_lock`, `pg_advisory_xact_lock`) 안에서 이걸 부른다. 여기서 commit 하면
    그 lock 이 **계획 생성 도중에 풀린다**(토큰이 만료되는 한 시간마다 한 번). 갱신한 토큰·
    회수 표시는 호출자의 마지막 commit 에 실린다. 호출자가 rollback 하면 사라지지만 다음
    조회가 같은 판단을 다시 하므로 잃는 게 없다.

    `lock_timeout_ms` 는 화면 경로용이다 — `_write_connection` 참조.
    """
    connection = await token_store.get_active(session, user_id=user_id)
    if connection is None:
        if await token_store.needs_reconnect(session, user_id=user_id):
            raise _TokenUnavailable(reconnect_required=True)
        return None

    try:
        if connection.expires_at - oauth.REFRESH_SKEW > datetime.now(UTC):
            return token_store.access_token_of(connection)
        refresh_token = token_store.refresh_token_of(connection)
    except EncryptionError as exc:
        logger.warning("calendar_token_unreadable reason=%s", type(exc).__name__)
        raise _TokenUnavailable(reconnect_required=False) from exc

    try:
        bundle = await oauth.refresh_access_token(refresh_token, known_scopes=connection.scopes)
    except oauth.OAuthError as exc:
        logger.info("calendar_refresh_failed reason=%s retryable=%s", exc.reason, exc.retryable)
        if exc.retryable:
            raise _TokenUnavailable(reconnect_required=False) from exc

        async def _revoke() -> None:
            await token_store.mark_revoked(session, connection, by_google=True)
            await session.flush()

        await _write_connection(session, _revoke, lock_timeout_ms=lock_timeout_ms)
        raise _TokenUnavailable(reconnect_required=True) from exc

    async def _save() -> None:
        await token_store.save(session, user_id=user_id, bundle=bundle)  # save 가 flush 한다

    await _write_connection(session, _save, lock_timeout_ms=lock_timeout_ms)
    return bundle.access_token


async def fetch_busy(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    start: datetime,
    end: datetime,
    hard_timeout: float = _HARD_TIMEOUT,
    lock_timeout_ms: int | None = None,
) -> FreeBusyResult:
    """[start, end) 의 busy 구간. 실패해도 예외를 올리지 않는다. commit 은 호출자 몫.

    `hard_timeout` 은 Google freebusy 왕복의 상한이다(토큰 갱신 왕복은 따로 `oauth` 의 상한).
    `lock_timeout_ms` 는 토큰 행 잠금 대기 상한 — 화면 경로만 준다(`_write_connection`).

    기능 스위치가 꺼져 있으면 **읽지 않는다**(`not_connected`). 계획·재계획·화면·브리프·
    조회 라우트가 전부 여기를 지나므로 한 곳에서 막는다 — 예전엔 화면·브리프만 스위치를 봐서,
    운영이 기능을 끈 동안에도 계획 생성은 사용자의 캘린더를 계속 읽었다.
    """
    if not oauth.is_enabled():
        return FreeBusyResult("not_connected", [])

    try:
        access_token = await _access_token(
            session, user_id=user_id, lock_timeout_ms=lock_timeout_ms
        )
    except _TokenUnavailable as exc:
        if exc.reconnect_required:
            return FreeBusyResult("not_connected", [], reconnect_required=True)
        return FreeBusyResult("failed", [])
    if access_token is None:
        # 연결한 적이 없거나 앱에서 해제했다 — '캘린더 없이 조용히 진행'이다.
        return FreeBusyResult("not_connected", [])

    # 스레드 쪽 timeout 도 상한 안으로 줄인다 — 코루틴만 포기하고 스레드가 8초씩 남으면
    # 화면 조회가 몰릴 때 스레드 풀이 막힌다.
    http_timeout = (min(_CONNECT_TIMEOUT, hard_timeout), min(_READ_TIMEOUT, hard_timeout))
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_query, access_token, start, end, http_timeout),
            timeout=hard_timeout,
        )
    except (TimeoutError, requests.RequestException) as exc:
        logger.info("calendar_freebusy_failed reason=%s", type(exc).__name__)
        return FreeBusyResult("failed", [])

    if response.status_code != 200:
        logger.info("calendar_freebusy_failed reason=http_%s", response.status_code)
        return FreeBusyResult("failed", [])

    try:
        return FreeBusyResult("ok", _parse(response.json()))
    except (ValueError, KeyError, TypeError) as exc:
        logger.info("calendar_freebusy_unparsable reason=%s", str(exc)[:120])
        return FreeBusyResult("failed", [])


def split_by_day(intervals: list[TimeInterval]) -> dict[date, list[BusyBlock]]:
    """구간을 KST 날짜별로 쪼갠다 — 스케줄러가 날짜 단위로 free 를 계산하기 때문.

    자정을 넘는 일정(23:00~01:00)은 **두 날짜로 잘라야** 한다. 시작일에만 달면 다음 날
    새벽이 비어 있는 것으로 보여 그 위에 카드가 잡힌다.
    """
    by_day: dict[date, list[BusyBlock]] = defaultdict(list)
    for interval in intervals:
        cursor = interval.start
        while cursor < interval.end:
            day = cursor.date()
            midnight = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=KST)
            piece_end = min(interval.end, midnight)
            by_day[day].append(
                BusyBlock(TimeInterval(cursor, piece_end), "calendar", "캘린더 일정")
            )
            cursor = piece_end
    return dict(by_day)


async def fetch_busy_by_day(
    session: AsyncSession, *, user_id: uuid.UUID, start_day: date, end_day: date
) -> tuple[dict[date, list[BusyBlock]], PlanCalendarStatus]:
    """계획 생성이 쓰는 진입점 — 지평 전체를 **한 번에** 조회해 날짜별 busy 로.

    `_existing_busy_by_day` 와 같은 모양이라 `busy_for_day` 에 그대로 얹힌다.
    상태가 `reconnect_required` 면 호출자는 `CALENDAR_RECONNECT_WARNING` 을 싣는다.
    """
    span_days = (end_day - start_day).days
    if span_days > MAX_RANGE_DAYS:
        end_day = start_day + timedelta(days=MAX_RANGE_DAYS)
    start = datetime.combine(start_day, time(0, 0), tzinfo=KST)
    end = datetime.combine(end_day + timedelta(days=1), time(0, 0), tzinfo=KST)

    result = await fetch_busy(session, user_id=user_id, start=start, end=end)
    status: PlanCalendarStatus = (
        "reconnect_required" if result.reconnect_required else result.status
    )
    return split_by_day(result.intervals), status


# ── 화면 조회 (오늘·주간) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class _ScreenCacheEntry:
    result: FreeBusyResult
    checked_at: datetime


_screen_cache: dict[tuple[uuid.UUID, datetime, datetime], _ScreenCacheEntry] = {}


def clear_screen_cache(user_id: uuid.UUID | None = None) -> None:
    """화면 캐시 비우기 — 연결·해제 직후 호출한다(안 비우면 5분간 옛 상태를 그린다)."""
    if user_id is None:
        _screen_cache.clear()
        return
    for key in [k for k in _screen_cache if k[0] == user_id]:
        del _screen_cache[key]


async def fetch_busy_for_screen(
    session: AsyncSession, *, user_id: uuid.UUID, start: datetime, end: datetime
) -> tuple[FreeBusyResult, datetime]:
    """화면용 조회 — 5분 캐시 + 2초 상한. `(결과, 확인 시각)` 을 돌려준다.

    **실패는 캐시하지 않는다** — Google 이 잠깐 느렸던 것 때문에 5분 동안 캘린더 없는
    화면을 보여줄 이유가 없다. 다음 화면 진입에서 다시 시도한다.

    토큰 행이 계획 생성 트랜잭션에 잠겨 있으면 기다리지 않는다(`SCREEN_TOKEN_LOCK_TIMEOUT_MS`).
    """
    now = datetime.now(UTC)
    key = (user_id, start, end)
    hit = _screen_cache.get(key)
    if hit is not None and now - hit.checked_at < SCREEN_CACHE_TTL:
        return hit.result, hit.checked_at

    result = await fetch_busy(
        session,
        user_id=user_id,
        start=start,
        end=end,
        hard_timeout=SCREEN_HARD_TIMEOUT,
        lock_timeout_ms=SCREEN_TOKEN_LOCK_TIMEOUT_MS,
    )
    if result.status != "failed":
        # 만료된 항목도 같이 치운다 — 주간 화면을 여러 주 넘겨 보면 키가 계속 쌓인다.
        for stale in [
            k for k, v in _screen_cache.items() if now - v.checked_at >= SCREEN_CACHE_TTL
        ]:
            del _screen_cache[stale]
        _screen_cache[key] = _ScreenCacheEntry(result, now)
    return result, now


@dataclass(frozen=True)
class ScreenConflicts[K]:
    """화면에 실을 캘린더 상태 + 겹치는 블록 key + 읽은 busy 구간(`ok` 일 때만).

    `intervals` 는 주간 그리드가 블록과 안 겹치는 약속까지 그리는 데 쓴다 — 겹침 판정에 이미
    읽은 구간이라 Google 을 한 번 더 부르지 않는다.
    """

    status: Status
    checked_at: datetime | None
    keys: set[K]
    intervals: list[TimeInterval] = field(default_factory=list)


async def screen_conflicts[K](
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    start: datetime,
    end: datetime,
    blocks: list[tuple[K, str, datetime, datetime]],
    now: datetime,
) -> ScreenConflicts[K]:
    """화면 구간의 캘린더 일정과 겹치는 블록. 기능이 꺼져 있으면 Google 을 부르지 않는다."""
    if not oauth.is_enabled():
        return ScreenConflicts("not_connected", None, set())
    result, checked_at = await fetch_busy_for_screen(session, user_id=user_id, start=start, end=end)
    if result.status != "ok":
        return ScreenConflicts(result.status, None, set())
    keys = calendar_conflict.conflicting_keys(
        blocks, [(iv.start, iv.end) for iv in result.intervals], now=now
    )
    return ScreenConflicts("ok", checked_at, keys, list(result.intervals))
