"""연속 실패 에스컬레이션 회귀 (근거 대장 §5 L0~L2).

세션 없는 순수 함수라 이력을 손으로 구성해 직접 검증한다 — `orchestrator/recovery.py`
의 `select_strategies` 테스트와 같은 스타일.
"""

from __future__ import annotations

import pytest

from reaction_backend.orchestrator.escalation import (
    L1_CONSECUTIVE_FAILURE_THRESHOLD,
    L1_RECOVERY_ABANDONED_THRESHOLD,
    L2_SAME_TAG_FAILURE_THRESHOLD,
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


class TestDetermineEscalationLevel:
    def test_zero_counters_is_l0(self) -> None:
        counters = EscalationCounters(0, 0, 0, 0)
        assert determine_escalation_level(counters) == "L0"

    def test_one_below_l1_threshold_stays_l0(self) -> None:
        counters = EscalationCounters(
            consecutive_failure_count=L1_CONSECUTIVE_FAILURE_THRESHOLD - 1,
            same_tag_failure_count=0,
            recovery_rejected_streak=0,
            recovery_abandoned_streak=0,
        )
        assert determine_escalation_level(counters) == "L0"

    def test_consecutive_failure_at_threshold_is_l1(self) -> None:
        counters = EscalationCounters(
            consecutive_failure_count=L1_CONSECUTIVE_FAILURE_THRESHOLD,
            same_tag_failure_count=0,
            recovery_rejected_streak=0,
            recovery_abandoned_streak=0,
        )
        assert determine_escalation_level(counters) == "L1"

    def test_abandoned_streak_alone_at_threshold_is_l1(self) -> None:
        """L1 은 OR 조건 — consecutive_failure 가 0 이어도 abandoned 1회면 L1."""
        counters = EscalationCounters(
            consecutive_failure_count=0,
            same_tag_failure_count=0,
            recovery_rejected_streak=0,
            recovery_abandoned_streak=L1_RECOVERY_ABANDONED_THRESHOLD,
        )
        assert determine_escalation_level(counters) == "L1"

    def test_same_tag_at_threshold_is_l2(self) -> None:
        counters = EscalationCounters(
            consecutive_failure_count=0,
            same_tag_failure_count=L2_SAME_TAG_FAILURE_THRESHOLD,
            recovery_rejected_streak=0,
            recovery_abandoned_streak=0,
        )
        assert determine_escalation_level(counters) == "L2"

    def test_l2_condition_wins_even_when_l1_conditions_also_met(self) -> None:
        counters = EscalationCounters(
            consecutive_failure_count=5,
            same_tag_failure_count=L2_SAME_TAG_FAILURE_THRESHOLD,
            recovery_rejected_streak=0,
            recovery_abandoned_streak=3,
        )
        assert determine_escalation_level(counters) == "L2"

    def test_recovery_rejected_streak_alone_does_not_escalate(self) -> None:
        """§5.2 에 명시된 L1/L2 조건에 recovery_rejected_streak 은 없다 — 계산은
        하지만(향후 sustain talk 가드 등에 쓰일 수 있음) 레벨 판정에는 안 쓴다.
        """
        counters = EscalationCounters(
            consecutive_failure_count=0,
            same_tag_failure_count=0,
            recovery_rejected_streak=99,
            recovery_abandoned_streak=0,
        )
        assert determine_escalation_level(counters) == "L0"


class TestComputeEscalationState:
    def test_wires_all_four_histories_into_counters_and_level(self) -> None:
        state = compute_escalation_state(
            same_card_outcomes_most_recent_first=["failed", "failed"],
            same_tag_outcomes_most_recent_first=["failed"],
            recovery_decisions_most_recent_first=["rejected"],
            recovery_results_most_recent_first=["abandoned"],
        )

        assert state.counters == EscalationCounters(
            consecutive_failure_count=2,
            same_tag_failure_count=1,
            recovery_rejected_streak=1,
            recovery_abandoned_streak=1,
        )
        # consecutive_failure_count=2 → L1 (abandoned_streak=1 도 별개로 L1 조건 충족).
        assert state.level == "L1"

    def test_clean_history_is_l0(self) -> None:
        state = compute_escalation_state(
            same_card_outcomes_most_recent_first=["done"],
            same_tag_outcomes_most_recent_first=[],
            recovery_decisions_most_recent_first=[],
            recovery_results_most_recent_first=[],
        )
        assert state.level == "L0"


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
