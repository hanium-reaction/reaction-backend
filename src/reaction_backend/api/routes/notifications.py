"""Notifications — S08 + 알림 예산 enforce.

3 알림 클래스 (DevBaseline 잠금):
- morning_brief (기본 08:00, 06~10시 제한)
- evening_reflection (기본 21:00, 19~23시 제한)
- pre_card (카드 시작 2분 전, 옵트인)

규칙:
- 주 ≤ 3건 (Notification Tool 레이어가 enforce)
- 23~07시 자동 푸시 금지 (시간대 가드)
- 같은 클래스 24h 내 중복 발송 X
- push_subscription은 Web Push 표준 객체 그대로 저장

DB: notification_settings (user_id UNIQUE)

예정 endpoint:
- GET   /notifications/settings           — 내 알림 설정
- PATCH /notifications/settings           — 시간/토글 수정
- POST  /notifications/subscribe          — push subscription 등록
- DELETE /notifications/subscribe         — 구독 해제

구현 위치: integrations/web_push/ + scheduler/notification_dispatcher.py
"""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/settings", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_notification_settings() -> None:
    """내 알림 설정."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Defined in api-contract.md §15 — to be implemented in a follow-up.",
    )
