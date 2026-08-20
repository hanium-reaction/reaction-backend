"""L1-2 공용 타입 회귀 — Cohen's κ 계산과 사람 라벨 JSONL 왕복."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.l1_2_common import (
    HumanLabelRow,
    append_human_label,
    cohens_kappa,
    read_human_labels,
)

_LABEL = HumanLabelRow(
    case_id="single-AMBIGUITY-01",
    pair="1-3",
    rep_index=0,
    swap=False,
    axis_a=(2, 2, 2, 2, 2),
    axis_b=(4, 4, 4, 4, 4),
    disqualification_reason=None,
)


def test_human_label_row_json_roundtrip() -> None:
    line = _LABEL.to_json()
    assert "\n" not in line

    restored = HumanLabelRow.from_json(line)

    assert restored == _LABEL
    assert isinstance(restored.axis_a, tuple)


def test_human_label_key_identifies_the_matching_judgment_row() -> None:
    assert _LABEL.key() == ("single-AMBIGUITY-01", "1-3", 0, False)


def test_append_then_read_human_labels_preserves_content(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    second = _LABEL._replace(case_id="single-AMBIGUITY-02", rep_index=1)

    append_human_label(_LABEL, path)
    append_human_label(second, path)

    assert read_human_labels(path) == [_LABEL, second]


def test_read_human_labels_returns_empty_list_when_file_missing(tmp_path: Path) -> None:
    assert read_human_labels(tmp_path / "does-not-exist.jsonl") == []


class TestCohensKappa:
    def test_perfect_agreement_is_one(self) -> None:
        pairs = [("a", "a"), ("b", "b"), ("draw", "draw"), ("a", "a")]
        assert cohens_kappa(pairs) == pytest.approx(1.0)

    def test_empty_returns_none(self) -> None:
        assert cohens_kappa([]) is None

    def test_systematic_disagreement_is_negative(self) -> None:
        # 완전히 반대로만 답하는 두 평가자 — 우연보다도 못 맞는다.
        pairs = [("a", "b"), ("b", "a"), ("a", "b"), ("b", "a")]
        kappa = cohens_kappa(pairs)
        assert kappa is not None
        assert kappa < 0

    def test_degenerate_single_category_is_perfect_agreement(self) -> None:
        # 둘 다 항상 "a" 만 냄 — Pe=1 이 되는 축퇴 상황, 0-division 대신 1.0 처리.
        pairs = [("a", "a"), ("a", "a"), ("a", "a")]
        assert cohens_kappa(pairs) == 1.0

    def test_chance_level_agreement_is_near_zero(self) -> None:
        # 평가자1은 항상 "a", 평가자2 는 절반씩 "a"/"b" — 관측 일치율이 우연 수준과 같다.
        pairs = [("a", "a"), ("a", "b")] * 10
        kappa = cohens_kappa(pairs)
        assert kappa is not None
        assert kappa == pytest.approx(0.0, abs=1e-9)
