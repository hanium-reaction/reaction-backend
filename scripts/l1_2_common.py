"""L1-2 공용 타입 — LLM 심판 ↔ 사람 라벨 일치도 (`experiment-plan-v1.md` §2 L1-2).

⚠️ **설계 축소 안내**: 계획서 L1-2 는 "코더 2인 + 중재 1인(최소 1인은 v3 작성자가 아님)"
을 전제로, **inter-coder κ**(사람 2인 간 일치도)를 judge-human κ 보다 먼저 보고하도록
못박아 뒀다("인간끼리 낮으면 LLM 을 탓할 수 없다"). 이 프로젝트는 1인 개발이라 그 전제를
그대로 못 지킨다 — inter-coder κ 는 **계산이 원리적으로 불가능**(라벨러가 1명뿐이라
"두 사람 사이의 일치"라는 개념 자체가 없다).

그래서 여기서는 축소된 설계로 간다: **judge–human κ 만** 계산한다(사람 1인의 라벨과
LLM 심판의 판정을 직접 대조). "인간끼리 얼마나 일치하는가"라는, 원래 inter-coder κ 가
주려던 정보(과제 자체가 사람에게도 애매한지 아닌지)는 이 축소판에서 **아예 얻을 수
없다** — κ 가 낮게 나와도 "LLM 이 별로다"인지 "이 판정 과제 자체가 애매하다"인지 이
데이터만으로는 못 가른다. 이 한계는 결과 문서에 반드시 병기한다.

라벨은 `l1_2_label.py`(사람이 직접 실행하는 대화형 CLI, 이 저장소 밖에서 사람이 손으로
채점)가 쓰고, `l1_2_analyze.py`(LLM 호출 없는 순수 계산)가 읽는다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

HUMAN_LABELS_PATH = Path(__file__).resolve().parent.parent / "eval" / "l1_2_human_labels.jsonl"

# 계획서 L1-2 "소요 100건"에서, 팀(코더 2인) 전제가 1인으로 축소된 만큼 현실적인 1회
# 세션 분량으로 낮춘 기본값. `--n` 으로 언제든 늘려 재실행(이미 라벨링한 항목은 건너뜀).
DEFAULT_SAMPLE_SIZE = 40
SAMPLE_SEED = 20260820


class HumanLabelRow(NamedTuple):
    """사람 라벨 1건 — `eval/l1_1_judgments.jsonl` 의 같은
    (case_id, pair, rep_index, swap) 항목과 짝을 이룬다.

    버전 정보(version_a/version_b)는 일부러 안 담는다 — 라벨링 화면에 그 값이 뜨면
    블라인딩이 깨진다. 분석 시점엔 원본 `JudgmentRow` 를 같은 키로 조인해서 얻는다.
    """

    case_id: str
    pair: str
    rep_index: int
    swap: bool
    axis_a: tuple[int, int, int, int, int]
    axis_b: tuple[int, int, int, int, int]
    disqualification_reason: str | None

    def key(self) -> tuple[str, str, int, bool]:
        return (self.case_id, self.pair, self.rep_index, self.swap)

    def to_json(self) -> str:
        return json.dumps(self._asdict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> HumanLabelRow:
        data = json.loads(line)
        data["axis_a"] = tuple(data["axis_a"])
        data["axis_b"] = tuple(data["axis_b"])
        return cls(**data)


def read_human_labels(path: Path = HUMAN_LABELS_PATH) -> list[HumanLabelRow]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [HumanLabelRow.from_json(line) for line in f if line.strip()]


def append_human_label(row: HumanLabelRow, path: Path = HUMAN_LABELS_PATH) -> None:
    """라벨 1건을 즉시 추가 기록 — 라벨링은 사람이 여러 세션에 걸쳐 하므로, 중간에
    끊겨도(Ctrl-C, 컴퓨터 종료) 이미 답한 것까지는 남아야 한다. 한 번에 다 모아서
    마지막에 쓰는 방식은 이 워크플로에 안 맞는다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(row.to_json())
        f.write("\n")


def cohens_kappa(label_pairs: list[tuple[str, str]]) -> float | None:
    """비가중 Cohen's κ — (평가자1 라벨, 평가자2 라벨) 쌍 목록에서 계산.

    라벨은 명목형(순서 없음, 여기서는 "a"/"b"/"draw")이라 가중치 없는 버전을 쓴다.
    κ = (Po - Pe) / (1 - Pe):
    - Po = 관측된 일치 비율.
    - Pe = 각 평가자의 한계분포(marginal)로 기대되는 우연 일치 비율.

    표본이 0건이면 None. Pe == 1(두 평가자가 우연히도 항상 같은 카테고리로만 쏠려
    분모가 0이 되는 축퇴 상황)이면 완전 일치로 보고 1.0 을 반환한다.
    """
    if not label_pairs:
        return None

    categories = sorted({c for pair in label_pairs for c in pair})
    n = len(label_pairs)
    row_totals: dict[str, int] = dict.fromkeys(categories, 0)
    col_totals: dict[str, int] = dict.fromkeys(categories, 0)
    agree = 0
    for a, b in label_pairs:
        row_totals[a] += 1
        col_totals[b] += 1
        if a == b:
            agree += 1

    po = agree / n
    pe = sum((row_totals[c] / n) * (col_totals[c] / n) for c in categories)
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1 - pe)
