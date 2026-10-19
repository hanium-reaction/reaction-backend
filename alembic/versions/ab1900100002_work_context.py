"""Optional action links and independent execution notes."""

import sqlalchemy as sa
from alembic import op

revision = "ab1900100002"
down_revision = "ab1900100001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, key, target, content in (
        ("action_resources", "action_id", "action_items.id", "content_encrypted"),
        ("execution_notes", "execution_id", "execution_events.id", "memo_encrypted"),
    ):
        op.create_table(
            name,
            sa.Column(key, sa.Uuid(), sa.ForeignKey(target), primary_key=True),
            sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column(content, sa.Text()),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(f"ix_{name}_user_id", name, ["user_id"])


def downgrade() -> None:
    for name in ("execution_notes", "action_resources"):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {name} LIMIT 1) THEN RAISE EXCEPTION 'Context records exist: roll back application only'; END IF; END $$;"
        )
        op.drop_table(name)
