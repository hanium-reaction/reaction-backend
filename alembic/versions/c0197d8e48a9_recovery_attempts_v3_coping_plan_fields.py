"""recovery_attempts v3 coping plan fields

Revision ID: c0197d8e48a9
Revises: cd1196cb65a1
Create Date: 2026-08-25 20:47:44.937201

근거 대장 §6.2 T2 후속 — S9 T2(#343)에서 사용 중인 `re_engagement_anchor_at` 옆에,
이번엔 S5/S1 acknowledgment/v3 승격(AVOIDANCE 전용)이 쓰는 코핑 플랜 3필드를 얹는다.
v2 personalize 는 이 필드를 요청하지 않아 항상 NULL — `db/models/recovery_attempt.py`
모듈 주석 참고.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0197d8e48a9"
down_revision: str | Sequence[str] | None = "cd1196cb65a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("recovery_attempts", sa.Column("obstacle", sa.Text(), nullable=True))
    op.add_column("recovery_attempts", sa.Column("coping_clause", sa.Text(), nullable=True))
    op.add_column("recovery_attempts", sa.Column("acknowledgment", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("recovery_attempts", "acknowledgment")
    op.drop_column("recovery_attempts", "coping_clause")
    op.drop_column("recovery_attempts", "obstacle")
