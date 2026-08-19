"""L1-1 생성·판정 스크립트 공용 타입 (`docs/experiments/preregistration-v1.md` §2).

생성(`l1_1_generate.py`)이 쓰는 JSONL 1행 = `GenerationRow` 1개. 판정(`l1_1_judge.py`,
후속 PR)이 이 파일을 읽어 쌍대비교를 만든다. 두 스크립트가 서로 다른 실행(별도 프로세스,
보통 며칠 간격)이라 파일이 유일한 인터페이스 — 필드를 하나 빠뜨리면 판정 단계에서 그
정보를 영영 복구할 수 없으므로, 컨텍스트(failure_type 등)도 매 행에 중복 저장한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

# 골든셋과 나란히 둔다 — 실 LLM 호출 결과라 골든셋과 달리 재현되지 않으므로 커밋 대상이
# 아니다(.gitignore 의 eval/l1_1_*.jsonl).
GENERATIONS_PATH = Path(__file__).resolve().parent.parent / "eval" / "l1_1_generations.jsonl"

# 사전등록 §2 — 120건 × 3버전 × 3반복 = 1,080.
VERSIONS = ("1", "2", "3")
REPEATS_PER_VERSION = 3


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
