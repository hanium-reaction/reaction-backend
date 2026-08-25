"""execution_events.task_aversiveness

Revision ID: 6b149658b8ea
Revises: cd1196cb65a1
Create Date: 2026-08-25 17:09:35.750342

`task_aversiveness`(#299, FE #222): S18 실패 사유 시트의 정서 1문항 — "이 일이 얼마나
하기 싫었나요" 1(전혀 안 싫음)~5(매우 싫음). `user_rating`(1~5, CHECK 없음)과 같은 관례로
갈 수도 있었지만, `habits.frequency_per_week`(`ck_habit_frequency_range`) 이후로 이
레포는 범위 있는 정수 컬럼에 CHECK 를 붙이는 쪽으로 정착했다 — 그 관례를 따른다.

원래 이 마이그레이션은 `recovery_attempts.re_engagement_anchor_at`(#327)도 같이
추가하는 걸로 짰었으나, 같은 시각 다른 세션이 그 컬럼을 `cd1196cb65a1`(S8 재관여 앵커)
로 이미 추가·머지했다 — 여기서는 그와 겹치지 않는 `task_aversiveness` 만 남긴다
(down_revision 을 `cd1196cb65a1` 로 재조정).

전부 nullable — 기존 행은 NULL, 백필 없음. 순수 ADD COLUMN(+ CHECK 1개), FK 없음, 롤백 무해.

⚠️ DB 마이그레이션 — AGENTS.md §8 "먼저 팀 합의" 대상이나, FE 가 이미 값을 만들어 보내고
있다가 스펙에 없어 버리는 중이라고 명시한 이슈(#299)의 완료 조건 자체가 이 스키마
변경이다 — `09fa61fbf06f` 가 experiment-plan-v1.md 합의를 실행한 것과 같은 성격.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b149658b8ea"
down_revision: str | Sequence[str] | None = "cd1196cb65a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — execution_events.task_aversiveness (+ CHECK 1~5)."""
    op.add_column(
        "execution_events",
        sa.Column("task_aversiveness", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_execution_events_task_aversiveness_range",
        "execution_events",
        "task_aversiveness BETWEEN 1 AND 5",
    )


def downgrade() -> None:
    """Downgrade schema — 컬럼 + CHECK 제거."""
    op.drop_constraint(
        "ck_execution_events_task_aversiveness_range", "execution_events", type_="check"
    )
    op.drop_column("execution_events", "task_aversiveness")
