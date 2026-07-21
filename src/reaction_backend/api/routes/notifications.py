"""Notifications — 알림 설정 + Web Push 구독 (S08, api-contract §15).

Issue #17 + #16(BE 측) 실구현:
- `/notifications/settings` GET/PATCH — 실 DB (`notification_settings` 테이블, user 당 1행)
- 첫 PATCH 시 onboarding_state 전이: NOTIFICATIONS → ACTIVE
- 시간 가드: morning 06~10시, evening 19~23시 (422 `NOTIF_TIME_RANGE`)
- `/notifications/subscribe` POST/DELETE — 구독 객체를 `push_subscription` JSONB 에 저장/해제.
  발송 자체(cron·게이트)는 scheduler 쪽 — 여기는 구독 저장만 책임진다.
"""

from __future__ import annotations

from datetime import time
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.session import get_db
from reaction_backend.repositories.notification_repo import (
    NotificationRepo,
    get_notification_repo,
)
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.notifications import (
    NotificationSettings,
    NotificationSettingsUpdateRequest,
    PushSubscribeRequest,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _to_schema(setting: NotificationSetting) -> NotificationSettings:
    return NotificationSettings(
        morning_brief_time=setting.morning_brief_time.strftime("%H:%M"),
        evening_reflection_time=setting.evening_reflection_time.strftime("%H:%M"),
        pre_card_enabled=setting.pre_card_enabled,
        push_subscribed=setting.push_subscription is not None,
    )


def _parse_hhmm(value: str, *, field: str) -> time:
    try:
        h, m = value.split(":", 1)
        return time(int(h), int(m))
    except (ValueError, TypeError) as e:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            f"{field} 형식이 올바르지 않아요 (HH:MM).",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field=field,
        ) from e


def _enforce_morning(t: time) -> None:
    if not 6 <= t.hour <= 10:
        raise ApiError(
            ErrorCode.NOTIF_TIME_RANGE,
            "모닝 브리프는 06~10시 사이로 설정할 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="morningBriefTime",
        )


def _enforce_evening(t: time) -> None:
    if not 19 <= t.hour <= 23:
        raise ApiError(
            ErrorCode.NOTIF_TIME_RANGE,
            "저녁 정리는 19~23시 사이로 설정할 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="eveningReflectionTime",
        )


RepoDep = Annotated[NotificationRepo, Depends(get_notification_repo)]
UserRepoDep = Annotated[UserRepo, Depends(get_user_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("/settings")
async def get_settings(
    user: CurrentUser, repo: RepoDep, session: SessionDep
) -> NotificationSettings:
    """내 알림 설정. 없으면 default 로 1행 자동 생성."""
    setting = await repo.get_or_create(user.id)
    await session.commit()
    return _to_schema(setting)


@router.patch("/settings")
async def update_settings(
    body: NotificationSettingsUpdateRequest,
    user: CurrentUser,
    repo: RepoDep,
    user_repo: UserRepoDep,
    session: SessionDep,
) -> NotificationSettings:
    """알림 시간 / 토글 수정. morning 06~10·evening 19~23 범위 검증.

    부수 효과: `ONBOARDING_NOTIFICATIONS` → `ACTIVE` 로 전이 (멱등).
    """
    morning = (
        _parse_hhmm(body.morning_brief_time, field="morningBriefTime")
        if body.morning_brief_time
        else None
    )
    evening = (
        _parse_hhmm(body.evening_reflection_time, field="eveningReflectionTime")
        if body.evening_reflection_time
        else None
    )
    if morning is not None:
        _enforce_morning(morning)
    if evening is not None:
        _enforce_evening(evening)

    setting = await repo.get_or_create(user.id)
    updated = await repo.update(
        setting,
        morning_brief_time=morning,
        evening_reflection_time=evening,
        pre_card_enabled=body.pre_card_enabled,
    )
    await user_repo.advance_onboarding(
        user,
        expected_from="ONBOARDING_NOTIFICATIONS",
        to="ACTIVE",
    )
    await session.commit()
    await session.refresh(updated)
    return _to_schema(updated)


# ───── Web Push subscription (Issue #16 BE 측) ─────


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe(
    body: PushSubscribeRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> NotificationSettings:
    """Web Push 구독 등록 — `push_subscription` JSONB 저장 (재구독은 덮어쓰기).

    저장 형태는 pywebpush 가 그대로 받는 `{endpoint, keys: {p256dh, auth}}`.
    응답은 실 설정 행 기준 — pushSubscribed 는 저장 결과에서 파생된다.
    """
    setting = await repo.get_or_create(user.id)
    updated = await repo.set_push_subscription(
        setting, {"endpoint": body.endpoint, "keys": body.keys}
    )
    await session.commit()
    await session.refresh(updated)
    return _to_schema(updated)


@router.delete("/subscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> None:
    """Web Push 구독 해제 — 멱등 (구독이 없어도 204)."""
    setting = await repo.get_by_user(user.id)
    if setting is not None and setting.push_subscription is not None:
        await repo.clear_push_subscription(setting)
        await session.commit()
    return None
