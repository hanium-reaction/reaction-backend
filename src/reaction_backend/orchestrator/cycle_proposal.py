"""다음 2주 열기 제안 판정 — 만다라 유래 목표 전용 (ADR-0008 §5, §8 "G").

ADR-0007 §5 의 "잔여 세션 판정"(마지막 주를 **날짜**가 아니라 **완료 여부**로 본다)을
재사용한다 — 3개 가드 중 마일스톤 가드는 뺐다: 이 스코프(만다라 2주 rolling 창, PR D)의
목표는 마감이 없을 수도 있고 마일스톤 층을 안 거친다.

이 모듈은 **판정만** 한다. 전환 "생성"은 새로 안 만든다 — 승인은 기존
`POST /plans/generate`(빈 바디로 호출하면 최근 완료 인터뷰를 재투영, `_resolve_outcome`
우선순위 ③) + `POST /plans/{id}/approve` 를 그대로 탄다(ADR-0008 §5). "커서 전진"은
재생성 시점이 항상 '지금'이라는 사실에서 자연히 나온다(ADR-0008 §8 "D" 완료 메모).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem

# 아직 안 끝난 카드. 단, "남은 카드"로 세려면 `target_date` 도 아직 안 지나야 한다
# (`should_propose_next_cycle` 참고) — 날짜가 지난 미종결 카드는 '밀린 것'이지 '남은 것'이 아니다.
_ACTIVE_ACTION_STATUSES = ("planned", "in_progress")
# 성공/실패 정의는 mandala_adapter.py·weekly_review.py 와 같은 값이다(다른 지표와 다른
# 숫자를 말하지 않으려고) — 순환 의존을 피해 재정의한다(두 모듈이 이미 쓰는 관례).
_TERMINAL_ACTION_STATUSES = ("done", "partial_done", "failed", "over_done")

__all__ = [
    "NextCycleProposal",
    "fetch_action_items_for_leaf_nodes",
    "should_propose_next_cycle",
]


@dataclass(frozen=True)
class NextCycleProposal:
    """다음 2주 열기 제안 대상 1건 — 승인은 기존 generate+approve 가 그대로 한다."""

    goal_id: uuid.UUID
    goal_title: str
    axis_title: str | None


def should_propose_next_cycle(action_items: Sequence[ActionItem], *, today: date) -> bool:
    """`action_items` 는 **이번 주기**(현재 활성 계획 트리) 것만 넘겨야 한다 — goal 전체가 아니다.

    과거 주기의 종결 카드는 재생성 때 archive 되지 않고 영구 보존되므로(AGENTS §2, 원본
    status 불변) goal 전체를 대상으로 물으면 "종결 카드가 하나라도 있다"가 첫 주기 이후
    영원히 참이 돼버린다. 호출자가 `GoalRepo.list_nodes(goal_id, tree_kind="plan")` 의
    활성(비-archived) leaf 로 이미 좁혀서 넘겨야 이 판정이 매 주기 의미를 유지한다.

    ADR-0007 §5 의 첫 두 가드:
    ① **아직 할 날이 남은** 미종결 카드가 있으면 아직 마지막 주가 아니다.
    ② 종결 카드가 하나도 없으면(시작도 안 함) 제안하지 않는다 — 날짜만으로 판정하면
       "아직 아무것도 안 한 사용자에게 다음 2주를 열어준다"는 함정에 빠진다.
    카드 자체가 없으면(승인 직후 등) 판단 근거가 없어 제안하지 않는다.

    **①에서 `target_date < today` 인 미종결 카드를 제외하는 이유**(회귀 방지 — 이걸 빼면
    제안이 영영 안 뜬다): `expire_unreflected` cron 은 `completion_status='in_progress'` 인
    **실행(execution_events)** 을 기준으로 카드를 만료시킨다. 즉 한 번도 [▶시작] 하지 않은
    `planned` 카드는 execution_event 자체가 없어 **영원히 쓸려나가지 않는다.** 날짜 조건 없이
    "미종결 카드가 하나라도 있으면 아직"으로 두면, 체크를 흘려보내는 사용자(= 이 제품이
    도우려는 바로 그 사용자)는 2주가 지나도 밀린 `planned` 카드 한 장 때문에 다음 주기 제안을
    영영 못 받는다. 날짜가 지난 미종결 카드는 '남은 일'이 아니라 '밀린 일'이므로 — 재계획이
    다뤄야 할 대상이지 재계획을 막을 근거가 아니다(`orchestrator/replan.py` 도 과거 미착수
    블록을 '밀린 일'로 보고 배정으로 치지 않는다).

    `today` 는 호출자가 KST 기준으로 넘긴다(`now_kst().date()`) — 이 모듈은 시계를 직접 읽지
    않는다(다른 순수 함수와 같은 "DB·시계 무관" 규약).
    """
    if not action_items:
        return False
    has_remaining = any(
        a.status in _ACTIVE_ACTION_STATUSES and a.target_date >= today for a in action_items
    )
    has_done_something = any(a.status in _TERMINAL_ACTION_STATUSES for a in action_items)
    return not has_remaining and has_done_something


async def fetch_action_items_for_leaf_nodes(
    session: AsyncSession, leaf_node_ids: Sequence[uuid.UUID]
) -> list[ActionItem]:
    """활성 계획 트리 leaf 에 매달린 action_item — `should_propose_next_cycle` 입력.

    `leaf_node_ids` 는 호출자가 `GoalRepo.list_nodes(goal_id, tree_kind="plan")` 의 결과에서
    `node_type == "leaf"` 로 걸러 넘긴다 — 이미 archived_at 필터를 거친 목록이라 과거 주기
    노드는 자연히 빠져 있다.
    """
    if not leaf_node_ids:
        return []
    stmt = select(ActionItem).where(
        ActionItem.goal_node_id.in_(leaf_node_ids), ActionItem.archived_at.is_(None)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
