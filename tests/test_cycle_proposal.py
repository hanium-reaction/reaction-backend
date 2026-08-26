"""`cycle_proposal` 의 순수 함수 테스트 — 만다라 2주(ADR-0008 §8 "G") + 일반형(ADR-0007 PR-4).

`fetch_action_items_for_leaf_nodes` 는 `select().where(.in_())` 뿐이라(다른 모듈의 같은
모양 fetch_* 처럼) 여기선 별도 real-DB 테스트를 두지 않는다 — `mandala_adapter.
fetch_habit_instances_for_week` 등 형제 함수들과 같은 판단. `fetch_goals_with_milestones`
는 join + 다중 WHERE 라 `test_cycle_proposal_real_db.py` 에서 실 Postgres 로 검증한다.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.orchestrator import cycle_proposal
from reaction_backend.schemas.common import now_kst

TODAY = date(2026, 8, 25)
TOMORROW = TODAY + timedelta(days=1)
YESTERDAY = TODAY - timedelta(days=1)


def _action(status: str, *, target_date: date = TOMORROW) -> ActionItem:
    """기본값은 **아직 안 지난** 카드 — 날짜를 안 쓰는 테스트의 의도를 바꾸지 않기 위해서."""
    a = ActionItem()
    a.id = uuid4()
    a.status = status
    a.target_date = target_date
    return a


def test_no_action_items_does_not_propose() -> None:
    """카드 자체가 없으면(승인 직후 등) 판단 근거가 없어 제안하지 않는다."""
    assert cycle_proposal.should_propose_next_cycle([], today=TODAY) is False


def test_remaining_planned_card_blocks_proposal() -> None:
    items = [_action("done"), _action("planned")]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is False


def test_remaining_in_progress_card_blocks_proposal() -> None:
    items = [_action("done"), _action("in_progress")]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is False


def test_no_terminal_card_does_not_propose() -> None:
    """시작도 안 했으면(planned 만 있음) 제안하지 않는다 — 날짜 함정 방지(ADR-0007 §5)."""
    items = [_action("planned"), _action("planned")]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is False


def test_all_terminal_and_at_least_one_success_proposes() -> None:
    items = [_action("done"), _action("over_done"), _action("failed")]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is True


def test_partial_done_and_failed_only_still_proposes() -> None:
    """전부 종결이면 성공/실패 구성과 무관하게 제안 — 판단은 "남았나"이지 "잘했나"가 아니다."""
    items = [_action("partial_done"), _action("failed")]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is True


# ── 밀린 카드는 '남은 카드'가 아니다 (회귀 방지) ──


def test_overdue_planned_card_does_not_block_proposal() -> None:
    """**핵심 회귀 테스트.** 날짜가 지난 `planned` 카드는 제안을 막지 않는다.

    이 가드가 없으면: `expire_unreflected` cron 이 `completion_status='in_progress'` 인 실행만
    쓸어내므로, 한 번도 [▶시작] 하지 않은 `planned` 카드는 execution_event 자체가 없어 영원히
    남는다 → 밀린 카드 한 장 때문에 다음 주기 제안이 **영영 안 뜬다**.
    """
    items = [_action("done"), _action("planned", target_date=YESTERDAY)]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is True


def test_overdue_in_progress_card_does_not_block_proposal() -> None:
    """`in_progress` 도 같은 규칙 — 날짜가 지났으면 '밀린 일'이다.

    실무상 이 카드는 만료 cron 이 3일 안에 archive 하지만(그러면 입력에서 아예 빠진다),
    `SCHEDULER_ENABLED=false`(기본값) 환경에서는 안 쓸린다. 날짜 기준으로 판정해 cron 가동
    여부에 결과가 좌우되지 않게 한다.
    """
    items = [_action("done"), _action("in_progress", target_date=YESTERDAY)]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is True


def test_card_due_today_still_blocks_proposal() -> None:
    """경계 — 오늘이 마감인 카드는 아직 '남은 일'이다(`>= today`)."""
    items = [_action("done"), _action("planned", target_date=TODAY)]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is False


def test_all_overdue_and_nothing_done_still_does_not_propose() -> None:
    """가드②는 그대로 — 전부 밀렸고 끝낸 게 하나도 없으면 제안하지 않는다.

    "아직 아무것도 안 한 사용자에게 다음 2주를 열어준다"는 함정을 날짜 가드가 뚫으면 안 된다.
    이 사용자에게 필요한 건 새 2주가 아니라 지금 계획의 조정이다.
    """
    items = [
        _action("planned", target_date=YESTERDAY),
        _action("planned", target_date=YESTERDAY),
    ]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is False


def test_mixed_overdue_and_future_cards_block_on_the_future_one() -> None:
    """밀린 카드가 있어도 **아직 안 지난** 카드가 하나라도 있으면 이번 주기는 안 끝났다."""
    items = [
        _action("done"),
        _action("planned", target_date=YESTERDAY),
        _action("planned", target_date=TOMORROW),
    ]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is False


# ── has_open_milestone 가드 (ADR-0007 §5 세 번째 가드, PR-4 일반형) ──


def test_has_open_milestone_default_does_not_gate_mandala_scope() -> None:
    """기본값(생략)은 가드 비활성 — 만다라 2주 스코프(G)는 마일스톤이 없어도 판정이 그대로다."""
    items = [_action("done"), _action("failed")]
    assert cycle_proposal.should_propose_next_cycle(items, today=TODAY) is True


def test_no_open_milestone_blocks_even_when_cards_are_all_done() -> None:
    """열린 마일스톤이 없으면(전부 완료) '다음 주기'가 아니라 '목표 완료 확인' 대상이다."""
    items = [_action("done"), _action("over_done")]
    assert (
        cycle_proposal.should_propose_next_cycle(items, today=TODAY, has_open_milestone=False)
        is False
    )


def test_open_milestone_present_proposes_normally() -> None:
    items = [_action("done"), _action("over_done")]
    assert (
        cycle_proposal.should_propose_next_cycle(items, today=TODAY, has_open_milestone=True)
        is True
    )


def _milestone(*, completed: bool) -> GoalNode:
    n = GoalNode()
    n.id = uuid4()
    n.node_type = "milestone"
    n.completed_at = now_kst() if completed else None
    return n


def test_has_open_milestone_true_when_any_incomplete() -> None:
    milestones = [_milestone(completed=True), _milestone(completed=False)]
    assert cycle_proposal.has_open_milestone(milestones) is True


def test_has_open_milestone_false_when_all_complete() -> None:
    milestones = [_milestone(completed=True), _milestone(completed=True)]
    assert cycle_proposal.has_open_milestone(milestones) is False


def test_has_open_milestone_false_when_empty() -> None:
    assert cycle_proposal.has_open_milestone([]) is False
