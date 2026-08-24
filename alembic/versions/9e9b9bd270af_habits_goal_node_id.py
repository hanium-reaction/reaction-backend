"""habits.goal_node_id — 반복형 만다라 칸 링크 (ADR-0008 §1, §7)

만다라 64칸 중 "코딩테스트 1일 1문제"·"쓰레기 줍기" 처럼 끝이 없는 칸은 계획(마감이 있는
action_item)으로 내려보내지 않는다. 대신 기존 습관 인프라(habits/habit_instances)에 링크해
주간 횟수로만 추적한다 — 칸의 종류(프로젝트형/반복형)는 별도 컬럼을 두지 않고 이 링크의
유무로 판정한다.

nullable 이라 기존 행은 전부 NULL(= 만다라와 무관한 일반 습관)로 즉시 VALID. 칸이
삭제(archived)돼도 습관 기록은 남아야 하므로 `ON DELETE SET NULL`(hard delete 금지,
AGENTS §2 와 같은 방향). 부분 유니크 인덱스로 "칸 하나당 활성 습관 하나"를 강제한다 —
`uq_goal_nodes_mandala_slot` 등 이 레포의 기존 부분 유니크 인덱스 패턴과 동일.

Revision ID: 9e9b9bd270af
Revises: a1b2c3d4e5f6
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9e9b9bd270af"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("habits", sa.Column("goal_node_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_habits_goal_node_id",
        "habits",
        "goal_nodes",
        ["goal_node_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_habits_goal_node_id", "habits", ["goal_node_id"])
    op.create_index(
        "uq_habits_goal_node_id_active",
        "habits",
        ["goal_node_id"],
        unique=True,
        postgresql_where=sa.text("goal_node_id IS NOT NULL AND archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_habits_goal_node_id_active", table_name="habits")
    op.drop_index("ix_habits_goal_node_id", table_name="habits")
    op.drop_constraint("fk_habits_goal_node_id", "habits", type_="foreignkey")
    op.drop_column("habits", "goal_node_id")
