"""Calendar 도메인 스키마 (api-contract §9) — S04.

#3-C 단계는 mock 스텁. 실제 Google OAuth·freebusy 조회는 후속.
"""

from __future__ import annotations

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, KstDatetime


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
