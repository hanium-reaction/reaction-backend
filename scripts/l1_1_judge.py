"""L1-1 판정 하네스 — 생성물(v1/v2/v3)을 쌍대비교로 심판한다 (사전등록 §2, 루브릭 §3).

`l1_1_generate.py` 가 만든 `eval/l1_1_generations.jsonl` 을 읽어, 3쌍(v1-v2, v2-v3,
v1-v3) × 120건 × 2반복(3반복 중 fallback 아닌 것부터) × 양방향(정방향/역방향) = 1,440
판정을 만든다. 심판은 축별 점수만 내고, 승자 산정(`l1_1_common.decide_winner`)은 분석
단계가 결정적으로 한다(루브릭 §3 — "심판이 승자까지 같이 내면 점수와 결론이 따로 놀 수
있다").

## 왜 `aiClient.run()` 이 아니라 `provider.generate_structured()` 직접 호출인가

`prompts.registry` 는 8개 잠금 도메인(`SUPPORTED_DOMAINS`) 밖의 디렉터리를 스캔에서
아예 제외한다 — 이 심판 프롬프트는 그 8개 중 어디에도 속하지 않는 연구용 평가 도구라
`prompt_id` 로 등록할 자리가 구조적으로 없다(9번째 "eval" 도메인을 새로 여는 것도, 기존
도메인에 억지로 얹는 것도 각각 다른 문제가 있어 채택하지 않았다 — 자세한 내용은 PR
설명). AGENTS.md §2 "LLM SDK 직접 import 금지 — 모두 Tool Executor 경유"는 이 판단을
사용자와 상의해 확인받았다: `provider.generate_structured()` 는 원본 SDK(google-genai)
가 아니라 `reaction_backend.llm` 내부 래퍼이므로 "SDK 직접 import 금지"는 여전히 지켜지고,
Tool Executor 의 나머지 단계(레지스트리 렌더·예산 체크·`llm_runs` 기록·금지어 치환)는
전부 **프로덕션 사용자 트래픽에 대한 안전장치**라 심판 호출(축 점수 JSON, 사용자에게
노출 안 됨)에는 애초에 적용 대상이 아니다 — 특히 금지어 치환은 `axis4_disqualification_reason`
필드에 실제 금지어 인용이 들어가야 하는데(예: "게으르 가 그대로 있음"), 프로덕션 치환
필터를 거치면 그 인용 자체가 다른 말로 바뀌어버린다.

실패(재시도 후에도)는 **fallback 으로 메꾸지 않는다** — 생성의 fallback 은 "LLM 이
실패하면 룰 템플릿을 보여준다"는 실제 제품 동작이라 정직한 기록이지만, 심판이 실패했을
때 만든 점수는 실제 판정이 아니라 조작된 통계가 된다. 실패한 판정은 그냥 버리고 개수를
로그에 남긴다.

⚠️ **비용 경고**: 기본 실행은 최대 1,440회 심판 호출이다. `--limit` 으로 먼저
소규모 검증할 것.

실행:
  uv run python -m scripts.l1_1_judge                       # 전체 1,440건 (최대)
  uv run python -m scripts.l1_1_judge --limit-cases 2         # 골든셋 앞 2건만 (스모크)
  uv run python -m scripts.l1_1_judge --dry-run                # 페어링·프롬프트만 확인
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from reaction_backend.llm.provider import (
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderValidationError,
    generate_structured,
)
from scripts.l1_1_common import (
    GENERATIONS_PATH,
    JUDGMENTS_PATH,
    PAIRS,
    REPS_PER_PAIR,
    GenerationRow,
    JudgeVerdict,
    JudgmentRow,
    pair_key,
    read_generations,
    write_judgments,
)

_log = logging.getLogger(__name__)

# 루브릭 §4-4 — 생성과 같은 provider, 다른 모델(자기고평가 편향 완화 시도). 팀 합의 전까지의
# 잠정 선택이라 self-enhancement bias 를 L1-2(judge–human κ)로 대조하는 것이 최종 검증.
DEFAULT_JUDGE_MODEL = "gemini-3.5-flash"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_ATTEMPTS = 3

# 블라인딩 절차의 시드 — 실행마다 같은 배정이 나오게 고정(루브릭 §4-2, 사전등록 §6).
# 값 자체는 이 커밋에 기록되는 것으로 "시드를 사전등록에 커밋 해시로 남긴다"를 만족한다.
RANDOM_SEED = 20260819


@dataclass(frozen=True, slots=True)
class JudgmentUnit:
    """판정 1쌍(정방향+역방향)의 입력 — 아직 A/B 배정은 안 된 상태."""

    case_id: str
    pair: str
    rep_index: int
    version_low: str
    version_high: str
    row_low: GenerationRow
    row_high: GenerationRow


def select_judgment_units(
    rows: list[GenerationRow], *, pairs: tuple[tuple[str, str], ...] = PAIRS
) -> tuple[list[JudgmentUnit], dict[str, int]]:
    """생성물에서 판정 대상을 고른다 — 케이스×쌍마다 fallback 아닌 반복을 최대 2개.

    3반복으로 넉넉히 생성했으므로(`l1_1_generate.py`), 두 버전 모두 성공한 rep_index 를
    낮은 순서부터 최대 `REPS_PER_PAIR`(2)개 고른다. 부족하면(0~1개) 있는 만큼만 쓰고
    반환값 두 번째 원소(케이스×쌍별 부족분 카운트)에 정직하게 기록한다 — 조용히 넘기지
    않는다.
    """
    by_case_version_rep: dict[tuple[str, str, int], GenerationRow] = {}
    for row in rows:
        if not row.fell_back:
            by_case_version_rep[(row.case_id, row.version, row.repeat_index)] = row

    case_ids = sorted({row.case_id for row in rows})
    max_rep_index = max((r.repeat_index for r in rows), default=-1)
    units: list[JudgmentUnit] = []
    shortfalls: dict[str, int] = {}

    for case_id in case_ids:
        for version_low, version_high in pairs:
            common_reps = [
                rep
                for rep in range(max_rep_index + 1)
                if (case_id, version_low, rep) in by_case_version_rep
                and (case_id, version_high, rep) in by_case_version_rep
            ]
            chosen = common_reps[:REPS_PER_PAIR]
            key = f"{case_id}:{pair_key(version_low, version_high)}"
            if len(chosen) < REPS_PER_PAIR:
                shortfalls[key] = REPS_PER_PAIR - len(chosen)
            for rep in chosen:
                units.append(
                    JudgmentUnit(
                        case_id=case_id,
                        pair=pair_key(version_low, version_high),
                        rep_index=rep,
                        version_low=version_low,
                        version_high=version_high,
                        row_low=by_case_version_rep[(case_id, version_low, rep)],
                        row_high=by_case_version_rep[(case_id, version_high, rep)],
                    )
                )
    return units, shortfalls


def _candidate_payload(row: GenerationRow) -> dict[str, object]:
    """심판에게 넘길 블라인드 후보 JSON — 버전·case_id 등 정체 정보는 절대 포함 안 함.

    루브릭 §4-1: `prompt_version`/`prompt_id`/파일 경로를 절대 포함하지 않는다.
    """
    return {
        "strategy_code": row.strategy_code,
        "if_clause": row.if_clause,
        "then_clause": row.then_clause,
        "rationale": row.rationale,
        "obstacle": row.obstacle,
        "coping_clause": row.coping_clause,
        "acknowledgment": row.acknowledgment,
        "estimated_workload_change_minutes": row.estimated_workload_change_minutes,
    }


def build_judge_prompt(
    *,
    failure_type: str,
    strategy_label: str,
    strategy_group: str,
    base_template: str,
    context_summary: str,
    candidate_a_json: str,
    candidate_b_json: str,
) -> str:
    """`rubric-v1.md` §3 심판 프롬프트 원문 — 글자 그대로 옮긴 것, 임의 수정 없음."""
    return (
        "당신은 실행 코칭 카드의 품질을 채점하는 심사위원입니다. 아래 두 후보(A, B)는 같은\n"
        "상황에 대해 서로 다른 방식으로 만들어진 if-then 코칭 카드입니다. 어느 쪽이 더 나은지\n"
        "판단하지 말고, 각 후보를 5개 축에서 **독립적으로** 1~5점으로 채점하세요.\n"
        "\n"
        "# 상황\n"
        f"- 실패 사유: {failure_type}\n"
        f"- 룰이 고른 전략: {strategy_label} (그룹: {strategy_group})\n"
        f"- 카탈로그 기본 문구: {base_template}\n"
        f"- 실행 컨텍스트: {context_summary}\n"
        "\n"
        "# 후보 A\n"
        f"{candidate_a_json}\n"
        "\n"
        "# 후보 B\n"
        f"{candidate_b_json}\n"
        "\n"
        "# 채점 축 (각 1~5점, docs/experiments/rubric-v1.md §1 의 앵커를 그대로 적용)\n"
        '1. if절 단서 구체성 — 시간/장소/직전 행동이 구체적인가. 날짜어("오늘"/"내일")가 있으면\n'
        "   최대 3점.\n"
        "2. then절 unpacking — 시간만 자른 게 아니라 식별 가능한 하위 단계로 나눴는가.\n"
        "3. coping절 독립성 — then절과 다른, 장애물 대응이 있는가. then절의 재진술이면 4점을\n"
        "   넘지 않는다.\n"
        "4. 톤 — 아래 금지어가 치환 없이 그대로 있으면 1점(실격): 실패, 또 못, 안 됐, 못했,\n"
        "   왜 안, 다시 실수, 게으르, 한심, 패배, 포기. 원인이 사람에게 귀속되면 2점. 상황 탓 +\n"
        "   자기자비(공통 인간성/판단 없는 수용/증진적 신념)면 4~5점. 능력·자질 칭찬이 있으면\n"
        "   4점을 넘지 않는다.\n"
        '5. 사실 정합 — "실행 컨텍스트" 밖의 구체적 사실을 지어냈는가. 전략 그룹의 방향(줄이기/\n'
        "   미루기/이월/보류)을 유지했는가.\n"
        "\n"
        "# 출력 형식 (JSON, 다른 텍스트 금지)\n"
        "{\n"
        '  "candidate_a": {"axis1": <1-5>, "axis2": <1-5>, "axis3": <1-5>, "axis4": <1-5>, "axis5": <1-5>},\n'
        '  "candidate_b": {"axis1": <1-5>, "axis2": <1-5>, "axis3": <1-5>, "axis4": <1-5>, "axis5": <1-5>},\n'
        '  "axis4_disqualification_reason": "<A 또는 B 가 1점이면 어느 금지어가 걸렸는지, 아니면 null>"\n'
        "}"
    )


async def _call_judge(
    prompt_text: str,
    *,
    model: str,
    timeout: float,
    max_attempts: int,
) -> JudgeVerdict | None:
    """`provider.generate_structured()` 를 재시도와 함께 호출. 전부 실패하면 None(버림)."""
    for attempt in range(1, max_attempts + 1):
        try:
            validated, _ = await asyncio.wait_for(
                generate_structured(
                    schema=JudgeVerdict, prompt_text=prompt_text, timeout=timeout, model=model
                ),
                timeout=timeout,
            )
            return validated
        except ProviderUnavailable as exc:
            _log.error("judge unavailable (API key 없음?): %s", exc)
            return None
        except (TimeoutError, ProviderRateLimited, ProviderValidationError, ProviderError) as exc:
            _log.warning("judge 호출 실패 (시도 %d/%d): %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                await asyncio.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
    return None


async def judge_unit(
    unit: JudgmentUnit,
    *,
    forward_a_is_low: bool,
    model: str,
    timeout: float,
    max_attempts: int,
) -> list[JudgmentRow]:
    """단위 하나 → 정방향/역방향 판정 최대 2건 (실패한 방향은 빠진다)."""
    low_payload = _candidate_payload(unit.row_low)
    high_payload = _candidate_payload(unit.row_high)

    # forward_a_is_low 가 그 case×pair×rep 의 "정방향" A/B 배정을 고정한다.
    # 역방향(swap=True)은 항상 그 반대 — 독립적으로 다시 뽑지 않는다(사전등록 §6).
    if forward_a_is_low:
        forward = (False, unit.version_low, low_payload, unit.version_high, high_payload)
        reversed_ = (True, unit.version_high, high_payload, unit.version_low, low_payload)
    else:
        forward = (False, unit.version_high, high_payload, unit.version_low, low_payload)
        reversed_ = (True, unit.version_low, low_payload, unit.version_high, high_payload)

    rows: list[JudgmentRow] = []
    for swap, version_a, payload_a, version_b, payload_b in (forward, reversed_):
        prompt = build_judge_prompt(
            failure_type=unit.row_low.failure_type,
            strategy_label=unit.row_low.strategy_label,
            strategy_group=unit.row_low.strategy_group,
            base_template=unit.row_low.base_template,
            context_summary=unit.row_low.context_summary,
            candidate_a_json=json.dumps(payload_a, ensure_ascii=False),
            candidate_b_json=json.dumps(payload_b, ensure_ascii=False),
        )
        verdict = await _call_judge(prompt, model=model, timeout=timeout, max_attempts=max_attempts)
        if verdict is None:
            _log.warning(
                "판정 버림: case=%s pair=%s rep=%d swap=%s (심판 호출 최종 실패)",
                unit.case_id,
                unit.pair,
                unit.rep_index,
                swap,
            )
            continue
        rows.append(
            JudgmentRow(
                case_id=unit.case_id,
                pair=unit.pair,
                rep_index=unit.rep_index,
                swap=swap,
                version_a=version_a,
                version_b=version_b,
                axis_a=verdict.candidate_a.as_tuple(),
                axis_b=verdict.candidate_b.as_tuple(),
                disqualification_reason=verdict.axis4_disqualification_reason,
            )
        )
    return rows


async def run(
    units: list[JudgmentUnit],
    *,
    model: str = DEFAULT_JUDGE_MODEL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    seed: int = RANDOM_SEED,
) -> list[JudgmentRow]:
    rng = random.Random(seed)
    # 유닛 순서를 고정(case_id, pair, rep_index 로 이미 정렬돼 select_judgment_units 에서
    # 나온다)한 뒤 순서대로 난수를 뽑아야 재현 가능하다 — 병렬 실행 순서에 의존하면 안 됨.
    forward_assignments = [rng.random() < 0.5 for _ in units]

    all_rows: list[JudgmentRow] = []
    total = len(units)
    for i, (unit, forward_a_is_low) in enumerate(zip(units, forward_assignments, strict=True), 1):
        rows = await judge_unit(
            unit,
            forward_a_is_low=forward_a_is_low,
            model=model,
            timeout=timeout,
            max_attempts=max_attempts,
        )
        all_rows.extend(rows)
        if i % 25 == 0 or i == total:
            _log.info("progress %d/%d units (%d judgments so far)", i, total, len(all_rows))
    return all_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="l1_1_judge",
        description="L1-1 심판 하네스 — 생성물 쌍대비교 (사전등록 §2, 루브릭 §3).",
    )
    parser.add_argument(
        "--limit-cases", type=int, default=None, help="골든셋 앞 N건에 해당하는 판정만 (스모크)"
    )
    parser.add_argument(
        "--model", default=DEFAULT_JUDGE_MODEL, help="심판 모델 (기본: %(default)s)"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--dry-run", action="store_true", help="호출 없이 페어링·부족분·프롬프트 예시만 확인"
    )
    parser.add_argument(
        "--output", default=None, help="출력 경로 (기본: eval/l1_1_judgments.jsonl)"
    )
    parser.add_argument(
        "--input", default=None, help="입력 경로 (기본: eval/l1_1_generations.jsonl)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows = read_generations(GENERATIONS_PATH if args.input is None else Path(args.input))
    _log.info("생성물 %d행 로드", len(rows))

    if args.limit_cases is not None:
        case_ids = sorted({r.case_id for r in rows})[: args.limit_cases]
        rows = [r for r in rows if r.case_id in case_ids]
        _log.info("--limit-cases %d → %d행으로 축소", args.limit_cases, len(rows))

    units, shortfalls = select_judgment_units(rows)
    _log.info("판정 단위 %d개 (최대 %d 판정 예정)", len(units), len(units) * 2)
    if shortfalls:
        _log.warning(
            "%d개 (케이스,쌍) 조합이 반복 %d개를 못 채웠다(생성 fallback 이 많았을 수 있음): %s",
            len(shortfalls),
            REPS_PER_PAIR,
            shortfalls,
        )

    if args.dry_run:
        by_pair: dict[str, int] = defaultdict(int)
        for u in units:
            by_pair[u.pair] += 1
        _log.info("쌍별 단위 수: %s", dict(by_pair))
        if units:
            sample = units[0]
            prompt = build_judge_prompt(
                failure_type=sample.row_low.failure_type,
                strategy_label=sample.row_low.strategy_label,
                strategy_group=sample.row_low.strategy_group,
                base_template=sample.row_low.base_template,
                context_summary=sample.row_low.context_summary,
                candidate_a_json="{...}",
                candidate_b_json="{...}",
            )
            _log.info("샘플 프롬프트:\n%s", prompt)
        return 0

    all_rows = asyncio.run(run(units, model=args.model, timeout=args.timeout))

    output_path = JUDGMENTS_PATH if args.output is None else Path(args.output)
    write_judgments(all_rows, output_path)
    expected = len(units) * 2
    _log.info(
        "완료: %d/%d 판정 (%d건은 심판 호출 실패로 버림) → %s",
        len(all_rows),
        expected,
        expected - len(all_rows),
        output_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
