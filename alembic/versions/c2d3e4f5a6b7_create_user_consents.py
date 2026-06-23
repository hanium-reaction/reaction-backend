"""create user_consents (S28 Privacy 동의 기록, #23-B)

Revision ID: c2d3e4f5a6b7
Revises: b1f2a3c4d5e6
Create Date: 2026-06-23 00:00:00.000000

⚠️ 새 테이블 추가 — AGENTS §8 "DB 마이그레이션은 먼저 팀 합의". append-only 동의 기록.
Issue #23-B 리뷰 PR 초안으로, 팀 합의 후 머지한다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: str | Sequence[str] | None = "b1f2a3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — user_consents 테이블 생성 (append-only)."""
    op.create_table(
        "user_consents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "consent_type",
            sa.Enum("required", "marketing", "research", name="consent_type"),
            nullable=False,
        ),
        sa.Column("is_granted", sa.Boolean(), nullable=False),
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
    op.create_index(op.f("ix_user_consents_user_id"), "user_consents", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema — user_consents 제거."""
    op.drop_index(op.f("ix_user_consents_user_id"), table_name="user_consents")
    op.drop_table("user_consents")
    sa.Enum(name="consent_type").drop(op.get_bind(), checkfirst=True)
