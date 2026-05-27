"""Calendar — Google Calendar 연동 (S04, api-contract §9).

Issue #17 (Alpha MVP): **캘린더 OAuth 자체를 P1 로 미룬다 (PM 결정, 이슈 본문)**.
S04 는 skip 경로만 활성화 — FE 는 "수동 입력으로 시작" 권장. `connect`/`disconnect` 는 501.

freebusy / sync-preview / approve-insert 는 #18 First Plan 흐름에서 다시 다룬다.
현재는 mock 응답 유지 (#3-C).
"""

from datetime import datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query

from reaction_backend.api.mock.calendar import DEMO_FREEBUSY
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
from reaction_backend.schemas.errors import ApiError, ErrorCode

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.post("/connect")
async def connect_calendar(body: CalendarConnectRequest) -> CalendarConnection:
    """Google Calendar 연결 — **베타 이후 지원 (P1)**.

    Issue #17 MVP 결정: 캘린더 OAuth 는 P1 로 미룸. FE 는 S05 수동 입력으로 진행.
    """
    raise ApiError(
        ErrorCode.COMMON_NOT_IMPLEMENTED,
        "Google Calendar 연결은 베타 이후 지원돼요. 지금은 '수동 입력으로 시작'을 눌러주세요.",
        http_status=HTTPStatus.NOT_IMPLEMENTED,
    )


@router.delete("/connect")
async def disconnect_calendar() -> None:
    """Google Calendar 연결 해제 — **베타 이후 지원 (P1)**."""
    raise ApiError(
        ErrorCode.COMMON_NOT_IMPLEMENTED,
        "Google Calendar 연결 해제는 베타 이후 지원돼요.",
        http_status=HTTPStatus.NOT_IMPLEMENTED,
    )


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
