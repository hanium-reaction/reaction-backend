"""interview_sessions.used_fallback 추가 (#6 인터뷰 fallback 텔레메트리 영속)

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-07-03 00:00:00.000000

인터뷰 중 한 번이라도 LLM 룰 fallback 이 있었는지(used_fallback)를 세션에 영속한다.
그동안 이 값은 턴마다 재조립되며 리셋돼 `outcome.analysis_source` 가 마지막 턴 기준으로만
정확했다 → 세션 컬럼으로 OR 누적해 전체 인터뷰 기준으로 맞춘다.

⚠️ DB 마이그레이션 — AGENTS §8 "먼저 팀 합의". 기존 행은 server_default(false)로 백필.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: str | Sequence[str] | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — interview_sessions.used_fallback (NOT NULL, default false)."""
    op.add_column(
        "interview_sessions",
        sa.Column(
            "used_fallback",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema — used_fallback 제거."""
    op.drop_column("interview_sessions", "used_fallback")
