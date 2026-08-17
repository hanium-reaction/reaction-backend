"""회복 수락률 vs 완주율 갭 리포트의 판정 고정.

이 리포트가 답해야 하는 것은 "수락했는가" 가 아니라 "**실제로 완주했는가**" 다. 그룹마다
완주의 정의가 다르므로(`api/routes/recovery.py::_GROUP_TO_SOURCE` 가 DOWNSCOPE/CARRY_OVER
에만 파생 카드를 만든다), 여기서 못 박는 것은 단일 정의로 뭉뚱그리면 생기는 **정반대
편향**이다 — RESCHEDULE/PARK 를 파생 카드 기준으로 재면 항상 실패로 계산된다.

가드 테스트는 위반 입력을 만들어야 검증된다: "수락됐지만 아직 완주 전(pending)" 케이스를
반드시 넣는다 — 이게 바로 이 리포트가 존재하는 이유(수락 ≠ 완주)이기 때문이다.
"""

from __future__ import annotations

from uuid import uuid4

from scripts.report_recovery_followthrough import (
    AttemptRow,
    _accepted_group,
    _is_followthrough,
)


def _row(
    *,
    execution_id=None,
    group: str = "DOWNSCOPE",
    decision: str = "accepted",
    result: str = "pending",
    resulting_action_item_id=None,
) -> AttemptRow:
    return AttemptRow(
        execution_id=execution_id or uuid4(),
        user_id=uuid4(),
        option_group=group,
        user_decision=decision,
        recovery_result=result,
        resulting_action_item_id=resulting_action_item_id,
    )


# ── _accepted_group ──────────────────────────────────────────────────────


def test_accepted_group_returns_none_when_nothing_adopted() -> None:
    rows = [_row(decision="rejected"), _row(decision="pending"), _row(decision="skipped")]
    assert _accepted_group(rows) is None


def test_accepted_group_treats_edited_as_adopted() -> None:
    """`edited` 는 accepted 와 부수효과가 같다(ADOPTED_DECISION_VALUES) — 놓치면 지표에서 빠진다."""
    rows = [_row(decision="rejected", group="PARK"), _row(decision="edited", group="RESCHEDULE")]
    assert _accepted_group(rows) == "RESCHEDULE"


def test_accepted_group_finds_the_adopted_card_regardless_of_order() -> None:
    rows = [
        _row(decision="rejected", group="DOWNSCOPE"),
        _row(decision="accepted", group="PARK"),
        _row(decision="rejected", group="CARRY_OVER"),
    ]
    assert _accepted_group(rows) == "PARK"


# ── _is_followthrough — 카드 있는 그룹 (DOWNSCOPE / CARRY_OVER) ──────────


def test_card_bearing_group_followthrough_requires_derived_card_done() -> None:
    action_id = uuid4()
    rows = [_row(group="DOWNSCOPE", decision="accepted", resulting_action_item_id=action_id)]
    assert _is_followthrough(rows, derived_done_ids={action_id}) is True


def test_card_bearing_group_not_followthrough_when_derived_card_not_done() -> None:
    """파생 카드가 생겼어도(수락됨) 아직 done/over_done 이 아니면 완주가 아니다.

    이게 바로 이 리포트의 존재 이유 — '수락' 과 '완주' 를 같은 것으로 세면 안 된다.
    """
    action_id = uuid4()
    rows = [_row(group="DOWNSCOPE", decision="accepted", resulting_action_item_id=action_id)]
    assert _is_followthrough(rows, derived_done_ids=set()) is False


def test_card_bearing_group_with_no_resulting_action_item_is_not_followthrough() -> None:
    """방어적 케이스: 수락됐는데 파생 카드 id 가 없다면(정상 흐름상 없어야 하는 상태) False."""
    rows = [_row(group="CARRY_OVER", decision="accepted", resulting_action_item_id=None)]
    assert _is_followthrough(rows, derived_done_ids=set()) is False


# ── _is_followthrough — 카드 없는 그룹 (RESCHEDULE / PARK) ───────────────


def test_no_card_group_followthrough_uses_recovery_result_completed() -> None:
    rows = [_row(group="RESCHEDULE", decision="accepted", result="completed")]
    assert _is_followthrough(rows, derived_done_ids=set()) is True


def test_no_card_group_pending_is_not_followthrough() -> None:
    """수락 직후엔 recovery_result 가 기본값 'pending' 이다 — 이 상태를 완주로 세면

    수락률과 완주율이 같아져 버려 갭 리포트 자체가 무의미해진다.
    """
    rows = [_row(group="PARK", decision="accepted", result="pending")]
    assert _is_followthrough(rows, derived_done_ids=set()) is False


def test_no_card_group_abandoned_is_not_followthrough() -> None:
    rows = [_row(group="RESCHEDULE", decision="accepted", result="abandoned")]
    assert _is_followthrough(rows, derived_done_ids=set()) is False


# ── _is_followthrough — 수락 안 된 카드는 완주로 세면 안 된다 ────────────


def test_rejected_card_never_counts_even_if_derived_id_matches() -> None:
    """거절된 카드는 애초에 파생 카드를 안 만들지만, 만약 우연히 id 가 겹쳐도

    user_decision 필터가 먼저 걸려야 한다 — ADOPTED_DECISION_VALUES 체크가
    빠지면 이 테스트가 죽는다.
    """
    action_id = uuid4()
    rows = [_row(group="DOWNSCOPE", decision="rejected", resulting_action_item_id=action_id)]
    assert _is_followthrough(rows, derived_done_ids={action_id}) is False


def test_multiple_attempts_one_adopted_one_rejected() -> None:
    """한 실행에 카드가 여럿 나와도(2~4장), 그중 수락된 하나만 완주 판정에 쓴다."""
    action_id = uuid4()
    rows = [
        _row(group="PARK", decision="rejected", result="pending"),
        _row(group="RESCHEDULE", decision="accepted", result="completed"),
    ]
    assert _is_followthrough(rows, derived_done_ids={action_id}) is True
