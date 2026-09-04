"""goal_nodes.source — 룰이 만든 리프를 'rule' 로 정정 (#454)

`goal_nodes.source` 는 처음부터 `llm | rule | user` 를 뜻했고 CHECK 제약도 그렇다.
그런데 **계획 트리에서 이 값이 맞은 적이 한 번도 없다.**

- `1ee508b967ba` 가 기존 행을 전량 `llm` 로 백필했다 — "기존 행은 계획 LLM 이 만든
  것이므로 'llm' 이 정확". 그 시점엔 합리적인 가정이었지만 룰 패딩까지 함께 llm 이 됐다.
- 그 뒤 행은 `first_plan_adapter._apply_once` 가 세팅을 안 해 서버 기본값 `user` 로 떨어졌다.

실측(2026-09-05, 로컬): 자리표시자 카드 77장의 노드가 llm 68 · user 9. **둘 다 거짓이다.**

## 왜 지금 고치는가

승인 시점에 초안 node_id(`tmp-continue-N`)가 실제 UUID 로 바뀌고, **초안 id 를 보존하는
컬럼이 없다.** 이 값을 안 남기면 DB 에 들어간 뒤로는 자리표시자를 식별할 방법이 없다.
재계획이 그 내용을 채우려면(#454 방향 1) 이 컬럼이 유일한 단서다.

## 무엇을 고치고 무엇을 안 고치는가

**리프만 고친다.** 판별 근거는 그 리프의 카드가 들고 있는 **룰의 고정 first_step** 이다:

| 문자열 | 만든 곳 |
|---|---|
| `지난 회차에서 이어서 5분만 시작하기` | `extend_action_plan_to_horizon` (마감까지 보충) |
| `가장 쉬운 부분부터 5분만 시작하기` | `first_plan._rule_fallback` (분해 LLM 실패) |

레포 전체에서 이 두 문자열을 쓰는 코드는 각각 한 곳뿐이라 오탐이 날 수 없다.
사용자가 이미 손대 문구가 바뀐 카드는 매치되지 않는다 — **그게 맞다.** 더 이상
자리표시자가 아니므로 채워 줄 대상도 아니다.

**구조 노드(브랜치·루트)는 건드리지 않는다.** 행마다의 증거가 없다 — '이어가기' 브랜치는
제목으로 추정할 수 있지만, 실측에서 룰 리프의 부모 9개 중 하나는 자식 8개 중 7개만 룰인
`core` 노드였다(폴백 루트로 보인다). 추정으로 사용자 데이터를 덮어쓰지 않는다.
앞으로 생기는 행은 코드가 브랜치까지 `rule` 로 남긴다 — 과거분과 이 차이는 의도된 것이다.

downgrade 는 되돌리지 않는다. `llm` 로 돌리면 **다시 거짓이 되고**, 어느 행이 원래
`user` 였는지도 알 수 없다. 컬럼 정의는 그대로이므로 스키마상 아무것도 잃지 않는다.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3a17f4d92be"
down_revision = "6b149658b8ea"
branch_labels = None
depends_on = None

# `first_plan_adapter.extend_action_plan_to_horizon` · `first_plan._rule_fallback` 이
# 박는 고정 first_step. 코드 쪽 상수와 갈리면 백필이 조용히 빗나가므로,
# tests/test_goal_node_source.py 가 이 목록과 코드를 대조한다.
_RULE_FIRST_STEPS = (
    "지난 회차에서 이어서 5분만 시작하기",
    "가장 쉬운 부분부터 5분만 시작하기",
)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE goal_nodes
               SET source = 'rule'
             WHERE source <> 'rule'
               AND id IN (
                   SELECT DISTINCT a.goal_node_id
                     FROM action_items a
                    WHERE a.goal_node_id IS NOT NULL
                      AND a.first_step = ANY(:steps)
               )
            """
        ).bindparams(sa.bindparam("steps", value=list(_RULE_FIRST_STEPS), type_=sa.ARRAY(sa.Text)))
    )


def downgrade() -> None:
    """되돌리지 않는다 — 모듈 docstring 참고."""
