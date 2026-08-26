"""연속 실패 에스컬레이션 회귀 (근거 대장 §5 L0~L3).

세션 없는 순수 함수라 이력을 손으로 구성해 직접 검증한다 — `orchestrator/recovery.py`
의 `select_strategies` 테스트와 같은 스타일.
"""

from __future__ import annotations

import pytest

from reaction_backend.orchestrator.escalation import (
    L1_CONSECUTIVE_FAILURE_THRESHOLD,
    L1_RECOVERY_ABANDONED_THRESHOLD,
    L2_SAME_TAG_FAILURE_THRESHOLD,
    L3_GOAL_FAILURE_THRESHOLD,
    L3_REJECTED_STREAK_THRESHOLD,
    EscalationCounters,
    compute_consecutive_failure_count,
    compute_escalation_state,
    compute_recovery_abandoned_streak,
    compute_recovery_rejected_streak,
    determine_escalation_level,
)


class TestComputeConsecutiveFailureCount:
    def test_empty_history_is_zero(self) -> None:
        assert compute_consecutive_failure_count([]) == 0

    def test_most_recent_done_resets_to_zero_even_with_older_failures(self) -> None:
        assert compute_consecutive_failure_count(["done", "failed", "failed"]) == 0

    def test_over_done_also_resets(self) -> None:
        assert compute_consecutive_failure_count(["over_done", "failed"]) == 0

    def test_counts_consecutive_failed_until_done(self) -> None:
        assert compute_consecutive_failure_count(["failed", "failed", "failed", "done"]) == 3

    def test_all_failed_no_terminator_counts_everything(self) -> None:
        assert compute_consecutive_failure_count(["failed", "failed"]) == 2

    def test_partial_done_is_frozen_not_counted_and_does_not_break_streak(self) -> None:
        """동결 — partial_done 은 세지도 않고, 그 뒤(더 과거)의 failed 도 계속 이어서 센다."""
        history = ["failed", "partial_done", "failed", "partial_done", "partial_done", "failed"]
        assert compute_consecutive_failure_count(history) == 3

    def test_partial_done_streak_never_terminates_without_done(self) -> None:
        """매일 조금씩만 하고 마는 사용자가 영원히 카운터 0 인 사각지대를 막는다는
        설계 의도(원문 근거)를 직접 검증 — partial_done 만 반복돼도 실패가 있으면 센다.
        """
        history = ["partial_done"] * 10 + ["failed", "failed"]
        assert compute_consecutive_failure_count(history) == 2

    def test_leading_partial_done_does_not_hide_a_done_reset_further_back(self) -> None:
        history = ["partial_done", "done", "failed", "failed"]
        assert compute_consecutive_failure_count(history) == 0


class TestComputeRecoveryRejectedStreak:
    def test_empty_is_zero(self) -> None:
        assert compute_recovery_rejected_streak([]) == 0

    def test_accepted_resets(self) -> None:
        assert compute_recovery_rejected_streak(["accepted", "rejected", "rejected"]) == 0

    def test_edited_also_resets(self) -> None:
        assert compute_recovery_rejected_streak(["edited", "skipped"]) == 0

    def test_counts_rejected_and_skipped_together(self) -> None:
        assert compute_recovery_rejected_streak(["rejected", "skipped", "rejected"]) == 3


class TestComputeRecoveryAbandonedStreak:
    def test_empty_is_zero(self) -> None:
        assert compute_recovery_abandoned_streak([]) == 0

    def test_completed_resets(self) -> None:
        assert compute_recovery_abandoned_streak(["completed", "abandoned"]) == 0

    def test_counts_abandoned(self) -> None:
        assert compute_recovery_abandoned_streak(["abandoned", "abandoned"]) == 2


def _counters(
    *,
    consecutive_failure_count: int = 0,
    same_tag_failure_count: int = 0,
    same_goal_failure_count: int = 0,
    recovery_rejected_streak: int = 0,
    recovery_abandoned_streak: int = 0,
) -> EscalationCounters:
    return EscalationCounters(
        consecutive_failure_count=consecutive_failure_count,
        same_tag_failure_count=same_tag_failure_count,
        same_goal_failure_count=same_goal_failure_count,
        recovery_rejected_streak=recovery_rejected_streak,
        recovery_abandoned_streak=recovery_abandoned_streak,
    )


