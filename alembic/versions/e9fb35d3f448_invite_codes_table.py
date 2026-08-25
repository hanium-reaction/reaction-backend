"""invite_codes table

Revision ID: e9fb35d3f448
Revises: cd1196cb65a1
Create Date: 2026-08-25 17:49:26.003923

초대코드 가입 게이트(#324, FE #237 §8) — Play 첫 공개 30명 원칙. 코드는
`scripts/manage_invite_codes.py` 로 운영자가 미리 발급하고, `POST /auth/google` 의
신규 가입(이미 있는 email 은 게이트를 안 거친다)이 유효·미사용 코드 하나를 소비한다.

- `code` unique — 같은 문자열로 두 번 발급하려 하면 DB 가 막는다(운영 실수 방지).
- `used_by_user_id` FK ondelete=SET NULL — 사용자가 나중에 soft-delete 되어도 코드
  행 자체(발급·소진 이력)는 남는다, 참조만 끊는다.

전부 nullable(`note`/`used_at`/`used_by_user_id`) 이거나 신규 테이블이라 백필 없음.
롤백 무해(drop_table).

⚠️ DB 마이그레이션 — AGENTS.md §8 "먼저 팀 합의" 대상. #324(FE #237 §8 파생, P0)의
완료 조건 자체가 이 테이블 신설이라 이 PR 이 그 실행분이다.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9fb35d3f448"
down_revision: str | Sequence[str] | None = "cd1196cb65a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — invite_codes 테이블 신설 + code unique index."""
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_user_id", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(["used_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_invite_codes_code"), "invite_codes", ["code"], unique=True)


def downgrade() -> None:
    """Downgrade schema — invite_codes 테이블 제거."""
    op.drop_index(op.f("ix_invite_codes_code"), table_name="invite_codes")
    op.drop_table("invite_codes")
