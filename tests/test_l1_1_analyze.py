"""L1-1 분석 회귀 — swap consistency 정리, 승률/부트스트랩 CI, 태그 층 분해, AND-게이트."""

from __future__ import annotations

import pytest
from scripts.l1_1_analyze import (
    ResolvedOutcome,
    _print_report,
    cluster_bootstrap_ci,
    disqualification_rates,
    resolve_swap_consistency,
    tag_stratum_win_rates,
    win_rate,
)
from scripts.l1_1_common import JudgmentRow


def _judgment(
    case_id: str,
    pair: str,
    rep_index: int,
    *,
    swap: bool,
    version_a: str,
    version_b: str,
    axis_a: tuple[int, int, int, int, int],
    axis_b: tuple[int, int, int, int, int],
) -> JudgmentRow:
    return JudgmentRow(
        case_id=case_id,
        pair=pair,
        rep_index=rep_index,
        swap=swap,
        version_a=version_a,
        version_b=version_b,
        axis_a=axis_a,
        axis_b=axis_b,
        disqualification_reason=None,
    )


class TestResolveSwapConsistency:
    def test_consistent_pair_is_resolved_to_the_agreed_winner(self) -> None:
        # 정방향: A=v1(약함), B=v3(강함) → v3 승. 역방향: A=v3(강함), B=v1(약함) → 여전히 v3 승.
        forward = _judgment(
            "c1",
            "1-3",
            0,
            swap=False,
            version_a="1",
            version_b="3",
            axis_a=(2, 2, 2, 2, 2),
            axis_b=(4, 4, 4, 4, 4),
        )
        reversed_ = _judgment(
            "c1",
            "1-3",
            0,
            swap=True,
            version_a="3",
            version_b="1",
            axis_a=(4, 4, 4, 4, 4),
            axis_b=(2, 2, 2, 2, 2),
        )

        result = resolve_swap_consistency([forward, reversed_])

        assert result.total_pairs_with_both_directions == 1
        assert result.inconsistent_n == 0
        assert result.resolved == [ResolvedOutcome(case_id="c1", pair="1-3", winner_version="3")]
        assert result.consistency_rate == 1.0

    def test_inconsistent_pair_is_excluded_from_resolved(self) -> None:
        # 정방향은 v3 승, 역방향은 v1 승 — 제시 순서에 따라 결론이 바뀌었다(불일치).
        forward = _judgment(
            "c1",
            "1-3",
            0,
            swap=False,
            version_a="1",
            version_b="3",
            axis_a=(2, 2, 2, 2, 2),
            axis_b=(4, 4, 4, 4, 4),
        )
        reversed_ = _judgment(
            "c1",
            "1-3",
            0,
            swap=True,
            version_a="3",
            version_b="1",
            axis_a=(2, 2, 2, 2, 2),
            axis_b=(4, 4, 4, 4, 4),
        )

        result = resolve_swap_consistency([forward, reversed_])

        assert result.resolved == []
        assert result.inconsistent_n == 1
        assert result.total_pairs_with_both_directions == 1
        assert result.consistency_rate == 0.0

    def test_both_directions_draw_counts_as_consistent(self) -> None:
        forward = _judgment(
            "c1",
            "1-3",
            0,
            swap=False,
            version_a="1",
            version_b="3",
            axis_a=(3, 3, 3, 3, 3),
            axis_b=(3, 3, 3, 3, 3),
        )
        reversed_ = _judgment(
            "c1",
            "1-3",
            0,
            swap=True,
            version_a="3",
            version_b="1",
            axis_a=(3, 3, 3, 3, 3),
            axis_b=(3, 3, 3, 3, 3),
        )

        result = resolve_swap_consistency([forward, reversed_])

        assert result.resolved == [ResolvedOutcome(case_id="c1", pair="1-3", winner_version=None)]
        assert result.inconsistent_n == 0

    def test_missing_one_direction_is_incomplete_not_inconsistent(self) -> None:
        forward = _judgment(
            "c1",
            "1-3",
            0,
            swap=False,
            version_a="1",
            version_b="3",
            axis_a=(2, 2, 2, 2, 2),
            axis_b=(4, 4, 4, 4, 4),
        )

        result = resolve_swap_consistency([forward])

        assert result.resolved == []
        assert result.inconsistent_n == 0
        assert result.incomplete_n == 1
        assert result.total_pairs_with_both_directions == 0
        assert result.consistency_rate is None


class TestWinRate:
    def test_counts_wins_losses_draws(self) -> None:
        outcomes = [
            ResolvedOutcome("c1", "1-3", "3"),
            ResolvedOutcome("c2", "1-3", "3"),
            ResolvedOutcome("c3", "1-3", "1"),
            ResolvedOutcome("c4", "1-3", None),
        ]

        rate, wins, losses, draws = win_rate(outcomes, winner="3", loser="1")

        assert wins == 2
        assert losses == 1
        assert draws == 1
        assert rate == pytest.approx(2 / 3)

    def test_zero_denominator_returns_none_rate(self) -> None:
        rate, wins, losses, draws = win_rate(
            [ResolvedOutcome("c1", "1-3", None)], winner="3", loser="1"
        )
        assert rate is None
        assert wins == 0 and losses == 0 and draws == 1


