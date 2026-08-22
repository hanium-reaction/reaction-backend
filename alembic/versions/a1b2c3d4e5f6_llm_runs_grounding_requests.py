"""llm_runs.grounding_requests — 검색 그라운딩 요청 계량 (#259 §3)

그라운딩은 토큰과 **별도 과금**인데(무료 5,000건/월, 초과분 $14/1,000건) 검색이 서버 쪽에서
일어나 입력 토큰이 17개로 잡힌다. 그래서 `tokens_in + tokens_out` 기반인 일일 토큰 예산이
이 비용을 전혀 못 본다 — 루프가 돌면 계량기는 0 인데 돈이 나간다(#259 §3 실측).

이 컬럼이 그 계량기다. `safety/llm_budget.check_grounding()` 이 이 값을 합산해 사용자별
일일 상한을 건다.

기존 행은 0 — 그라운딩을 쓴 호출이 아직 없으므로 소급 계산할 것도 없다.

Revision ID: a1b2c3d4e5f6
Revises: d4e5f6a7b8c9
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_runs",
        sa.Column(
            "grounding_requests",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_runs", "grounding_requests")
