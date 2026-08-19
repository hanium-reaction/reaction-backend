"""L1-1 생성·판정·분석 스크립트 공용 타입 (`docs/experiments/preregistration-v1.md` §2).

생성(`l1_1_generate.py`)이 쓰는 JSONL 1행 = `GenerationRow` 1개. 판정(`l1_1_judge.py`)이
이 파일을 읽어 쌍대비교를 만들고, 그 결과(`JudgmentRow`)를 또 JSONL 로 쓴다. 분석
(`l1_1_analyze.py`)이 그 판정 파일만 읽어 순수 계산(승률·CI·swap consistency)을 한다.
세 스크립트가 서로 다른 실행(보통 며칠 간격, 판정은 특히 비용 때문에 재실행을 피하고
싶다)이라 파일이 유일한 인터페이스 — 필드를 하나 빠뜨리면 다음 단계가 그 정보를 영영
복구할 수 없으므로, 각 행에 필요한 컨텍스트를 전부 중복 저장한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, Field

# 골든셋과 나란히 둔다 — 실 LLM 호출 결과라 골든셋과 달리 재현되지 않으므로 커밋 대상이
# 아니다(.gitignore 의 eval/l1_1_*.jsonl).
GENERATIONS_PATH = Path(__file__).resolve().parent.parent / "eval" / "l1_1_generations.jsonl"
JUDGMENTS_PATH = Path(__file__).resolve().parent.parent / "eval" / "l1_1_judgments.jsonl"

# 사전등록 §2 — 120건 × 3버전 × 3반복 = 1,080.
VERSIONS = ("1", "2", "3")
REPEATS_PER_VERSION = 3

# 사전등록 §2 — 3쌍(v1-v2, v2-v3, v1-v3) × 120건 × 2반복 × 양방향(2) = 1,440 판정.
# "2반복"은 REPEATS_PER_VERSION(3) 중 fallback 이 아닌 것부터 최대 2개를 고른 것 —
# 3반복으로 넉넉히 생성해 둔 이유가 바로 이 여유분(l1_1_generate.py 참고).
PAIRS: tuple[tuple[str, str], ...] = (("1", "2"), ("2", "3"), ("1", "3"))
REPS_PER_PAIR = 2


def pair_key(version_low: str, version_high: str) -> str:
    return f"{version_low}-{version_high}"


class GenerationRow(NamedTuple):
    """`RecoveryProposalLLM` 출력 1건 + 판정에 필요한 컨텍스트."""

    case_id: str
    version: str
    repeat_index: int
    fell_back: bool
    reason: str | None
    strategy_code: str
    if_clause: str
    then_clause: str
    rationale: str
    obstacle: str
    coping_clause: str
    acknowledgment: str
    estimated_workload_change_minutes: int
    # 판정 시점에 프롬프트를 다시 조립할 수 있게 컨텍스트도 그대로 싣는다 — 세 버전·세
    # 반복이 같은 case_id 안에서는 전부 동일한 값이므로 중복이지만, 행 하나만으로 판정
    # 가능해야 두 스크립트 사이 결합이 "파일 하나"로 끝난다.
    failure_type: str
    strategy_label: str
    strategy_group: str
    base_template: str
    context_summary: str

    def to_json(self) -> str:
        return json.dumps(self._asdict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> GenerationRow:
        return cls(**json.loads(line))


def read_generations(path: Path = GENERATIONS_PATH) -> list[GenerationRow]:
    with path.open(encoding="utf-8") as f:
        return [GenerationRow.from_json(line) for line in f if line.strip()]


def write_generations(rows: list[GenerationRow], path: Path = GENERATIONS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(row.to_json())
            f.write("\n")


class AxisScores(BaseModel):
    """루브릭 5축 점수 — `rubric-v1.md` §3 심판 출력 스키마의 `candidate_a`/`candidate_b`."""

    axis1: int = Field(ge=1, le=5)
    axis2: int = Field(ge=1, le=5)
    axis3: int = Field(ge=1, le=5)
    axis4: int = Field(ge=1, le=5)
    axis5: int = Field(ge=1, le=5)

    def as_tuple(self) -> tuple[int, int, int, int, int]:
        return (self.axis1, self.axis2, self.axis3, self.axis4, self.axis5)


class JudgeVerdict(BaseModel):
    """`rubric-v1.md` §3 심판 프롬프트의 전체 출력 스키마.

    승자 산정은 이 스키마에 없다 — §2 규칙(`decide_winner`)을 분석 코드가 결정적으로
    적용한다(rubric-v1.md §3 마지막 문단 — "심판이 승자까지 같이 내면... 논리적으로
    안 맞을 수 있다").
    """

    candidate_a: AxisScores
    candidate_b: AxisScores
    axis4_disqualification_reason: str | None = None


class JudgmentRow(NamedTuple):
    """심판 판정 1건 — 같은 (case_id, pair, rep_index) 에 forward/reversed 2행이 쌍을 이룬다."""

    case_id: str
    pair: str  # "1-2" | "2-3" | "1-3" — 낮은 버전이 앞
    rep_index: int
    swap: bool  # False=정방향(canonical A/B 배정), True=역방향(A/B 뒤집음)
    version_a: str  # 이 판정에서 실제로 "A"로 제시된 버전
    version_b: str
    axis_a: tuple[int, int, int, int, int]
    axis_b: tuple[int, int, int, int, int]
    disqualification_reason: str | None

    def to_json(self) -> str:
        return json.dumps(self._asdict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> JudgmentRow:
        data = json.loads(line)
        data["axis_a"] = tuple(data["axis_a"])
        data["axis_b"] = tuple(data["axis_b"])
        return cls(**data)

    def winner_label(self) -> str:
        """§2 규칙 적용 결과 — "a" | "b" | "draw" (A/B 라벨 기준, 버전 기준 아님)."""
        return decide_winner(self.axis_a, self.axis_b)

    def winner_version(self) -> str | None:
        """§2 규칙 적용 결과를 **버전**으로 변환. draw 면 None."""
        label = self.winner_label()
        if label == "a":
            return self.version_a
        if label == "b":
            return self.version_b
        return None


def decide_winner(
    axis_a: tuple[int, int, int, int, int], axis_b: tuple[int, int, int, int, int]
) -> str:
    """`rubric-v1.md` §2 종합 판정 규칙 — 축 인덱스는 0-based (axis4 = index 3).

    1. 축④=1점(실격)인 쪽은 합산과 무관하게 자동 패배. 둘 다 실격이면 무승부.
    2. 실격이 없으면 5축 합산이 높은 쪽 승리.
    3. 합산 동점이면 축④ 점수로 tiebreak.
    4. 축④까지 동점이면 무승부.
    """
    a_disqualified = axis_a[3] == 1
    b_disqualified = axis_b[3] == 1
    if a_disqualified and b_disqualified:
        return "draw"
    if a_disqualified:
        return "b"
    if b_disqualified:
        return "a"

    sum_a, sum_b = sum(axis_a), sum(axis_b)
    if sum_a != sum_b:
        return "a" if sum_a > sum_b else "b"

    if axis_a[3] != axis_b[3]:
        return "a" if axis_a[3] > axis_b[3] else "b"

    return "draw"


def read_judgments(path: Path = JUDGMENTS_PATH) -> list[JudgmentRow]:
    with path.open(encoding="utf-8") as f:
        return [JudgmentRow.from_json(line) for line in f if line.strip()]


def write_judgments(rows: list[JudgmentRow], path: Path = JUDGMENTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(row.to_json())
            f.write("\n")


def load_golden_cases(limit: int | None = None) -> list[dict[str, Any]]:
    """`eval/golden_recovery_cases.jsonl` 을 읽는다 — L1-0 산출물, 커밋된 파일이 진실.

    `scripts.build_golden_recovery_cases.build_cases()` 를 다시 부르지 않는 이유: 그러면
    "생성기 코드"가 진실이 되어 디스크의 커밋된 파일과 실행 시점에 갈라질 수 있다
    (`test_golden_recovery_cases.py` 가 둘이 같음을 보장하긴 하지만, 그 보장은 테스트
    스위트에서만 확인되지 이 스크립트 실행 시점엔 재검증되지 않는다). 커밋된 파일을
    직접 읽으면 "그 커밋에서 실제로 뭘 읽었는지"가 그대로 진실이다.
    """
    from scripts.build_golden_recovery_cases import OUTPUT_PATH

    with OUTPUT_PATH.open(encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]
    return cases[:limit] if limit is not None else cases
