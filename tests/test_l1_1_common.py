"""L1-1 공용 타입 회귀 — JSONL 왕복과 골든셋 로딩."""

from __future__ import annotations

from pathlib import Path

from scripts.l1_1_common import (
    GenerationRow,
    load_golden_cases,
    read_generations,
    write_generations,
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
