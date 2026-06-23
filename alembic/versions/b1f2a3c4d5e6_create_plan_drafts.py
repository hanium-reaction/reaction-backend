"""create plan_drafts (First Plan HITL Draft 영속화, #62)

Revision ID: b1f2a3c4d5e6
Revises: d09c105520b5
Create Date: 2026-06-22 00:00:00.000000

⚠️ 새 테이블 추가 — AGENTS §8 "DB 마이그레이션은 먼저 팀 합의". 본 마이그레이션은
Issue #62 리뷰 PR 의 초안으로, 팀 합의 후 머지한다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b1f2a3c4d5e6"
down_revision: str | Sequence[str] | None = "d09c105520b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — plan_drafts 테이블 생성."""
    op.create_table(
        "plan_drafts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "approved", "expired", name="plan_draft_status"),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("horizon", sa.String(length=10), nullable=True),
        sa.Column(
            "ai_source",
            sa.Enum("llm", "rule", name="plan_draft_ai_source"),
            server_default="llm",
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_plan_drafts_user_id"), "plan_drafts", ["user_id"], unique=False)
    op.create_index(op.f("ix_plan_drafts_status"), "plan_drafts", ["status"], unique=False)


def downgrade() -> None:
    """Downgrade schema — plan_drafts 제거."""
    op.drop_index(op.f("ix_plan_drafts_status"), table_name="plan_drafts")
    op.drop_index(op.f("ix_plan_drafts_user_id"), table_name="plan_drafts")
    op.drop_table("plan_drafts")
    sa.Enum(name="plan_draft_ai_source").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="plan_draft_status").drop(op.get_bind(), checkfirst=True)
