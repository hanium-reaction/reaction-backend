"""User — 사용자 본체. 화면 S01 Welcome 진입점.

핵심 규칙 (DB 시나리오 분석):
- `onboarding_state` 가 앱 전체 라우팅의 single source of truth
- `last_active_at` 은 90일 비활성 자동 익명화 cron (매일 04:00 KST) 의 기준
- `tone_mode` 는 S03 Analysis Confirm 에서 인터뷰 결과 기반으로 set
- soft delete 는 `archived_at` 외에 `anonymized_at` 도 사용 (익명화는 PII 마스킹)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.behavioral_profile import BehavioralProfile
    from reaction_backend.db.models.calendar_connection import CalendarConnection
    from reaction_backend.db.models.fixed_schedule import FixedSchedule
    from reaction_backend.db.models.interaction_style import InteractionStyle
    from reaction_backend.db.models.interview_session import InterviewSession
    from reaction_backend.db.models.notification_setting import NotificationSetting


ONBOARDING_STATE_VALUES = (
    "WELCOME",
    "ONBOARDING_INTERVIEW",
    "ONBOARDING_CONFIRM",
    "ONBOARDING_CALENDAR",
    "ONBOARDING_MANUAL_SCHEDULE",
    "ONBOARDING_POLICIES",
    "ONBOARDING_FIRST_PLAN",
    "ONBOARDING_NOTIFICATIONS",
    "ACTIVE",
)

TONE_MODE_VALUES = ("gentle", "strict", "encouraging")


class User(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # 인증 — Google OAuth email 이 사용자 식별의 1차 키
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)

    # Google OAuth 사용자 표시 이름 (DB 설계서 v0.7.1 §5.1)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # 라우팅 / 상태머신
    onboarding_state: Mapped[str] = mapped_column(
        Enum(*ONBOARDING_STATE_VALUES, name="user_onboarding_state"),
        nullable=False,
        server_default="WELCOME",
    )

    # 톤 (S03 인터뷰 결과로 set)
    tone_mode: Mapped[str | None] = mapped_column(
        Enum(*TONE_MODE_VALUES, name="user_tone_mode"),
        nullable=True,
    )

    is_beta: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # 익명화 완료 플래그 (cron 의 명시적 처리 표시) — DB 설계서 v0.7.1 §5.1
    is_anonymized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # IANA timezone (예: Asia/Seoul). 응답 시간 변환 / cron 기준.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="Asia/Seoul")

    # 90일 비활성 익명화 cron의 기준
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # 익명화 처리된 시각 (PII 마스킹). 통계 행은 보존.
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 포커스 모드 선호 (S11 카드 상세 / S12 Focus Entry 자동 켜기 룰)
    # by_action_pattern / by_category / default 순으로 매칭
    focus_mode_preferences: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ── relationships ──
    interview_sessions: Mapped[list[InterviewSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    behavioral_profile: Mapped[BehavioralProfile | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    interaction_style: Mapped[InteractionStyle | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    notification_setting: Mapped[NotificationSetting | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    calendar_connection: Mapped[CalendarConnection | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    fixed_schedules: Mapped[list[FixedSchedule]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