class TestDetermineEscalationLevel:
    def test_zero_counters_is_l0(self) -> None:
        assert determine_escalation_level(_counters()) == "L0"

    def test_one_below_l1_threshold_stays_l0(self) -> None:
        counters = _counters(consecutive_failure_count=L1_CONSECUTIVE_FAILURE_THRESHOLD - 1)
        assert determine_escalation_level(counters) == "L0"

    def test_consecutive_failure_at_threshold_is_l1(self) -> None:
        counters = _counters(consecutive_failure_count=L1_CONSECUTIVE_FAILURE_THRESHOLD)
        assert determine_escalation_level(counters) == "L1"

    def test_abandoned_streak_alone_at_threshold_is_l1(self) -> None:
        """L1 은 OR 조건 — consecutive_failure 가 0 이어도 abandoned 1회면 L1."""
        counters = _counters(recovery_abandoned_streak=L1_RECOVERY_ABANDONED_THRESHOLD)
        assert determine_escalation_level(counters) == "L1"

    def test_same_tag_at_threshold_is_l2(self) -> None:
        counters = _counters(same_tag_failure_count=L2_SAME_TAG_FAILURE_THRESHOLD)
        assert determine_escalation_level(counters) == "L2"

    def test_l2_condition_wins_even_when_l1_conditions_also_met(self) -> None:
        counters = _counters(
            consecutive_failure_count=5,
            same_tag_failure_count=L2_SAME_TAG_FAILURE_THRESHOLD,
            recovery_abandoned_streak=3,
        )
        assert determine_escalation_level(counters) == "L2"

    def test_same_goal_at_threshold_is_l3(self) -> None:
        counters = _counters(same_goal_failure_count=L3_GOAL_FAILURE_THRESHOLD)
        assert determine_escalation_level(counters) == "L3"

    def test_one_below_l3_goal_threshold_stays_below_l3(self) -> None:
        counters = _counters(same_goal_failure_count=L3_GOAL_FAILURE_THRESHOLD - 1)
        assert determine_escalation_level(counters) == "L0"

    def test_rejected_streak_at_threshold_is_l3(self) -> None:
        """§5.2 L3 — "회복 2회 연속 rejected" 단독으로도 진입(OR 조건, L1 과 같은 형태)."""
        counters = _counters(recovery_rejected_streak=L3_REJECTED_STREAK_THRESHOLD)
        assert determine_escalation_level(counters) == "L3"

    def test_one_below_l3_rejected_threshold_does_not_escalate(self) -> None:
        counters = _counters(recovery_rejected_streak=L3_REJECTED_STREAK_THRESHOLD - 1)
        assert determine_escalation_level(counters) == "L0"

    def test_l3_condition_wins_even_when_l1_and_l2_conditions_also_met(self) -> None:
        """ "순서의 근거" — 재협상(L3)이 단서 전환(L2)·축소분해(L1)보다 강한 개입이라
        먼저 검사된다. 셋 다 동시에 참이어도 L3 가 이긴다."""
        counters = _counters(
            consecutive_failure_count=5,
            same_tag_failure_count=L2_SAME_TAG_FAILURE_THRESHOLD,
            same_goal_failure_count=L3_GOAL_FAILURE_THRESHOLD,
            recovery_abandoned_streak=3,
        )
        assert determine_escalation_level(counters) == "L3"


class TestComputeEscalationState:
    def test_wires_all_five_histories_into_counters_and_level(self) -> None:
        state = compute_escalation_state(
            same_card_outcomes_most_recent_first=["failed", "failed"],
            same_tag_outcomes_most_recent_first=["failed"],
            same_goal_outcomes_most_recent_first=["failed"],
            recovery_decisions_most_recent_first=["rejected"],
            recovery_results_most_recent_first=["abandoned"],
        )

        assert state.counters == _counters(
            consecutive_failure_count=2,
            same_tag_failure_count=1,
            same_goal_failure_count=1,
            recovery_rejected_streak=1,
            recovery_abandoned_streak=1,
        )
        # consecutive_failure_count=2 → L1 (abandoned_streak=1 도 별개로 L1 조건 충족).
        # 나머지(same_tag=1, same_goal=1, rejected=1)는 전부 각 레벨 임계 미달.
        assert state.level == "L1"

    def test_clean_history_is_l0(self) -> None:
        state = compute_escalation_state(
            same_card_outcomes_most_recent_first=["done"],
            same_tag_outcomes_most_recent_first=[],
            same_goal_outcomes_most_recent_first=[],
            recovery_decisions_most_recent_first=[],
            recovery_results_most_recent_first=[],
        )
        assert state.level == "L0"

    def test_goal_failure_alone_wires_to_l3(self) -> None:
        state = compute_escalation_state(
            same_card_outcomes_most_recent_first=["failed"],
            same_tag_outcomes_most_recent_first=["failed"],
            same_goal_outcomes_most_recent_first=["failed"] * L3_GOAL_FAILURE_THRESHOLD,
            recovery_decisions_most_recent_first=[],
            recovery_results_most_recent_first=[],
        )
        assert state.counters.same_goal_failure_count == L3_GOAL_FAILURE_THRESHOLD
        assert state.level == "L3"

    def test_rejected_decisions_alone_wires_to_l3(self) -> None:
        state = compute_escalation_state(
            same_card_outcomes_most_recent_first=[],
            same_tag_outcomes_most_recent_first=[],
            same_goal_outcomes_most_recent_first=[],
            recovery_decisions_most_recent_first=["rejected"] * L3_REJECTED_STREAK_THRESHOLD,
            recovery_results_most_recent_first=[],
        )
        assert state.level == "L3"


@pytest.mark.parametrize(
    "history",
    [
        [],
        ["failed"],
        ["done"],
        ["partial_done"],
        ["failed", "partial_done", "over_done"],
    ],
)
def test_consecutive_failure_count_never_negative(history: list[str]) -> None:
    assert compute_consecutive_failure_count(history) >= 0  # type: ignore[arg-type]
