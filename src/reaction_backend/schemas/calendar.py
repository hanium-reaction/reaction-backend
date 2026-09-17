"""Calendar 도메인 스키마 (api-contract §9) — S04.

#3-C 단계는 mock 스텁. 실제 Google OAuth·freebusy 조회는 후속.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime

CalendarCheckStatus = Literal["ok", "failed", "not_connected"]


class CalendarConnectRequest(CamelModel):
    """POST /calendar/connect 요청 — Google OAuth authorization code."""

    code: str = Field(min_length=1)


class CalendarConnection(CamelModel):
    """캘린더 연결 상태 — POST /calendar/connect 응답."""

    provider: str
    connected: bool
    scopes: list[str]


class BusyInterval(CamelModel):
    """freebusy 의 busy 구간 한 개."""

    start: KstDatetime
    end: KstDatetime


class FreeBusy(CamelModel):
    """GET /calendar/freebusy 응답."""

    busy: list[BusyInterval]


class CalendarEventPreview(CamelModel):
    """sync-preview 의 캘린더 이벤트 후보."""

    title: str
    start: KstDatetime
    end: KstDatetime
    conflict: bool


class SyncPreview(CamelModel):
    """POST /calendar/sync-preview 응답."""

    events: list[CalendarEventPreview]
    conflict_count: int


class ApproveInsertResult(CamelModel):
    """POST /calendar/events/approve-insert 응답."""

    inserted_count: int


class CalendarCheck(CamelModel):
    """화면(오늘·주간) 응답에 실리는 캘린더 확인 결과.

    - `ok` — 읽었다. 겹치는 블록은 각 블록의 `calendarConflict` 로 표시된다.
    - `failed` — 연결돼 있는데 못 읽었다(Google 지연·오류). 겹침 표시가 **없다는 뜻이 아니다**.
    - `not_connected` — 연결 안 함(또는 서버에서 기능이 꺼짐). 아무 안내도 하지 않는다.

    `checkedAt` 은 실제로 Google 에서 읽은 시각이다 — 화면 조회는 5분 캐시라 최대 5분 전일 수
    있다. `ok` 가 아니면 null.
    """

    status: CalendarCheckStatus = "not_connected"
    checked_at: KstDatetime | None = None
