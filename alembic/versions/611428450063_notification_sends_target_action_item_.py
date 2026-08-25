"""notification_sends target_action_item_id + opened_at

Revision ID: 611428450063
Revises: 9e9b9bd270af
Create Date: 2026-08-25 15:42:42.576128

근거 대장 §6.1 "선행 조건" — S9 재알림 T1 억제 조건과 근접 효과 측정에 필요하다고
명시한 두 컬럼. 모델 docstring 참고 — `opened_at` 을 실제로 채우는 FE 콜백은 아직 없다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "611428450063"
down_revision: str | Sequence[str] | None = "9e9b9bd270af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "notification_sends",
        sa.Column("target_action_item_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "notification_sends",
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_notification_sends_target_action_item_id",
        "notification_sends",
        "action_items",
        ["target_action_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_notification_sends_target_action_item",
        "notification_sends",
        ["target_action_item_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_notification_sends_target_action_item", table_name="notification_sends")
    op.drop_constraint(
        "fk_notification_sends_target_action_item_id",
        "notification_sends",
        type_="foreignkey",
    )
    op.drop_column("notification_sends", "opened_at")
    op.drop_column("notification_sends", "target_action_item_id")
