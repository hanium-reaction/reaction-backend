"""L1-2 라벨링 CLI 회귀 — 표본 추출·블라인딩·입력 검증. 실 사람 입력은 스텁으로 대체."""

from __future__ import annotations

from scripts.l1_1_common import GenerationRow, JudgmentRow
from scripts.l1_2_label import (
    dedupe_to_units,
    format_item_for_display,
    label_one_item,
    sample_for_labeling,
)


def _judgment(
    case_id: str,
    pair: str,
    rep_index: int,
    *,
    swap: bool,
    version_a: str = "1",
    version_b: str = "3",
) -> JudgmentRow:
    return JudgmentRow(
        case_id=case_id,
        pair=pair,
        rep_index=rep_index,
        swap=swap,
        version_a=version_a,
        version_b=version_b,
        axis_a=(3, 3, 3, 3, 3),
        axis_b=(3, 3, 3, 3, 3),
        disqualification_reason=None,
    )


def _generation(case_id: str, version: str, repeat_index: int) -> GenerationRow:
    return GenerationRow(
        case_id=case_id,
        version=version,
        repeat_index=repeat_index,
        fell_back=False,
        reason=None,
        strategy_code="downscope",
        if_clause=f"if-v{version}",
        then_clause=f"then-v{version}",
        rationale="rationale",
        obstacle="obstacle" if version == "3" else "",
        coping_clause="coping" if version == "3" else "",
        acknowledgment="",
        estimated_workload_change_minutes=0,
        failure_type="AMBIGUITY",
        strategy_label="5분 단위로 쪼개기",
        strategy_group="DOWNSCOPE",
        base_template="딱 5분만",
        context_summary="실행 카드: X / 결과: failed",
    )


class TestDedupeToUnits:
    def test_picks_exactly_one_direction_per_unit(self) -> None:
        judgments = [
            _judgment("c1", "1-3", 0, swap=False),
            _judgment("c1", "1-3", 0, swap=True),
            _judgment("c2", "1-2", 0, swap=False),  # 역방향 없음 — 하나뿐이면 그대로 씀
        ]

        units = dedupe_to_units(judgments, seed=1)

        keys = {(u.case_id, u.pair, u.rep_index) for u in units}
        assert keys == {("c1", "1-3", 0), ("c2", "1-2", 0)}
        assert len(units) == 2

    def test_is_deterministic_given_same_seed(self) -> None:
        judgments = [_judgment("c1", "1-3", r, swap=s) for r in range(5) for s in (False, True)]

        units_1 = dedupe_to_units(judgments, seed=42)
        units_2 = dedupe_to_units(judgments, seed=42)

        assert [u.swap for u in units_1] == [u.swap for u in units_2]


class TestSampleForLabeling:
    def test_respects_n(self) -> None:
        judgments = [_judgment(f"c{i}", "1-3", 0, swap=False) for i in range(10)]

        sampled = sample_for_labeling(judgments, n=3, seed=1)

        assert len(sampled) == 3

    def test_excludes_already_labeled(self) -> None:
        judgments = [_judgment(f"c{i}", "1-3", 0, swap=False) for i in range(5)]
        already = frozenset({("c0", "1-3", 0, False), ("c1", "1-3", 0, False)})

        sampled = sample_for_labeling(judgments, n=10, seed=1, already_labeled=already)

        sampled_keys = {(u.case_id, u.pair, u.rep_index, u.swap) for u in sampled}
        assert sampled_keys.isdisjoint(already)
        assert len(sampled) == 3

    def test_deterministic_given_same_seed(self) -> None:
        judgments = [_judgment(f"c{i}", "1-3", 0, swap=False) for i in range(20)]

        sampled_1 = sample_for_labeling(judgments, n=5, seed=7)
        sampled_2 = sample_for_labeling(judgments, n=5, seed=7)

        assert [u.case_id for u in sampled_1] == [u.case_id for u in sampled_2]


class TestFormatItemForDisplay:
    def test_never_leaks_version_identifiers(self) -> None:
        row = _judgment("c1", "1-3", 0, swap=False, version_a="1", version_b="3")
        lookup = {
            ("c1", "1", 0): _generation("c1", "1", 0),
            ("c1", "3", 0): _generation("c1", "3", 0),
        }

        text = format_item_for_display(row, lookup)

        # "version"/"prompt_id" 같은 구조적 필드명이 텍스트에 안 새어 나가는지 확인한다.
        # (카드 내용 자체는 얼마든지 실릴 수 있다 — 여기서 막는 건 필드명 누출이지 텍스트
        # 내용이 아니다.)
        assert "version" not in text.lower()
        assert "prompt_id" not in text


class TestLabelOneItem:
    def test_collects_axis_scores_and_reason_from_input(self) -> None:
        row = _judgment("c1", "1-3", 0, swap=False, version_a="1", version_b="3")
        lookup = {
            ("c1", "1", 0): _generation("c1", "1", 0),
            ("c1", "3", 0): _generation("c1", "3", 0),
        }
        # 후보 A 5개 + 후보 B 5개 + 실격 사유(빈 문자열).
        answers = iter(["3", "3", "3", "1", "3", "4", "4", "4", "5", "4", ""])

        label = label_one_item(
            row, lookup, input_fn=lambda _prompt: next(answers), print_fn=lambda _msg: None
        )

        assert label.axis_a == (3, 3, 3, 1, 3)
        assert label.axis_b == (4, 4, 4, 5, 4)
        assert label.disqualification_reason is None
        assert label.case_id == "c1"
        assert label.swap is False

    def test_reprompts_on_invalid_axis_score(self) -> None:
        row = _judgment("c1", "1-3", 0, swap=False, version_a="1", version_b="3")
        lookup = {
            ("c1", "1", 0): _generation("c1", "1", 0),
            ("c1", "3", 0): _generation("c1", "3", 0),
        }
        # 첫 축 점수에 잘못된 값 2개("abc", "9")를 준 뒤에야 유효한 값을 준다.
        answers = iter(
            ["abc", "9", "3", "3", "3", "3", "3", "4", "4", "4", "4", "4", "게으르 그대로 있음"]
        )
        messages: list[str] = []

        label = label_one_item(
            row, lookup, input_fn=lambda _prompt: next(answers), print_fn=messages.append
        )

        assert label.axis_a == (3, 3, 3, 3, 3)
        assert label.axis_b == (4, 4, 4, 4, 4)
        assert label.disqualification_reason == "게으르 그대로 있음"
        assert any("1~5 사이 정수" in m for m in messages)
