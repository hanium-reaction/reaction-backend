"""Calendar — Google Calendar 연동 (S04, api-contract §9).

#3-C 단계는 **mock 스텁**: 고정 freebusy·연결 상태를 반환한다.
실제 OAuth 토큰 교환·freebusy 조회·events.insert 는 후속 (integrations/google_calendar).
MVP 스코프는 read-only freebusy.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status

from reaction_backend.api.mock.calendar import DEMO_CALENDAR_PROVIDER, DEMO_FREEBUSY
from reaction_backend.schemas.calendar import (
    ApproveInsertResult,
    BusyInterval,
    CalendarConnection,
    CalendarConnectRequest,
    CalendarEventPreview,
    FreeBusy,
    SyncPreview,
)
from reaction_backend.schemas.common import KST

router = APIRouter(prefix="/calendar", tags=["calendar"])

_SCOPES = ["calendar.readonly", "calendar.events"]


@router.post("/connect", status_code=status.HTTP_201_CREATED)
async def connect_calendar(body: CalendarConnectRequest) -> CalendarConnection:
    """[stub] OAuth code → 캘린더 연결. 실제 토큰 교환·암호화 저장은 후속."""
    return CalendarConnection(provider=DEMO_CALENDAR_PROVIDER, connected=True, scopes=_SCOPES)


@router.delete("/connect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_calendar() -> None:
    """[stub] 캘린더 연결 해제 (토큰 폐기)."""
    return None


@router.get("/freebusy")
async def get_freebusy(
    from_: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query()],
) -> FreeBusy:
    """[stub] read-only freebusy 조회. from·to 는 조회 범위 (스텁은 고정 구간 반환)."""
    return FreeBusy(busy=[BusyInterval(start=iv.start, end=iv.end) for iv in DEMO_FREEBUSY])


@router.post("/sync-preview")
async def sync_preview() -> SyncPreview:
    """[stub] 계획 → 캘린더 이벤트 미리보기 + 충돌 체크."""
    events = [
        CalendarEventPreview(
            title="캡스톤 설계",
            start=datetime(2026, 5, 26, 10, 0, tzinfo=KST),
            end=datetime(2026, 5, 26, 12, 0, tzinfo=KST),
            conflict=False,
        ),
        CalendarEventPreview(
            title="토익 공부",
            start=datetime(2026, 5, 26, 14, 0, tzinfo=KST),
            end=datetime(2026, 5, 26, 15, 0, tzinfo=KST),
            conflict=True,
        ),
    ]
    return SyncPreview(events=events, conflict_count=1)


@router.post("/events/approve-insert")
async def approve_insert() -> ApproveInsertResult:
    """[stub] 사용자 승인 이벤트 일괄 삽입. Idempotency-Key 필수 (미들웨어가 강제)."""
    return ApproveInsertResult(inserted_count=2)
