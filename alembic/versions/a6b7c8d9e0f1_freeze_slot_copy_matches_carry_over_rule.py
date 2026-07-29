"""FREEZE_SLOT 문구를 실제 날짜 규칙에 맞춘다 (#175)

문제: 카탈로그가 '슬롯 예약 (다음 주)' / '이번 슬롯은 비워두고 다음 주 같은 시간에 예약할게요'
라고 약속하는데, 실제 날짜 규칙은 `recovery_target_date` 가 `option_group` 만 보고 CARRY_OVER
전체를 **결정일 +1일**로 만든다. 사용자가 HITL 로 '다음 주'를 승인했는데 내일 카드가 생겼다
= 승인한 것과 다른 결과 (AGENTS.md §1 '자동 적용 금지'의 전제 위반).

왜 날짜 규칙(+7)이 아니라 문구를 고쳤나:
- `replan.next_week_start(today) = today + (7 - weekday)` 라 `결정일+7` 은 **모든 요일에서**
  주간 forward 재계획 창 안이다. 그 창의 후보 수집(`list_scheduled_between`)은
  `source != 'user_edit'` 만 거르므로 `source='recovery'` 블록이 후보가 되고, 승인 시
  취소 후 다른 시각에 `ai_plan` 으로 재생성된다 — 승인한 슬롯이 조용히 옮겨져 같은 결함이
  다른 경로로 재발한다.
- '이번 슬롯을 **비워두고 예약**' 도 구현되지 않는다(스키마에 슬롯 hold 개념이 없고 원본
  블록도 그대로 남는다). 문구를 고치면 두 거짓이 한 번에 사라진다.

CARRYOVER_DEFAULT 와의 구분 축은 '시간 지평'에서 **귀인**(우선순위 변경 이월 vs 긴급 상황 뒤
슬롯 보전)으로 옮긴다. 같은 그룹이라 동시 노출은 구조적으로 불가능하다(그룹당 1카드).

시드 INSERT 는 `ON CONFLICT DO NOTHING` 이고 이미 적용된 revision 은 재실행되지 않으므로,
시드 파일만 고치면 기존 DB 는 영원히 옛 문구로 남는다 → UPDATE 가 필수다.

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
"""

from __future__ import annotations

from alembic import op

revision = "a6b7c8d9e0f1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None

_NEW_LABEL = "급한 일 먼저, 같은 슬롯 유지"
_NEW_TEMPLATE = "급한 일이 먼저였잖아요. 같은 시간대를 그대로 지켜서 다시 잡아드릴까요?"
_OLD_LABEL = "슬롯 예약 (다음 주)"
_OLD_TEMPLATE = "이번 슬롯은 비워두고 다음 주 같은 시간에 예약할게요."


def _set_copy(label: str, template: str) -> None:
    op.execute(
        f"""
        UPDATE recovery_strategy_catalog
           SET label_ko = '{label}',
               if_then_template = '{template}',
               updated_at = now()
         WHERE strategy_type = 'FREEZE_SLOT'
        """  # noqa: S608 — 리터럴 상수만, 사용자 입력 없음
    )


def upgrade() -> None:
    _set_copy(_NEW_LABEL, _NEW_TEMPLATE)


def downgrade() -> None:
    _set_copy(_OLD_LABEL, _OLD_TEMPLATE)
