"""Add first-action attempts without changing existing completion semantics.

Authorized P0 implementation; additive schema only. Apply separately from app deployment.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab1900100001"
down_revision: str | Sequence[str] | None = "c3a17f4d92be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reentry_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action_item_id", sa.Uuid(), sa.ForeignKey("action_items.id"), nullable=False),
        sa.Column("execution_id", sa.Uuid(), sa.ForeignKey("execution_events.id")),
        sa.Column("entry_kind", sa.String(20), nullable=False),
        sa.Column("first_step_encrypted", sa.Text()),
        *[
            sa.Column(name, sa.DateTime(timezone=True))
            for name in ("viewed_at", "selected_at", "started_at", "step_completed_at", "ended_at")
        ],
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_reentry_attempts_user_id", "reentry_attempts", ["user_id"])
    op.create_index("ix_reentry_attempts_action_item_id", "reentry_attempts", ["action_item_id"])


def downgrade() -> None:
    # Empty test schemas can downgrade; populated installations retain their records.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM reentry_attempts LIMIT 1) THEN
        RAISE EXCEPTION 'Reentry records exist: roll back application code only';
      END IF;
    END $$;""")
    op.drop_table("reentry_attempts")
