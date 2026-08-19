"""L1-1 공용 타입 회귀 — JSONL 왕복과 골든셋 로딩."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.l1_1_common import (
    GenerationRow,
    JudgmentRow,
    decide_winner,
    load_golden_cases,
    read_generations,
    read_judgments,
    write_generations,
    write_judgments,
)

_ROW = GenerationRow(
    case_id="single-AMBIGUITY-01",
    version="2",
    repeat_index=0,
    fell_back=False,
    reason=None,
    strategy_code="downscope",
    if_clause="책상에 앉으면",
    then_clause="GROUP BY 실습 예제 1절만 15분",
    rationale="컸던 것 같아요",
    obstacle="",
    coping_clause="",
    acknowledgment="",
    estimated_workload_change_minutes=-30,
    failure_type="AMBIGUITY",
    strategy_label="5분 단위로 쪼개기",
    strategy_group="DOWNSCOPE",
    base_template="딱 5분만, 첫 단계만 해볼까요? GROUP BY 실습",
    context_summary="실행 카드: GROUP BY 실습 / 결과: failed",
)


def test_generation_row_json_roundtrip() -> None:
    line = _ROW.to_json()
    assert "\n" not in line  # JSONL 은 행 하나 = 한 줄

    restored = GenerationRow.from_json(line)

    assert restored == _ROW


def test_write_then_read_generations_preserves_order_and_content(tmp_path: Path) -> None:
    rows = [_ROW, _ROW._replace(case_id="single-AMBIGUITY-02", repeat_index=1)]
    path = tmp_path / "gen.jsonl"

    write_generations(rows, path)
    restored = read_generations(path)

    assert restored == rows


def test_load_golden_cases_reads_committed_file() -> None:
    cases = load_golden_cases()

    assert len(cases) == 120
    assert all("case_id" in c and "failure_tags" in c for c in cases)


def test_load_golden_cases_respects_limit() -> None:
    cases = load_golden_cases(limit=3)

    assert len(cases) == 3


_JUDGMENT_ROW = JudgmentRow(
    case_id="single-AMBIGUITY-01",
    pair="1-3",
    rep_index=0,
    swap=False,
    version_a="1",
    version_b="3",
    axis_a=(3, 2, 1, 3, 4),
    axis_b=(4, 4, 4, 5, 5),
    disqualification_reason=None,
)


def test_judgment_row_json_roundtrip() -> None:
    line = _JUDGMENT_ROW.to_json()
    assert "\n" not in line

    restored = JudgmentRow.from_json(line)

    assert restored == _JUDGMENT_ROW
    assert isinstance(restored.axis_a, tuple)  # JSON 배열이 튜플로 복원돼야 동등비교가 성립


def test_judgment_row_winner_version_maps_ab_label_to_underlying_version() -> None:
    row = _JUDGMENT_ROW._replace(axis_a=(1, 1, 1, 1, 1), axis_b=(5, 5, 5, 5, 5))
    assert row.winner_label() == "b"
    assert row.winner_version() == row.version_b

    draw_row = _JUDGMENT_ROW._replace(axis_a=(3, 3, 3, 3, 3), axis_b=(3, 3, 3, 3, 3))
    assert draw_row.winner_label() == "draw"
    assert draw_row.winner_version() is None


def test_write_then_read_judgments_preserves_content(tmp_path: Path) -> None:
    rows = [_JUDGMENT_ROW, _JUDGMENT_ROW._replace(swap=True, version_a="3", version_b="1")]
    path = tmp_path / "judgments.jsonl"

    write_judgments(rows, path)
    restored = read_judgments(path)

    assert restored == rows


class TestDecideWinner:
    """rubric-v1.md §2 종합 판정 규칙."""

    def test_higher_sum_wins(self) -> None:
        assert decide_winner((5, 5, 5, 5, 5), (1, 1, 1, 2, 1)) == "a"
        assert decide_winner((1, 1, 1, 2, 1), (5, 5, 5, 5, 5)) == "b"

    def test_axis4_equals_1_is_automatic_loss_regardless_of_sum(self) -> None:
        # A 는 축④=1(실격) 이지만 다른 축 전부 만점 — 그래도 B 가 이긴다.
        assert decide_winner((5, 5, 5, 1, 5), (2, 2, 2, 2, 2)) == "b"

    def test_both_disqualified_is_draw(self) -> None:
        assert decide_winner((5, 5, 5, 1, 5), (5, 5, 5, 1, 5)) == "draw"

    def test_tied_sum_breaks_on_axis4(self) -> None:
        # 합산 15로 동점, axis4(인덱스 3)만 다름: A=4, B=2.
        assert decide_winner((3, 3, 3, 4, 2), (3, 3, 5, 2, 2)) == "a"

    def test_tied_sum_and_tied_axis4_is_draw(self) -> None:
        assert decide_winner((3, 3, 3, 3, 3), (3, 3, 3, 3, 3)) == "draw"

    @pytest.mark.parametrize(
        ("axis_a", "axis_b", "expected"),
        [
            ((2, 2, 2, 2, 2), (2, 2, 2, 2, 3), "b"),
            ((3, 3, 3, 3, 3), (2, 2, 2, 2, 2), "a"),
        ],
    )
    def test_non_tied_sum_ignores_axis4_tiebreak_path(
        self, axis_a: tuple[int, ...], axis_b: tuple[int, ...], expected: str
    ) -> None:
        assert decide_winner(axis_a, axis_b) == expected  # type: ignore[arg-type]
