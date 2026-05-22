"""Notifications — 알림 설정 + Web Push 구독 (S08, api-contract §15).

#3-C 단계는 **mock 스텁**: demo 알림 설정을 반환한다.
실제 푸시 발송·스케줄러·주 3건 예산 enforce·야간 차단은 후속 (scheduler).
시간 가드: 모닝 브리프 06~10시, 저녁 정리 19~23시.
"""

from fastapi import APIRouter, status

from reaction_backend.api.mock.notifications import DEMO_NOTIFICATION_SETTINGS
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.notifications import (
    NotificationSettings,
    NotificationSettingsUpdateRequest,
    PushSubscribeRequest,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _demo_settings(*, push_subscribed: bool | None = None) -> NotificationSettings:
    demo = DEMO_NOTIFICATION_SETTINGS
    return NotificationSettings(
        morning_brief_time=demo.morning_brief_time,
        evening_reflection_time=demo.evening_reflection_time,
        pre_card_enabled=demo.pre_card_enabled,
        push_subscribed=demo.push_subscribed if push_subscribed is None else push_subscribed,
    )


@router.get("/settings")
async def get_settings() -> NotificationSettings:
    """[stub] 내 알림 설정."""
    return _demo_settings()


@router.patch("/settings")
async def update_settings(body: NotificationSettingsUpdateRequest) -> NotificationSettings:
    """[stub] 알림 시간·토글 수정. 모닝 06~10시·저녁 19~23시 범위 검증."""
    demo = DEMO_NOTIFICATION_SETTINGS
    morning = body.morning_brief_time or demo.morning_brief_time
    evening = body.evening_reflection_time or demo.evening_reflection_time
    if not 6 <= int(morning[:2]) <= 10:
        raise ApiError(
            ErrorCode.NOTIF_TIME_RANGE,
            "모닝 브리프는 06~10시 사이로 설정할 수 있어요.",
            http_status=422,
            field="morningBriefTime",
        )
    if not 19 <= int(evening[:2]) <= 23:
        raise ApiError(
            ErrorCode.NOTIF_TIME_RANGE,
            "저녁 정리는 19~23시 사이로 설정할 수 있어요.",
            http_status=422,
            field="eveningReflectionTime",
        )
    return NotificationSettings(
        morning_brief_time=morning,
        evening_reflection_time=evening,
        pre_card_enabled=(
            body.pre_card_enabled if body.pre_card_enabled is not None else demo.pre_card_enabled
        ),
        push_subscribed=demo.push_subscribed,
    )


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe(body: PushSubscribeRequest) -> NotificationSettings:
    """[stub] Web Push 구독 등록."""
    return _demo_settings(push_subscribed=True)


@router.delete("/subscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe() -> None:
    """[stub] Web Push 구독 해제."""
    return None
