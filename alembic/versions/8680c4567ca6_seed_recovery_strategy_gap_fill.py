"""회복 전략 4종 신설 — 태그 구멍(TIME_SHORTAGE/OVERRUN/AVOIDANCE) + PARK 도달 경로

Revision ID: 8680c4567ca6
Revises: c8d9e0f1a2b3
Create Date: 2026-08-17

배경 (`docs/research/recovery-evidence-base.md` §4.1, PR #256 의 전수 열거 실증):

1. **`TIME_SHORTAGE` / `OVERRUN` / `AVOIDANCE` 는 어떤 전략에도 매칭되지 않았다**
   (`tests/test_recovery_catalog_sync.py::test_uncovered_tags_are_a_design_decision_not_a_gap`
   이 이걸 "갭이 아니라 설계"로 핀하고 있었다 — 이 마이그레이션이 그 설계를 바꾼다).
2. **PARK 그룹은 92개 계약상 가능 입력 전부에서 0회 노출됐다**
   (`tests/test_recovery_selection_coverage.py::test_park_group_is_unreachable_for_every_contract_valid_input`).
   PARK_DEFAULT 는 `primary_trigger_tags=[]` 라 정적 태그로는 절대 안 뜨고, 동적 조건
   (`overwhelm_level >= 4`)은 `select_strategies(failure_tags, strategies)` 가 애초에
   그 인자를 받지 않아 구현할 자리가 없다 — 데이터 공백이 아니라 시그니처 공백.

이 마이그레이션이 **택한 해법**: `select_strategies` 의 순수 함수 시그니처는 건드리지
않는다. 대신 GOAL_RECHECK 를 **PARK 그룹의 정적 태그 전략**으로 신설해, 기존 9전략과
완전히 같은 매칭 경로로 PARK 를 도달 가능하게 만든다. `overwhelm_level` 동적 트리거는
`select_strategies` 시그니처 확장이 필요한 별개의 더 큰 변경이라 이 PR 범위 밖에 남긴다
(PARK_DEFAULT 는 그대로 `primary_trigger_tags=[]` 유지 — 지울 이유가 없다, 동적 조건이
구현되면 그때 쓰인다).

신설 4종:

| 전략 | 그룹 | primary_trigger_tags | 근거 |
|---|---|---|---|
| TIMEBOX_REBUDGET | RESCHEDULE | TIME_SHORTAGE, OVERRUN | Buehler et al. 1994 — 완료시간 과소추정. `execution_events.actual_duration_minutes` 로 이미 갖고 있는 실측을 근거로 다음 슬롯을 재산정 |
| BUFFER_INSERT | RESCHEDULE | OVERRUN | 동일 근거. OVERRUN 은 '이 카드'가 아니라 **선행 카드**의 계획 오류라 축소가 아니라 버퍼가 맞다 |
| SELF_FORGIVENESS_NANO | DOWNSCOPE | AVOIDANCE, HARD_TO_START | Wohl, Pychyl & Bennett 2010 (자기용서→미루기 감소) + Breines & Chen 2012 (자기자비→재시도 노력) |
| GOAL_RECHECK | PARK | AVOIDANCE, PRIORITY_SHIFT | Sheeran, Webb & Gollwitzer 2005 — 실행의도 효과는 목표몰입 강도에 조절된다. 몰입 저하 신호에 if-then 을 붙이는 대신 목표 자체를 재확인한다 |

기존 2전략의 태그 확장(`DOWNSCOPE_DEFAULT +TIME_SHORTAGE`, `ENVIRONMENT_SHIFT +AVOIDANCE`)은
근거 대장이 제안했지만 이 PR 에는 **포함하지 않는다** — 후자는 "연속실패 L2 이상에서만"이라는
에스컬레이션 게이트(L0~L4, 아직 미구현)를 전제로 하고, 전자를 단독으로 넣으면 그 게이트
없이 조건 없는 확장만 절반 구현하게 된다. 두 태그 확장은 에스컬레이션 정책 PR 에서 함께.

시드 INSERT 는 `ON CONFLICT (strategy_type) DO NOTHING` — 멱등. 기존 9전략 행은 이 마이그
레이션이 손대지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "8680c4567ca6"
down_revision: str | Sequence[str] | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RECOVERY_STRATEGIES: list[dict[str, object]] = [
    {
        "code": "TIMEBOX_REBUDGET",
        "group": "RESCHEDULE",
        "label_ko": "실측 시간으로 재산정",
        "template": "이 카드는 보통 그보다 시간이 더 걸렸어요. 다음엔 여유를 두고 다시 잡아드릴까요?",
        "min_unit": 15,
        "primary_tags": '["TIME_SHORTAGE", "OVERRUN"]',
        "allow_rest": False,
        "display_priority": 55,
    },
    {
        "code": "BUFFER_INSERT",
        "group": "RESCHEDULE",
        "label_ko": "다음 슬롯에 여유 넣기",
        "template": "직전 일이 길어졌던 날이었어요. 다음 슬롯 앞에 15분 여유를 넣어둘까요?",
        "min_unit": 15,
        "primary_tags": '["OVERRUN"]',
        "allow_rest": False,
        "display_priority": 58,
    },
    {
        "code": "SELF_FORGIVENESS_NANO",
        "group": "DOWNSCOPE",
        "label_ko": "지난 일은 접어두고 한 걸음만",
        "template": "어제 미룬 건 이미 지난 일로 두어요. 지금은 딱 한 걸음만 떼어볼까요?",
        "min_unit": 5,
        "primary_tags": '["AVOIDANCE", "HARD_TO_START"]',
        "allow_rest": False,
        "display_priority": 15,
    },
    {
        "code": "GOAL_RECHECK",
        "group": "PARK",
        "label_ko": "목표 다시 확인하기",
        "template": "이 목표, 지금도 하고 싶은 게 맞을까요? 잠시 접어두고 다음 주 리뷰 때 다시 볼까요?",
        "min_unit": 0,
        "primary_tags": '["AVOIDANCE", "PRIORITY_SHIFT"]',
        "allow_rest": True,
        "display_priority": 85,
    },
]


def upgrade() -> None:
    conn = op.get_bind()
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
    conn = op.get_bind()
    conn.execute(
        text("DELETE FROM recovery_strategy_catalog WHERE strategy_type = ANY(:codes)"),
        {"codes": [s["code"] for s in RECOVERY_STRATEGIES]},
    )
