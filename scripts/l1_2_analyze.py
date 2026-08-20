"""L1-2 분석 — judge–human κ 계산. LLM 호출 없음, 순수 계산.

`l1_2_label.py` 로 모은 사람 라벨(`eval/l1_2_human_labels.jsonl`)과 원본 LLM 판정
(`eval/l1_1_judgments.jsonl`, 같은 (case_id, pair, rep_index, swap) 키로 조인)을 대조해
Cohen's κ 를 계산하고, 계획서 L1-2 의 성공 기준(축소판)을 적용한다.

⚠️ **이 실행이 계산하는 것은 계획서 원안의 부분집합이다.** `scripts/l1_2_common.py`
모듈 docstring 참고 — 1인 개발이라 inter-coder κ(사람 2인 간 일치도)는 계산 불가능하고,
judge–human κ(심판 vs 사람 1인)만 계산한다. **"인간끼리도 이 과제가 애매한가"라는
정보가 없으므로, κ 가 낮게 나와도 그 원인(LLM 문제 vs 과제 자체의 모호함)을 이 데이터
만으로는 못 가른다** — 이 한계는 보고서에 반드시 병기한다.

실행:
  uv run python -m scripts.l1_2_analyze
"""

from __future__ import annotations

import argparse
import logging

from scripts.l1_1_common import JudgmentRow, decide_winner, read_judgments
from scripts.l1_2_common import HumanLabelRow, cohens_kappa, read_human_labels

_log = logging.getLogger(__name__)

# 계획서 L1-2 성공 기준 임계값 — inter-coder κ 선행 조건은 이 축소판에서 검사 불가.
JUDGE_HUMAN_KAPPA_PROXY_THRESHOLD = 0.61  # 이상이면 L1-1 승률을 "인간 판단의 대리치"로.
JUDGE_HUMAN_KAPPA_AUXILIARY_THRESHOLD = 0.41  # 0.41~0.60 이면 보조 지표로만.


def match_labels_to_judgments(
    labels: list[HumanLabelRow], judgments: list[JudgmentRow]
) -> tuple[list[tuple[HumanLabelRow, JudgmentRow]], list[HumanLabelRow]]:
    """사람 라벨을 같은 키의 원본 판정과 조인한다. 못 찾으면 `unmatched` 로 정직하게 뺀다."""
    by_key = {(j.case_id, j.pair, j.rep_index, j.swap): j for j in judgments}
    matched: list[tuple[HumanLabelRow, JudgmentRow]] = []
    unmatched: list[HumanLabelRow] = []
    for label in labels:
        judgment = by_key.get(label.key())
        if judgment is None:
            unmatched.append(label)
        else:
            matched.append((label, judgment))
    return matched, unmatched


def label_pairs(matched: list[tuple[HumanLabelRow, JudgmentRow]]) -> list[tuple[str, str]]:
    """(사람 승자, 심판 승자) — "a"/"b"/"draw", A/B 는 그 항목 안에서만 의미 있는 슬롯이라
    항목 간 비교는 그대로 유효하다(두 평가자가 같은 항목의 같은 A/B 배정을 봤으므로).
    """
    pairs: list[tuple[str, str]] = []
    for label, judgment in matched:
        human_winner = decide_winner(label.axis_a, label.axis_b)
        judge_winner = judgment.winner_label()
        pairs.append((human_winner, judge_winner))
    return pairs


def _fmt(rate: float | None) -> str:
    return f"{rate:.3f}" if rate is not None else "N/A"


def _print_report(pairs: list[tuple[str, str]], *, unmatched_n: int) -> None:
    print("=" * 60)
    print("L1-2 분석 결과 (docs/experiments/experiment-plan-v1.md §2 L1-2, 축소판)")
    print("=" * 60)

    print(f"\n[표본] 사람 라벨 {len(pairs) + unmatched_n}건 중 판정과 매칭 성공 {len(pairs)}건")
    if unmatched_n:
        print(
            f"  ⚠️ {unmatched_n}건은 원본 판정과 매칭 실패 — l1_1_judgments.jsonl 이 라벨링 이후 바뀌었을 수 있다"
        )

    if not pairs:
        print("\n매칭된 라벨이 없어 κ 를 계산할 수 없다.")
        print("=" * 60)
        return

    agree = sum(1 for h, j in pairs if h == j)
    po = agree / len(pairs)
    kappa = cohens_kappa(pairs)

    print(f"\n[단순 일치율] {po:.3f} ({agree}/{len(pairs)})")
    print(f"[judge–human κ (Cohen's, unweighted)] {_fmt(kappa)}")

    print("\n[inter-coder κ (사람 2인 간 일치도)] N/A — 이 프로젝트는 1인 개발이라")
    print("  원리적으로 계산 불가. 계획서 원안의 선행 조건(inter-coder κ≥0.60)을")
    print("  검사하지 못했다 — 아래 판정은 그만큼 근거가 약하다.")

    print("\n" + "=" * 60)
    print("[해석] (계획서 L1-2 축소판 — inter-coder 선행 조건 미검증 상태로 적용)")
    if kappa is None:
        verdict = "판정 불가(표본 없음)"
    elif kappa >= JUDGE_HUMAN_KAPPA_PROXY_THRESHOLD:
        verdict = f"κ≥{JUDGE_HUMAN_KAPPA_PROXY_THRESHOLD} — L1-1 승률을 인간 판단의 대리치로 서술 가능한 수준"
    elif kappa >= JUDGE_HUMAN_KAPPA_AUXILIARY_THRESHOLD:
        verdict = f"{JUDGE_HUMAN_KAPPA_AUXILIARY_THRESHOLD}≤κ<{JUDGE_HUMAN_KAPPA_PROXY_THRESHOLD} — 보조 지표로만 사용"
    else:
        verdict = f"κ<{JUDGE_HUMAN_KAPPA_AUXILIARY_THRESHOLD} — L1-1 결과를 본문에서 부록으로 강등"
    print(f"  {verdict}")
    print(
        "  ⚠️ 위 등급은 inter-coder κ 선행 조건이 충족됐다는 전제 위에서만 유효하다.\n"
        "     이 실행은 그 전제를 검증하지 못했으므로, 이 등급 자체를 잠정으로 취급할 것."
    )
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="l1_2_analyze", description="L1-2 judge–human κ 분석 (축소판 — inter-coder 없음)."
    )
    parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    labels = read_human_labels()
    _log.info("사람 라벨 %d건 로드", len(labels))
    if not labels:
        _log.error("라벨 데이터가 없다 — l1_2_label.py 를 먼저 사람이 직접 실행해야 한다")
        return 1

    judgments = read_judgments()
    matched, unmatched = match_labels_to_judgments(labels, judgments)
    pairs = label_pairs(matched)

    _print_report(pairs, unmatched_n=len(unmatched))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
