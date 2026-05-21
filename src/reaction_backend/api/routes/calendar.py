"""Calendar — Google Calendar 연동 (S04).

MVP 스코프: read-only freebusy. write-back은 P1.

규칙:
- OAuth 토큰은 at-rest 암호화 (access_token_encrypted/refresh_token_encrypted)
- events.insert는 Idempotency-Key 필수 (24h 중복 방지)
- 권한 박탈 감지 시 calendar_connections.revoked_at 기록 후 재연결 안내
- 토큰 만료 시 refresh, refresh 실패 시 revoked 처리

DB: calendar_connections (user_id UNIQUE), idempotency_keys

예정 endpoint:
- POST   /calendar/connect              — OAuth 코드 → 토큰 교환 + 저장
- DELETE /calendar/connect              — 연결 해제 (토큰 폐기)
- GET    /calendar/freebusy?from=&to=   — read-only freebusy 조회 (캐시)
- POST   /calendar/sync-preview         — 계획 → 캘린더 이벤트 미리보기 (충돌 체크)
- POST   /calendar/events/approve-insert — 사용자 승인된 이벤트 일괄 삽입 (Idempotency-Key)

구현 위치: integrations/google_oauth/ + integrations/google_calendar/
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.post("/connect", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def connect_calendar() -> None:
    """Google OAuth code 교환 + 암호화 저장."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §9 — to be implemented in a follow-up.",
    )
