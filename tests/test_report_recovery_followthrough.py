"""회복 수락률 vs 완주율 갭 리포트의 판정 고정.

이 리포트가 답해야 하는 것은 "수락했는가" 가 아니라 "**실제로 완주했는가**" 다. 그룹마다
완주의 정의가 다르므로(`api/routes/recovery.py::_GROUP_TO_SOURCE` 가 DOWNSCOPE/CARRY_OVER
에만 파생 카드를 만든다), 여기서 못 박는 것은 단일 정의로 뭉뚱그리면 생기는 **정반대
편향**이다 — RESCHEDULE/PARK 를 파생 카드 기준으로 재면 항상 실패로 계산된다.

⚠️ **`recovery_attempts.recovery_result` 는 더 이상 쓰지 않는다.** RESCHEDULE/PARK 는
파생 카드가 없어 `RecoveryRepo.complete_for_action`(매칭 키가 `resulting_action_item_id`)
이 그 컬럼을 절대 못 채운다 — 영구 'pending'. 이 컬럼을 완주 신호로 쓰던 이전 버전은
RESCHEDULE/PARK 수락을 **항상 미완주로 계산**하고 있었다(수락률 편향을 고치려다 반대
편향을 만든 사고). 지금은 원본 카드/goal 계보의 **실제 실행 이력**으로 판정한다 — 그래서
`AttemptRow` 에 `recovery_result` 필드가 아예 없다(타입으로 회귀를 막는다).

가드 테스트는 위반 입력을 만들어야 검증된다: "수락됐지만 아직 완주 전(성공 실행 없음)"
케이스를 반드시 넣는다 — 이게 바로 이 리포트가 존재하는 이유(수락 ≠ 완주)이기 때문이다.
같은 이유로 분모 게이트(`_was_exposed`)도 `first_viewed_at=None` 인 행을 **직접 만들어**
고정한다 — 노출된 행만 넣으면 게이트를 통째로 지워도 테스트가 초록이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from scripts.report_recovery_followthrough import (
    AttemptRow,
    _accepted_group,
    _is_followthrough,
    _park_followthrough,
    _reschedule_followthrough,
    _was_exposed,
)

_NOW = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)


def _row(
    *,
    execution_id=None,
    group: str = "DOWNSCOPE",
    decision: str = "accepted",
    resulting_action_item_id=None,
    decided_at: datetime | None = _NOW,
    original_action_item_id=None,
    original_goal_id=None,
    first_viewed_at: datetime | None = _NOW,
    re_engagement_anchor_at: datetime | None = None,
) -> AttemptRow:
    return AttemptRow(
        execution_id=execution_id or uuid4(),
        user_id=uuid4(),
        option_group=group,
        user_decision=decision,
        resulting_action_item_id=resulting_action_item_id,
        recovery_decided_at=decided_at,
        original_action_item_id=original_action_item_id or uuid4(),
        original_goal_id=original_goal_id,
        first_viewed_at=first_viewed_at,
        re_engagement_anchor_at=re_engagement_anchor_at,
    )


# ── _was_exposed — ITT 분모 게이트 ───────────────────────────────────────


def test_unexposed_execution_is_excluded_from_denominator() -> None:
    """위반 입력: 카드가 만들어지기만 하고 **한 번도 응답으로 안 나간** 실행.

    이걸 분모에 넣으면 "회복 기회를 줬는데 안 했다"로 세어 완주율이 구조적으로
    과소평가된다. 정상 데이터만 넣는 테스트로는 게이트를 `return True` 로 바꿔도
    초록이라, NULL 행을 실제로 만들어야 이 가드가 검증된다.
    """
    rows = [
        _row(group="DOWNSCOPE", first_viewed_at=None),
        _row(group="RESCHEDULE", first_viewed_at=None),
    ]
    assert _was_exposed(rows) is False


def test_execution_counts_when_any_card_was_viewed() -> None:
    """카드 2~4장은 같은 응답으로 함께 나가므로 한 장만 스탬프돼 있어도 노출이다.

    (스탬프는 `stamp_first_viewed` 가 최초 1회만 찍는다 — 멱등 재조회로도 안 덮인다.)
    """
    rows = [
        _row(group="DOWNSCOPE", first_viewed_at=None),
        _row(group="PARK", first_viewed_at=_NOW),
    ]
    assert _was_exposed(rows) is True


def test_exposure_gate_ignores_decision_state() -> None:
    """노출 여부는 분모(기회를 줬는가)이고 결정은 분자 쪽이다 — 섞이면 안 된다.

    거절/미결정 카드도 '노출은 됐다'로 분모에 남아야 수락률·완주율이 ITT 로 유지된다.
    """
    assert _was_exposed([_row(decision="rejected", first_viewed_at=_NOW)]) is True
    assert _was_exposed([_row(decision="pending", first_viewed_at=_NOW)]) is True
    assert _was_exposed([_row(decision="accepted", first_viewed_at=None)]) is False


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
    assert (
        _is_followthrough(
            rows,
            derived_done_ids={action_id},
            reschedule_success={},
            park_success_by_goal={},
        )
        is True
    )


def test_card_bearing_group_not_followthrough_when_derived_card_not_done() -> None:
    """파생 카드가 생겼어도(수락됨) 아직 done/over_done 이 아니면 완주가 아니다.

    이게 바로 이 리포트의 존재 이유 — '수락' 과 '완주' 를 같은 것으로 세면 안 된다.
    """
    action_id = uuid4()
    rows = [_row(group="DOWNSCOPE", decision="accepted", resulting_action_item_id=action_id)]
    assert (
        _is_followthrough(
            rows, derived_done_ids=set(), reschedule_success={}, park_success_by_goal={}
        )
        is False
    )


def test_card_bearing_group_with_no_resulting_action_item_is_not_followthrough() -> None:
    """방어적 케이스: 수락됐는데 파생 카드 id 가 없다면(정상 흐름상 없어야 하는 상태) False."""
    rows = [_row(group="CARRY_OVER", decision="accepted", resulting_action_item_id=None)]
    assert (
        _is_followthrough(
            rows, derived_done_ids=set(), reschedule_success={}, park_success_by_goal={}
        )
        is False
    )


# ── _reschedule_followthrough ────────────────────────────────────────────


def test_reschedule_followthrough_true_when_success_after_decision() -> None:
    action_id = uuid4()
    success = {action_id: [_NOW + timedelta(hours=3)]}
    assert _reschedule_followthrough(_NOW, action_id, success) is True


def test_reschedule_followthrough_false_when_success_is_before_decision() -> None:
    """결정 이전의 과거 성공 실행은 이번 회복의 완주가 아니다 — 방향성 필수."""
    action_id = uuid4()
    success = {action_id: [_NOW - timedelta(days=10)]}
    assert _reschedule_followthrough(_NOW, action_id, success) is False


def test_reschedule_followthrough_false_when_no_success_recorded() -> None:
    action_id = uuid4()
    assert _reschedule_followthrough(_NOW, action_id, {}) is False


def test_reschedule_followthrough_false_when_decided_at_missing() -> None:
    """이론상 없어야 하지만(채택 시 항상 채워짐) 방어적으로 None 이면 완주로 세지 않는다."""
    action_id = uuid4()
    success = {action_id: [_NOW + timedelta(hours=1)]}
    assert _reschedule_followthrough(None, action_id, success) is False


# ── _park_followthrough ──────────────────────────────────────────────────


def test_park_followthrough_true_within_seven_day_window() -> None:
    goal_id = uuid4()
    success_by_goal = {goal_id: [_NOW + timedelta(days=3)]}
    assert _park_followthrough(_NOW, goal_id, success_by_goal) is True


def test_park_followthrough_false_after_seven_day_window() -> None:
    goal_id = uuid4()
    success_by_goal = {goal_id: [_NOW + timedelta(days=8)]}
    assert _park_followthrough(_NOW, goal_id, success_by_goal) is False


def test_park_followthrough_false_when_success_before_anchor() -> None:
    """앵커(결정 시각) 이전의 성공은 이번 회복과 무관 — 오래된 완주를 재활용하지 않는다."""
    goal_id = uuid4()
    success_by_goal = {goal_id: [_NOW - timedelta(hours=1)]}
    assert _park_followthrough(_NOW, goal_id, success_by_goal) is False


def test_park_followthrough_false_when_original_card_has_no_goal() -> None:
    """습관/인박스/수동 출처는 goal 계보가 없어 판정 불가 — 완주로 잘못 세지 않는다."""
    success_by_goal = {uuid4(): [_NOW + timedelta(days=1)]}
    assert _park_followthrough(_NOW, None, success_by_goal) is False


# ── _is_followthrough — 카드 없는 그룹 (RESCHEDULE / PARK) ───────────────


def test_no_card_group_reschedule_followthrough_via_original_action_item() -> None:
    action_id = uuid4()
    rows = [
        _row(
            group="RESCHEDULE",
            decision="accepted",
            original_action_item_id=action_id,
            decided_at=_NOW,
        )
    ]
    assert (
        _is_followthrough(
            rows,
            derived_done_ids=set(),
            reschedule_success={action_id: [_NOW + timedelta(hours=2)]},
            park_success_by_goal={},
        )
        is True
    )


def test_no_card_group_reschedule_pending_is_not_followthrough() -> None:
    """수락 직후엔 아직 재실행이 없다 — 이 상태를 완주로 세면 갭 리포트가 무의미해진다."""
    action_id = uuid4()
    rows = [_row(group="RESCHEDULE", decision="accepted", original_action_item_id=action_id)]
    assert (
        _is_followthrough(
            rows, derived_done_ids=set(), reschedule_success={}, park_success_by_goal={}
        )
        is False
    )


def test_no_card_group_park_followthrough_via_goal_lineage() -> None:
    goal_id = uuid4()
    rows = [_row(group="PARK", decision="accepted", original_goal_id=goal_id, decided_at=_NOW)]
    assert (
        _is_followthrough(
            rows,
            derived_done_ids=set(),
            reschedule_success={},
            park_success_by_goal={goal_id: [_NOW + timedelta(days=2)]},
        )
        is True
    )


def test_no_card_group_park_pending_is_not_followthrough() -> None:
    goal_id = uuid4()
    rows = [_row(group="PARK", decision="accepted", original_goal_id=goal_id)]
    assert (
        _is_followthrough(
            rows, derived_done_ids=set(), reschedule_success={}, park_success_by_goal={}
        )
        is False
    )


def test_no_card_group_park_prefers_real_anchor_over_decided_at() -> None:
    """S8(#336) 이후 결정 건은 `re_engagement_anchor_at`(다음 주 월요일)을 창 시작점으로
    쓴다 — `recovery_decided_at`(결정 시각) 기준이면 놓칠 완주를 실제 앵커 기준으로는 잡는다.
    """
    goal_id = uuid4()
    anchor = _NOW + timedelta(days=4)  # 결정 시각보다 한참 뒤(예: 다음 주 월요일)
    success_at = anchor + timedelta(days=1)  # decided_at(NOW) 기준 7일 창은 이미 지났다고 가정
    rows = [
        _row(
            group="PARK",
            decision="accepted",
            original_goal_id=goal_id,
            decided_at=_NOW,
            re_engagement_anchor_at=anchor,
        )
    ]
    assert (
        _is_followthrough(
            rows,
            derived_done_ids=set(),
            reschedule_success={},
            park_success_by_goal={goal_id: [success_at]},
        )
        is True
    )


def test_no_card_group_park_falls_back_to_decided_at_when_anchor_missing() -> None:
    """S8 이전에 결정된(마이그레이션 이전) 행은 `re_engagement_anchor_at` 이 NULL —
    옛 근사(`recovery_decided_at`)로 계속 판정한다. 데이터를 조용히 잃지 않는다.
    """
    goal_id = uuid4()
    rows = [
        _row(
            group="PARK",
            decision="accepted",
            original_goal_id=goal_id,
            decided_at=_NOW,
            re_engagement_anchor_at=None,
        )
    ]
    assert (
        _is_followthrough(
            rows,
            derived_done_ids=set(),
            reschedule_success={},
            park_success_by_goal={goal_id: [_NOW + timedelta(days=2)]},
        )
        is True
    )


# ── _is_followthrough — 수락 안 된 카드는 완주로 세면 안 된다 ────────────


def test_rejected_card_never_counts_even_if_derived_id_matches() -> None:
    """거절된 카드는 애초에 파생 카드를 안 만들지만, 만약 우연히 id 가 겹쳐도

    user_decision 필터가 먼저 걸려야 한다 — ADOPTED_DECISION_VALUES 체크가
    빠지면 이 테스트가 죽는다.
    """
    action_id = uuid4()
    rows = [_row(group="DOWNSCOPE", decision="rejected", resulting_action_item_id=action_id)]
    assert (
        _is_followthrough(
            rows,
            derived_done_ids={action_id},
            reschedule_success={},
            park_success_by_goal={},
        )
        is False
    )


def test_multiple_attempts_one_adopted_one_rejected() -> None:
    """한 실행에 카드가 여럿 나와도(2~4장), 그중 수락된 하나만 완주 판정에 쓴다."""
    action_id = uuid4()
    goal_id = uuid4()
    rows = [
        _row(group="PARK", decision="rejected", original_goal_id=goal_id),
        _row(
            group="RESCHEDULE",
            decision="accepted",
            original_action_item_id=action_id,
            decided_at=_NOW,
        ),
    ]
    assert (
        _is_followthrough(
            rows,
            derived_done_ids=set(),
            reschedule_success={action_id: [_NOW + timedelta(hours=1)]},
            park_success_by_goal={},
        )
        is True
    )