class TestClusterBootstrapCi:
    def test_all_wins_gives_degenerate_ci_at_one(self) -> None:
        outcomes = [ResolvedOutcome(f"c{i}", "1-3", "3") for i in range(20)]

        ci = cluster_bootstrap_ci(outcomes, winner="3", loser="1", iterations=500, seed=1)

        assert ci is not None
        lo, hi = ci
        assert lo == pytest.approx(1.0)
        assert hi == pytest.approx(1.0)

    def test_empty_outcomes_returns_none(self) -> None:
        assert cluster_bootstrap_ci([], winner="3", loser="1") is None

    def test_mixed_outcomes_ci_bounds_are_between_zero_and_one(self) -> None:
        outcomes = [ResolvedOutcome(f"c{i}", "1-3", "3" if i % 2 == 0 else "1") for i in range(30)]

        ci = cluster_bootstrap_ci(outcomes, winner="3", loser="1", iterations=1000, seed=7)

        assert ci is not None
        lo, hi = ci
        assert 0.0 <= lo <= hi <= 1.0

    def test_same_seed_is_reproducible(self) -> None:
        outcomes = [ResolvedOutcome(f"c{i}", "1-3", "3" if i % 3 else "1") for i in range(15)]

        ci_1 = cluster_bootstrap_ci(outcomes, winner="3", loser="1", iterations=500, seed=99)
        ci_2 = cluster_bootstrap_ci(outcomes, winner="3", loser="1", iterations=500, seed=99)

        assert ci_1 == ci_2


class TestTagStratumWinRates:
    def test_buckets_by_block_and_computes_per_block_rate(self) -> None:
        outcomes = [
            ResolvedOutcome("c1", "1-3", "3"),
            ResolvedOutcome("c2", "1-3", "1"),
            ResolvedOutcome("c3", "1-3", "3"),
        ]
        blocks = {"c1": "single_tag", "c2": "single_tag", "c3": "adversarial"}

        result = tag_stratum_win_rates(outcomes, blocks, winner="3", loser="1")

        assert result["single_tag"] == (0.5, 2)
        assert result["adversarial"] == (1.0, 1)


class TestDisqualificationRates:
    def test_counts_disqualification_events_per_version(self) -> None:
        rows = [
            _judgment(
                "c1",
                "1-3",
                0,
                swap=False,
                version_a="1",
                version_b="3",
                axis_a=(1, 1, 1, 1, 1),
                axis_b=(3, 3, 3, 3, 3),
            ),
            _judgment(
                "c2",
                "1-3",
                0,
                swap=False,
                version_a="3",
                version_b="1",
                axis_a=(3, 3, 3, 3, 3),
                axis_b=(3, 3, 3, 3, 3),
            ),
        ]

        rates = disqualification_rates(rows)

        # v1: 2번 등장(c1 의 A, c2 의 B), 1번 실격(c1 에서 axis_a[3]==1).
        assert rates["1"] == (1, 2)
        # v3: 2번 등장, 0번 실격.
        assert rates["3"] == (0, 2)


class TestPrintReportSuccessGate:
    def _base_kwargs(self) -> dict[str, object]:
        from scripts.l1_1_analyze import SwapConsistencyResult

        swap = SwapConsistencyResult(
            resolved=[], inconsistent_n=2, incomplete_n=0, total_pairs_with_both_directions=100
        )
        return {
            "swap_all": swap,
            "swap_primary": swap,
            "primary_rate": 0.70,
            "primary_wins": 70,
            "primary_losses": 30,
            "primary_draws": 0,
            "ci": (0.60, 0.80),
            "tag_rates": {"single_tag": (0.5, 40)},
            "secondary_v1v2": (0.55, 40, 30, 0),
            "secondary_v2v3": (0.60, 45, 30, 0),
            "dq_rates": {"1": (5, 100), "3": (1, 100)},
        }

    def test_all_three_criteria_pass(self, capsys: pytest.CaptureFixture[str]) -> None:
        kwargs = self._base_kwargs()
        overall = _print_report(**kwargs)  # type: ignore[arg-type]
        assert overall is True

    def test_fails_when_win_rate_below_threshold(self, capsys: pytest.CaptureFixture[str]) -> None:
        kwargs = self._base_kwargs()
        kwargs["primary_rate"] = 0.55
        overall = _print_report(**kwargs)  # type: ignore[arg-type]
        assert overall is False

    def test_fails_when_ci_lower_bound_not_above_half(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kwargs = self._base_kwargs()
        kwargs["ci"] = (0.45, 0.90)
        overall = _print_report(**kwargs)  # type: ignore[arg-type]
        assert overall is False

    def test_fails_when_swap_consistency_below_threshold(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from scripts.l1_1_analyze import SwapConsistencyResult

        kwargs = self._base_kwargs()
        kwargs["swap_primary"] = SwapConsistencyResult(
            resolved=[], inconsistent_n=30, incomplete_n=0, total_pairs_with_both_directions=100
        )
        overall = _print_report(**kwargs)  # type: ignore[arg-type]
        assert overall is False

    def test_fails_when_any_tag_stratum_below_threshold(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kwargs = self._base_kwargs()
        kwargs["tag_rates"] = {"single_tag": (0.5, 40), "adversarial": (0.20, 10)}
        overall = _print_report(**kwargs)  # type: ignore[arg-type]
        assert overall is False

    def test_none_tag_rate_does_not_crash_or_count_as_below_threshold(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """표본이 0(승+패 없음)인 층은 None 승률 — 미달로 잘못 세면 안 된다."""
        kwargs = self._base_kwargs()
        kwargs["tag_rates"] = {"single_tag": (0.5, 40), "uncovered_tag": (None, 0)}
        overall = _print_report(**kwargs)  # type: ignore[arg-type]
        assert overall is True
