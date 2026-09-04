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

## 왜 캐시를 안 두나

`integrations/google_calendar/README.md` 는 60s TTL 캐시를 예고했지만 두지 않았다.
계획 생성은 **지평 전체를 한 번에** 조회하고(`fetch_busy_by_day`), 그 결과를 날짜별
dict 로 들고 스케줄러에 넘긴다 — `existing_busy` 와 같은 모양이다. 하루마다 부르는
구조가 아니라서 한 번의 generate 가 API 를 한 번만 친다. 캐시가 막을 반복 호출이
구조적으로 없다.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Literal

import requests
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.integrations.google_calendar import oauth, token_store
from reaction_backend.orchestrator.goal_structuring import BusyBlock, TimeInterval
from reaction_backend.schemas.common import KST, to_kst

logger = logging.getLogger(__name__)

_FREEBUSY_URL: Final = "https://www.googleapis.com/calendar/v3/freeBusy"

# 계획 생성 안에서 도는 왕복이라 짧게 — LLM 분해가 이미 45초를 쓴다(#179).
_CONNECT_TIMEOUT: Final = 3.0
_READ_TIMEOUT: Final = 5.0
_HARD_TIMEOUT: Final = 10.0

#: 조회 상한 — Google 이 한 번에 돌려주는 범위. 4주 계획 지평보다 넉넉하다.
MAX_RANGE_DAYS: Final = 60

Status = Literal["ok", "not_connected", "failed"]


@dataclass(frozen=True)
class FreeBusyResult:
    """`status` 로 세 경우를 가른다 — 자세한 이유는 모듈 독스트링 참조."""

    status: Status
    intervals: list[TimeInterval]

    @property
    def connected_but_failed(self) -> bool:
        """연결돼 있는데 못 읽은 경우 — 호출자가 사용자에게 알려야 한다."""
        return self.status == "failed"


def _query(access_token: str, start: datetime, end: datetime) -> requests.Response:
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
        timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
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


async def _access_token(session: AsyncSession, *, user_id: uuid.UUID) -> str | None:
    """살아 있는 access token. 만료가 임박했으면 갱신하고 저장한다.

    갱신이 `invalid_grant` 로 실패하면 사용자가 Google 에서 권한을 뺏은 것이다 —
    `revoked_at` 을 찍어 다음 진입에 재연결을 안내한다. 일시적 실패(네트워크·5xx)는
    연결을 끊지 않는다. 그 구분이 `OAuthError.retryable` 이다.
    """
    connection = await token_store.get_active(session, user_id=user_id)
    if connection is None:
        return None

    if connection.expires_at - oauth.REFRESH_SKEW > datetime.now(UTC):
        return token_store.access_token_of(connection)

    try:
        bundle = await oauth.refresh_access_token(
            token_store.refresh_token_of(connection), known_scopes=connection.scopes
        )
    except oauth.OAuthError as exc:
        logger.info(
            "calendar_refresh_failed",
            extra={"reason": exc.reason, "retryable": exc.retryable},
        )
        if not exc.retryable:
            await token_store.mark_revoked(session, connection)
            await session.commit()
        return None

    await token_store.save(session, user_id=user_id, bundle=bundle)
    await session.commit()
    return bundle.access_token


async def fetch_busy(
    session: AsyncSession, *, user_id: uuid.UUID, start: datetime, end: datetime
) -> FreeBusyResult:
    """[start, end) 의 busy 구간. 실패해도 예외를 올리지 않는다."""
    access_token = await _access_token(session, user_id=user_id)
    if access_token is None:
        # 연결이 없거나, 갱신이 실패해 방금 회수됐다. 둘 다 '캘린더 없이 진행'이다.
        return FreeBusyResult("not_connected", [])

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_query, access_token, start, end), timeout=_HARD_TIMEOUT
        )
    except (TimeoutError, requests.RequestException) as exc:
        logger.info("calendar_freebusy_failed", extra={"reason": type(exc).__name__})
        return FreeBusyResult("failed", [])

    if response.status_code != 200:
        logger.info("calendar_freebusy_failed", extra={"reason": f"http_{response.status_code}"})
        return FreeBusyResult("failed", [])

    try:
        return FreeBusyResult("ok", _parse(response.json()))
    except (ValueError, KeyError, TypeError) as exc:
        logger.info("calendar_freebusy_unparsable", extra={"reason": str(exc)[:120]})
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
) -> tuple[dict[date, list[BusyBlock]], Status]:
    """계획 생성이 쓰는 진입점 — 지평 전체를 **한 번에** 조회해 날짜별 busy 로.

    `_existing_busy_by_day` 와 같은 모양이라 `busy_for_day` 에 그대로 얹힌다.
    """
    span_days = (end_day - start_day).days
    if span_days > MAX_RANGE_DAYS:
        end_day = start_day + timedelta(days=MAX_RANGE_DAYS)
    start = datetime.combine(start_day, time(0, 0), tzinfo=KST)
    end = datetime.combine(end_day + timedelta(days=1), time(0, 0), tzinfo=KST)

    result = await fetch_busy(session, user_id=user_id, start=start, end=end)
    return split_by_day(result.intervals), result.status
