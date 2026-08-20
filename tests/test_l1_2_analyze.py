"""L1-2 분석 회귀 — 라벨↔판정 매칭, 승자 라벨 변환."""

from __future__ import annotations

from scripts.l1_1_common import JudgmentRow
from scripts.l1_2_analyze import label_pairs, match_labels_to_judgments
from scripts.l1_2_common import HumanLabelRow


def _judgment(case_id: str, *, axis_a: tuple[int, ...], axis_b: tuple[int, ...]) -> JudgmentRow:
    return JudgmentRow(
        case_id=case_id,
        pair="1-3",
        rep_index=0,
        swap=False,
        version_a="1",
        version_b="3",
        axis_a=axis_a,  # type: ignore[arg-type]
        axis_b=axis_b,  # type: ignore[arg-type]
        disqualification_reason=None,
    )


def _label(case_id: str, *, axis_a: tuple[int, ...], axis_b: tuple[int, ...]) -> HumanLabelRow:
    return HumanLabelRow(
        case_id=case_id,
        pair="1-3",
        rep_index=0,
        swap=False,
        axis_a=axis_a,  # type: ignore[arg-type]
        axis_b=axis_b,  # type: ignore[arg-type]
        disqualification_reason=None,
    )


class TestMatchLabelsToJudgments:
    def test_matches_by_case_pair_rep_swap_key(self) -> None:
        judgments = [_judgment("c1", axis_a=(3, 3, 3, 3, 3), axis_b=(4, 4, 4, 4, 4))]
        labels = [_label("c1", axis_a=(2, 2, 2, 2, 2), axis_b=(5, 5, 5, 5, 5))]

        matched, unmatched = match_labels_to_judgments(labels, judgments)

        assert len(matched) == 1
        assert unmatched == []
        assert matched[0][0] is labels[0]
        assert matched[0][1] is judgments[0]

    def test_unmatched_when_no_corresponding_judgment(self) -> None:
        labels = [_label("orphan", axis_a=(2, 2, 2, 2, 2), axis_b=(5, 5, 5, 5, 5))]

        matched, unmatched = match_labels_to_judgments(labels, judgments=[])

        assert matched == []
        assert unmatched == labels


class TestLabelPairs:
    def test_converts_both_sides_to_winner_labels(self) -> None:
        # 사람: B 가 이김(5축 합산 우세). 심판: A 가 이김.
        judgment = _judgment("c1", axis_a=(4, 4, 4, 4, 4), axis_b=(2, 2, 2, 2, 2))
        label = _label("c1", axis_a=(2, 2, 2, 2, 2), axis_b=(5, 5, 5, 5, 5))

        pairs = label_pairs([(label, judgment)])

        assert pairs == [("b", "a")]

    def test_axis4_disqualification_rule_applies_to_human_side_too(self) -> None:
        # 사람이 A 를 축④=1(실격)로 채점했으면, 다른 축이 만점이어도 A 는 자동 패배.
        judgment = _judgment("c1", axis_a=(3, 3, 3, 3, 3), axis_b=(3, 3, 3, 3, 3))
        label = _label("c1", axis_a=(5, 5, 5, 1, 5), axis_b=(2, 2, 2, 2, 2))

        pairs = label_pairs([(label, judgment)])

        assert pairs == [("b", "draw")]
