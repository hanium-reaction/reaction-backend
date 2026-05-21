"""seed master data v0.7.1 (failure tags + recovery strategies)

Revision ID: d09c105520b5
Revises: 59acd6c5f086
Create Date: 2026-05-21

마스터 데이터 seed (DB 설계서 v0.7.1 기준):
- failure_reason_tags 13종 — PK = tag_code (string)
- recovery_strategy_catalog 9전략 — PK = strategy_type (string) + primary_trigger_tags JSONB (v0.7.1 신규)

idempotent: ON CONFLICT (PK) DO NOTHING
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "d09c105520b5"
down_revision: str | Sequence[str] | None = "59acd6c5f086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# DB 설계서 v0.7.1 §5.13 — 13종 잠금 (label_ko, description)
FAILURE_REASON_TAGS: list[dict[str, object]] = [
    {
        "code": "TIME_SHORTAGE",
        "label_ko": "시간이 부족했어요",
        "description": "예상보다 시간이 짧았거나, 다른 일에 시간을 더 썼어요.",
        "sort_order": 10,
    },
    {
        "code": "LOW_ENERGY",
        "label_ko": "에너지가 낮았어요",
        "description": "컨디션이 평소보다 낮았어요.",
        "sort_order": 20,
    },
    {
        "code": "HARD_TO_START",
        "label_ko": "시작이 어려웠어요",
        "description": "첫 5분이 잘 안 떨어졌어요.",
        "sort_order": 30,
    },
    {
        "code": "PRIORITY_SHIFT",
        "label_ko": "더 중요한 일이 생겼어요",
        "description": "우선순위가 바뀌었어요.",
        "sort_order": 40,
    },
    {
        "code": "PLAN_TOO_BIG",
        "label_ko": "계획이 너무 컸어요",
        "description": "한 번에 다루기 너무 컸어요.",
        "sort_order": 50,
    },
    {
        "code": "FATIGUE",
        "label_ko": "피곤했어요",
        "description": "신체적으로 지쳤어요.",
        "sort_order": 60,
    },
    {
        "code": "AMBIGUITY",
        "label_ko": "뭘 해야 할지 모호했어요",
        "description": "어디서 시작할지 명확하지 않았어요.",
        "sort_order": 70,
    },
    {
        "code": "CONFLICT",
        "label_ko": "다른 일정과 겹쳤어요",
        "description": "예상치 못한 일정 충돌이 있었어요.",
        "sort_order": 80,
    },
    {
        "code": "OVERRUN",
        "label_ko": "이전 일이 길어졌어요",
        "description": "직전 작업이 예상보다 오래 걸렸어요.",
        "sort_order": 90,
    },
    {
        "code": "AVOIDANCE",
        "label_ko": "회피하고 싶었어요",
        "description": "심리적으로 미루고 싶었어요.",
        "sort_order": 100,
    },
    {
        "code": "DISTRACTION",
        "label_ko": "방해를 받았어요",
        "description": "외부 방해(알림·사람·소음)가 있었어요.",
        "sort_order": 110,
    },
    {
        "code": "EMERGENCY",
        "label_ko": "급한 일이 있었어요",
        "description": "예상치 못한 긴급한 상황이 발생했어요.",
        "sort_order": 120,
    },
    {
        "code": "CONTEXT_LOSS",
        "label_ko": "맥락을 잃었어요",
        "description": "중단 후 어디까지 했는지 다시 잡기 힘들었어요.",
        "sort_order": 130,
    },
]


# DB 설계서 v0.7.1 §5.17 + §6.10 — 9전략 + primary_trigger_tags
RECOVERY_STRATEGIES: list[dict[str, object]] = [
    {
        "code": "NANO_STEP",
        "group": "DOWNSCOPE",
        "label_ko": "5분 단위로 쪼개기",
        "template": "딱 5분만, 첫 단계만 해볼까요? {first_step}",
        "min_unit": 5,
        "primary_tags": '["AMBIGUITY", "HARD_TO_START"]',
        "allow_rest": False,
        "display_priority": 10,
    },
    {
        "code": "DOWNSCOPE_DEFAULT",
        "group": "DOWNSCOPE",
        "label_ko": "범위 줄여서 진행",
        "template": "오늘은 절반만, 가능한 만큼만 해볼까요?",
        "min_unit": 15,
        "primary_tags": '["FATIGUE", "PLAN_TOO_BIG"]',
        "allow_rest": False,
        "display_priority": 20,
    },
    {
        "code": "ENVIRONMENT_SHIFT",
        "group": "DOWNSCOPE",
        "label_ko": "공간 옮겨서 30분",
        "template": "공간을 옮겨서 30분만 해볼까요? 잘 되는 자리가 있으셨죠.",
        "min_unit": 30,
        "primary_tags": '["DISTRACTION"]',
        "allow_rest": False,
        "display_priority": 30,
    },
    {
        "code": "CONTEXT_REWARMING",
        "group": "DOWNSCOPE",
        "label_ko": "맥락 워밍업 5분",
        "template": "{suspended_step} 부터, 5분 워밍업으로 다시 잡아볼까요?",
        "min_unit": 5,
        "primary_tags": '["CONTEXT_LOSS"]',
        "allow_rest": False,
        "display_priority": 40,
    },
    {
        "code": "RESCHEDULE_DEFAULT",
        "group": "RESCHEDULE",
        "label_ko": "내일로 옮기기",
        "template": "내일 잘 되는 시간대로 옮겨드릴까요?",
        "min_unit": 30,
        "primary_tags": '["CONFLICT"]',
        "allow_rest": False,
        "display_priority": 50,
    },
    {
        "code": "ACTIVE_RECOVERY",
        "group": "RESCHEDULE",
        "label_ko": "산책 후 가볍게",
        "template": "잠깐 산책 20분 후, 가벼운 정리만 해볼까요?",
        "min_unit": 20,
        "primary_tags": '["LOW_ENERGY", "FATIGUE"]',
        "allow_rest": True,
        "display_priority": 60,
    },
    {
        "code": "CARRYOVER_DEFAULT",
        "group": "CARRY_OVER",
        "label_ko": "내일 같은 시간",
        "template": "내일 같은 슬롯으로 그대로 옮겨드릴까요?",
        "min_unit": 30,
        "primary_tags": '["PRIORITY_SHIFT"]',
        "allow_rest": False,
        "display_priority": 70,
    },
    {
        "code": "FREEZE_SLOT",
        "group": "CARRY_OVER",
        "label_ko": "슬롯 예약 (다음 주)",
        "template": "이번 슬롯은 비워두고 다음 주 같은 시간에 예약할게요.",
        "min_unit": 30,
        "primary_tags": '["EMERGENCY"]',
        "allow_rest": False,
        "display_priority": 80,
    },
    {
        "code": "PARK_DEFAULT",
        "group": "PARK",
        "label_ko": "이번 주는 보류",
        "template": "이번 주는 보류하고, 다음 주 리뷰 때 다시 보는 건 어때요?",
        "min_unit": 0,
        "primary_tags": "[]",
        "allow_rest": True,
        "display_priority": 90,
    },
]


def upgrade() -> None:
    """Seed master data — idempotent via ON CONFLICT DO NOTHING."""
    conn = op.get_bind()

    for tag in FAILURE_REASON_TAGS:
        conn.execute(
            text(
                """
                INSERT INTO failure_reason_tags
                  (tag_code, label_ko, description, sort_order, is_active)
                VALUES (:code, :label_ko, :description, :sort_order, true)
                ON CONFLICT (tag_code) DO NOTHING
                """
            ),
            tag,
        )

    for strat in RECOVERY_STRATEGIES:
        conn.execute(
            text(
                """
                INSERT INTO recovery_strategy_catalog
                  (strategy_type, option_group, label_ko, if_then_template,
                   min_recovery_unit_minutes, primary_trigger_tags,
                   allow_rest_mode, display_priority, is_active)
                VALUES (:code, CAST(:group AS recovery_option_group), :label_ko, :template,
                        :min_unit, CAST(:primary_tags AS jsonb),
                        :allow_rest, :display_priority, true)
                ON CONFLICT (strategy_type) DO NOTHING
                """
            ),
            strat,
        )


def downgrade() -> None:
    """Remove seeded rows by code (only ones we inserted)."""
    conn = op.get_bind()
    conn.execute(
        text("DELETE FROM recovery_strategy_catalog WHERE strategy_type = ANY(:codes)"),
        {"codes": [s["code"] for s in RECOVERY_STRATEGIES]},
    )
    conn.execute(
        text("DELETE FROM failure_reason_tags WHERE tag_code = ANY(:codes)"),
        {"codes": [t["code"] for t in FAILURE_REASON_TAGS]},
    )
