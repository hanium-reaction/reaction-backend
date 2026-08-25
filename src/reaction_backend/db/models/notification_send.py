"""NotificationSend — Web Push 발송 이력 (INSERT only). Issue #20 알림 cron.

발송 **게이트**(`safety/push_gate.py`)의 상태 저장소다. 잠금 규칙 세 가지
(주 ≤ 3건 · 같은 클래스 하루 1건 · 23~07시 금지) 중 앞의 둘은 "이미 얼마나
보냈나"를 알아야 enforce 할 수 있는데, 재시작·다중 인스턴스에서도 성립하려면
메모리가 아니라 DB 에 남아야 한다 (설계서 v0.7.1 에 없는 테이블 — 추가 근거는
ADR-0006, plan_drafts·user_consents 와 같은 '보존한 개선' 선례).

행은 **실제 발송 성공 시에만** 기록한다 — 게이트에 막힌 시도가 예산을 소모하면
사용자는 한 건도 못 받았는데 주 예산이 바닥나는 모순이 생긴다.

`target_action_item_id`/`opened_at` (근거 대장 §6.1 "선행 조건") — S9 재알림 T1 억제
조건과 근접 효과 측정에 필요하다고 문서가 명시한 두 컬럼. `id` 는 이제 발송 전에
호출부(`notify_sweeps.py`)가 미리 생성해 push payload 에 실어 보낸다 — `opened_at` 을
채우려면 브라우저가 "어느 알림을 열었는지" 서버에 되돌려줘야 하는데, 발송 **후**에
서버 쪽에서 id 를 만들면(예전처럼 `server_default`) 그 id 가 payload 에 없어 클라이언트가
영원히 모른다. **`opened_at` 을 실제로 채우는 클라이언트 콜백(FE 의 push
`notificationclick` 핸들러)은 아직 없다** — 이 마이그레이션은 인프라만 미리 준비해
둔 것이고, FE 가 배선하기 전까지 `opened_at` 은 항상 NULL 이다.
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
    from reaction_backend.db.models.action_item import ActionItem
    from reaction_backend.db.models.user import User

# 잠금: 알림은 3 클래스만 (AGENTS.md §1 — DevBaseline §1.4).
NOTIFICATION_CLASSES = ("morning_brief", "pre_card", "evening_reflection")

# push payload 의 `id` 필드 = 이 접두어 + PK (api-contract §1.8 ID 표기 관례). 발송부
# (`scheduler/notify_sweeps.py`, 싣는 쪽)와 opened API(`api/routes/notifications.py`,
# 벗기는 쪽)가 이 상수 하나를 같이 참조 — 리터럴을 두 곳에 따로 두면 드리프트 위험이 있다.
NOTIFICATION_ID_PREFIX = "notif_"


class NotificationSend(Base, TimestampMixin):
    __tablename__ = "notification_sends"
    __table_args__ = (
        CheckConstraint(
            "notification_class IN ('morning_brief', 'pre_card', 'evening_reflection')",
            name="ck_notification_sends_class",
        ),
        # 게이트 조회 2종(주간 카운트·클래스 dedup)이 전부 user_id + sent_at 범위 스캔.
        Index("ix_notification_sends_user_sent", "user_id", "sent_at"),
        # 근접 효과 분석이 이 컬럼으로 execution_events 와 조인할 것을 대비 — alembic check
        # drift 를 피하려 마이그레이션과 이름을 맞춰 여기 명시.
        Index("ix_notification_sends_target_action_item", "target_action_item_id"),
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

    # 이 발송이 어떤 카드에 대한 것인지 — pre_card 는 항상 채워지고(그 블록의 카드),
    # evening_reflection/morning_brief 는 특정 카드 하나가 아니라 항상 NULL.
    target_action_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("action_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    # 사용자가 이 알림을 열었다고 서버가 확인한 시각 — 최초 1회만 채운다(멱등, `first_viewed_at`
    # 과 같은 관례). 채우는 경로(FE `notificationclick` → 아래 API)는 아직 없다 — 모듈 docstring.
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── relationships ──
    user: Mapped[User] = relationship()
    target_action_item: Mapped[ActionItem | None] = relationship()
