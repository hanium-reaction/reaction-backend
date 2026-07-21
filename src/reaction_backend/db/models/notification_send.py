"""NotificationSend — Web Push 발송 이력 (INSERT only). Issue #20 알림 cron.

발송 **게이트**(`safety/push_gate.py`)의 상태 저장소다. 잠금 규칙 세 가지
(주 ≤ 3건 · 같은 클래스 하루 1건 · 23~07시 금지) 중 앞의 둘은 "이미 얼마나
보냈나"를 알아야 enforce 할 수 있는데, 재시작·다중 인스턴스에서도 성립하려면
메모리가 아니라 DB 에 남아야 한다 (설계서 v0.7.1 에 없는 테이블 — 추가 근거는
ADR-0006, plan_drafts·user_consents 와 같은 '보존한 개선' 선례).

행은 **실제 발송 성공 시에만** 기록한다 — 게이트에 막힌 시도가 예산을 소모하면
사용자는 한 건도 못 받았는데 주 예산이 바닥나는 모순이 생긴다.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reaction_backend.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from reaction_backend.db.models.user import User

# 잠금: 알림은 3 클래스만 (AGENTS.md §1 — DevBaseline §1.4).
NOTIFICATION_CLASSES = ("morning_brief", "pre_card", "evening_reflection")


class NotificationSend(Base, TimestampMixin):
    __tablename__ = "notification_sends"
    __table_args__ = (
        CheckConstraint(
            "notification_class IN ('morning_brief', 'pre_card', 'evening_reflection')",
            name="ck_notification_sends_class",
        ),
        # 게이트 조회 2종(주간 카운트·클래스 dedup)이 전부 user_id + sent_at 범위 스캔.
        Index("ix_notification_sends_user_sent", "user_id", "sent_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    notification_class: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ── relationships ──
    user: Mapped[User] = relationship()
