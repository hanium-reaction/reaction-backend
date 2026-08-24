"""`cycle_proposal.should_propose_next_cycle` — 순수 함수 테스트 (ADR-0008 §8 "G").

`fetch_action_items_for_leaf_nodes` 는 `select().where(.in_())` 뿐이라(다른 모듈의 같은
모양 fetch_* 처럼) 여기선 별도 real-DB 테스트를 두지 않는다 — `mandala_adapter.
fetch_habit_instances_for_week` 등 형제 함수들과 같은 판단.
"""

from __future__ import annotations

from uuid import uuid4

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.orchestrator import cycle_proposal


def _action(status: str) -> ActionItem:
    a = ActionItem()
    a.id = uuid4()
    a.status = status
    return a


def test_no_action_items_does_not_propose() -> None:
    """카드 자체가 없으면(승인 직후 등) 판단 근거가 없어 제안하지 않는다."""
    assert cycle_proposal.should_propose_next_cycle([]) is False


def test_remaining_planned_card_blocks_proposal() -> None:
    items = [_action("done"), _action("planned")]
    assert cycle_proposal.should_propose_next_cycle(items) is False


def test_remaining_in_progress_card_blocks_proposal() -> None:
    items = [_action("done"), _action("in_progress")]
    assert cycle_proposal.should_propose_next_cycle(items) is False


def test_no_terminal_card_does_not_propose() -> None:
    """시작도 안 했으면(planned 만 있음) 제안하지 않는다 — 날짜 함정 방지(ADR-0007 §5)."""
    items = [_action("planned"), _action("planned")]
    assert cycle_proposal.should_propose_next_cycle(items) is False


def test_all_terminal_and_at_least_one_success_proposes() -> None:
    items = [_action("done"), _action("over_done"), _action("failed")]
    assert cycle_proposal.should_propose_next_cycle(items) is True


def test_partial_done_and_failed_only_still_proposes() -> None:
    """전부 종결이면 성공/실패 구성과 무관하게 제안 — 판단은 "남았나"이지 "잘했나"가 아니다."""
    items = [_action("partial_done"), _action("failed")]
    assert cycle_proposal.should_propose_next_cycle(items) is True
