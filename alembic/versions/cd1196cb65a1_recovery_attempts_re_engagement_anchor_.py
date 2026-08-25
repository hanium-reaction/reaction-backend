"""recovery_attempts re_engagement_anchor_at

Revision ID: cd1196cb65a1
Revises: 611428450063
Create Date: 2026-08-25 16:33:10.341805

근거 대장 §3 S8 — "PARK/CARRY_OVER 수락 시 `re_engagement_anchor_at` 필수". 모델
docstring 참고 — PARK 는 새 카드를 안 만들어 이 필드가 없으면 미래 접점이 사라진다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cd1196cb65a1"
down_revision: str | Sequence[str] | None = "611428450063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "recovery_attempts",
        sa.Column("re_engagement_anchor_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("recovery_attempts", "re_engagement_anchor_at")
