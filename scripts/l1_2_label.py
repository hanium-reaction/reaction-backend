"""L1-2 사람 라벨링 — 대화형 CLI. **사용자가 직접 터미널에서 실행한다.**

`l1_1_judge.py` 가 이미 판정한 실 비교(1,430건, `eval/l1_1_judgments.jsonl`)에서 표본을
뽑아, LLM 심판이 본 것과 **완전히 같은 블라인드 표현**(버전 정보 없음)을 사람에게 보여
주고 루브릭 5축을 채점받는다. 이렇게 나온 사람 라벨을 나중에 `l1_2_analyze.py` 가
LLM 판정과 대조해 judge–human κ 를 계산한다.

⚠️ **설계 축소** — `scripts/l1_2_common.py` 모듈 docstring 참고. 계획서가 요구한
"코더 2인" 을 이 프로젝트(1인 개발)는 못 채운다. inter-coder κ 는 원리적으로 계산
불가능하고, judge–human κ 만 계산한다.

이 스크립트는 `input()` 으로 답을 받는 진짜 대화형 프로그램이다 — 자동화 파이프라인이나
Claude 가 대신 실행할 수 없다(누가 답해도 "사람 라벨"이 아니게 된다). **사용자 본인이
자기 터미널에서 직접 돌려야 한다.**

실행:
  uv run python -m scripts.l1_2_label                # 새 40건(기본) 라벨링
  uv run python -m scripts.l1_2_label --n 20          # 20건만(짧게 여러 세션에 나눠서)
  uv run python -m scripts.l1_2_label --n 100 --seed 1   # 다른 시드로 다른 표본

Ctrl-C 로 언제든 중단해도 그때까지 답한 건 `eval/l1_2_human_labels.jsonl` 에 즉시
저장돼 있다 — 다시 실행하면 이미 답한 항목은 자동으로 건너뛴다.
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Callable, Iterable

from scripts.l1_1_common import (
    GenerationRow,
    JudgmentRow,
    candidate_payload,
    read_generations,
    read_judgments,
)
from scripts.l1_2_common import (
    DEFAULT_SAMPLE_SIZE,
    SAMPLE_SEED,
    HumanLabelRow,
    append_human_label,
    read_human_labels,
)

_AXIS_LABELS = (
    "① if절 단서 구체성",
    "② then절 unpacking",
    "③ coping절 독립성",
    "④ 톤(금지어가 그대로 있으면 1점)",
    "⑤ 사실 정합",
)


def dedupe_to_units(judgments: Iterable[JudgmentRow], *, seed: int) -> list[JudgmentRow]:
    """(case_id, pair, rep_index) 당 정방향/역방향 중 하나만 고른다.

    사람 라벨링 예산은 제한적이라, 같은 내용을 순서만 바꿔 두 번 라벨링시키는 건 낭비다
    — LLM 심판은 swap consistency 자체를 재려고 양방향을 다 봤지만(rubric-v1.md §4-3),
    사람 라벨은 judge–human κ 만 재면 되므로 한 방향이면 충분하다.
    """
    by_unit: dict[tuple[str, str, int], list[JudgmentRow]] = {}
    for row in judgments:
        by_unit.setdefault((row.case_id, row.pair, row.rep_index), []).append(row)

    rng = random.Random(seed)
    units: list[JudgmentRow] = []
    for key in sorted(by_unit):
        candidates = by_unit[key]
        units.append(candidates[0] if len(candidates) == 1 else rng.choice(candidates))
    return units


def sample_for_labeling(
    judgments: Iterable[JudgmentRow],
    *,
    n: int,
    seed: int = SAMPLE_SEED,
    already_labeled: frozenset[tuple[str, str, int, bool]] = frozenset(),
) -> list[JudgmentRow]:
    """라벨링할 항목 N개를 고른다 — 이미 답한 건 제외, 시드 고정으로 재현 가능."""
    units = dedupe_to_units(judgments, seed=seed)
    remaining = [
        u for u in units if (u.case_id, u.pair, u.rep_index, u.swap) not in already_labeled
    ]
    rng = random.Random(seed + 1)  # dedupe 의 방향 선택과 다른 난수열 — 서로 상관되지 않게.
    rng.shuffle(remaining)
    return remaining[:n]


def format_item_for_display(
    row: JudgmentRow, generation_lookup: dict[tuple[str, str, int], GenerationRow]
) -> str:
    """LLM 심판이 본 것과 동일한 블라인드 표현. **버전 정보를 절대 포함하지 않는다.**"""
    gen_a = generation_lookup[(row.case_id, row.version_a, row.rep_index)]
    gen_b = generation_lookup[(row.case_id, row.version_b, row.rep_index)]
    lines = [
        "# 상황",
        f"- 실패 사유: {gen_a.failure_type}",
        f"- 룰이 고른 전략: {gen_a.strategy_label} (그룹: {gen_a.strategy_group})",
        f"- 카탈로그 기본 문구: {gen_a.base_template}",
        f"- 실행 컨텍스트: {gen_a.context_summary}",
        "",
        "# 후보 A",
        json.dumps(candidate_payload(gen_a), ensure_ascii=False, indent=2),
        "",
        "# 후보 B",
        json.dumps(candidate_payload(gen_b), ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines)


def _prompt_axis_score(
    label: str, *, input_fn: Callable[[str], str], print_fn: Callable[[str], None]
) -> int:
    while True:
        raw = input_fn(f"  {label} (1~5): ").strip()
        try:
            value = int(raw)
        except ValueError:
            print_fn("    1~5 사이 정수를 입력하세요.")
            continue
        if 1 <= value <= 5:
            return value
        print_fn("    1~5 사이 정수를 입력하세요.")


def _score_candidate(
    label: str, *, input_fn: Callable[[str], str], print_fn: Callable[[str], None]
) -> tuple[int, int, int, int, int]:
    print_fn(f"{label} 채점 (rubric-v1.md §1 앵커 기준):")
    a1 = _prompt_axis_score(_AXIS_LABELS[0], input_fn=input_fn, print_fn=print_fn)
    a2 = _prompt_axis_score(_AXIS_LABELS[1], input_fn=input_fn, print_fn=print_fn)
    a3 = _prompt_axis_score(_AXIS_LABELS[2], input_fn=input_fn, print_fn=print_fn)
    a4 = _prompt_axis_score(_AXIS_LABELS[3], input_fn=input_fn, print_fn=print_fn)
    a5 = _prompt_axis_score(_AXIS_LABELS[4], input_fn=input_fn, print_fn=print_fn)
    return (a1, a2, a3, a4, a5)


def label_one_item(
    row: JudgmentRow,
    generation_lookup: dict[tuple[str, str, int], GenerationRow],
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> HumanLabelRow:
    print_fn(format_item_for_display(row, generation_lookup))
    print_fn("")
    axis_a = _score_candidate("후보 A", input_fn=input_fn, print_fn=print_fn)
    axis_b = _score_candidate("후보 B", input_fn=input_fn, print_fn=print_fn)
    reason = input_fn("금지어 실격 사유가 있으면 적고, 없으면 그냥 엔터: ").strip() or None
    return HumanLabelRow(
        case_id=row.case_id,
        pair=row.pair,
        rep_index=row.rep_index,
        swap=row.swap,
        axis_a=axis_a,
        axis_b=axis_b,
        disqualification_reason=reason,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="l1_2_label",
        description="L1-2 사람 라벨링(대화형) — LLM 심판이 본 실 비교를 사람이 채점한다.",
    )
    parser.add_argument("--n", type=int, default=DEFAULT_SAMPLE_SIZE, help="이번 세션 라벨링 개수")
    parser.add_argument("--seed", type=int, default=SAMPLE_SEED, help="표본 추출 시드")
    args = parser.parse_args(argv)

    judgments = read_judgments()
    generations = read_generations()
    generation_lookup = {(g.case_id, g.version, g.repeat_index): g for g in generations}

    already_labeled = frozenset(row.key() for row in read_human_labels())
    units = sample_for_labeling(
        judgments, n=args.n, seed=args.seed, already_labeled=already_labeled
    )

    if not units:
        print(
            f"이미 {len(already_labeled)}건 라벨링 완료 — 더 뽑을 새 항목이 없다(또는 --n 을 늘리세요)."
        )
        return 0

    print(
        f"이번 세션 {len(units)}건 라벨링 시작 (기존 완료: {len(already_labeled)}건). "
        "Ctrl-C 로 언제든 중단해도 지금까지 답한 건 저장됩니다."
    )
    for i, unit in enumerate(units, 1):
        print(f"\n--- {i}/{len(units)} ---")
        try:
            label = label_one_item(unit, generation_lookup)
        except KeyboardInterrupt:
            print("\n중단됨. 지금까지 답한 내용은 저장돼 있습니다.")
            return 0
        append_human_label(label)

    print(f"\n완료: 이번 세션 {len(units)}건. 누적 {len(already_labeled) + len(units)}건.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
