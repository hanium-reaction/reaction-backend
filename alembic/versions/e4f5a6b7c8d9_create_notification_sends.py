"""create notification_sends (Web Push 발송 이력, #20 알림 cron)

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-07-21 00:00:00.000000

⚠️ 새 테이블 추가 — AGENTS §8 "DB 마이그레이션은 먼저 팀 합의". 설계서 v0.7.1 에 없는
테이블이며(발송 이력·budget 추적 테이블 부재 확인 — erd-diff.md), 잠금 규칙(주 ≤3건 ·
같은 클래스 하루 1건)을 재시작 후에도 enforce 하려면 상태 저장이 필요하다. 근거·대안
검토는 ADR-0006. plan_drafts·user_consents 와 같은 '설계서 외 보존한 개선' 선례를 따른다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e4f5a6b7c8d9"
down_revision: str | Sequence[str] | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — notification_sends 테이블 생성."""
    op.create_table(
        "notification_sends",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_class", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "notification_class IN ('morning_brief', 'pre_card', 'evening_reflection')",
            name="ck_notification_sends_class",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_sends_user_sent",
        "notification_sends",
        ["user_id", "sent_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema — notification_sends 제거."""
    op.drop_index("ix_notification_sends_user_sent", table_name="notification_sends")
    op.drop_table("notification_sends")
