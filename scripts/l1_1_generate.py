"""L1-1 생성 하네스 — v1/v2/v3 를 골든셋 120건에 돌려 1,080건을 만든다.

`docs/experiments/preregistration-v1.md` §2 의 표본 설계를 그대로 구현한다: 골든셋
(`eval/golden_recovery_cases.jsonl`) 120건 각각에 실 프로덕션 라우트(`api/routes/recovery.py`
`generate_recovery_proposals`)와 **동일한 방식**으로 변수를 구성해, `recovery/if_then_proposal`
v1/v2/v3 각각 3반복(=1,080 호출) 돌린다. 판정(후속 PR `l1_1_judge.py`)이 그중 일부를
골라 쌍대비교한다 — 3반복인 이유는 판정 예산(1,440건, 사전등록 §2)을 채우기 전에 LLM
실패(fallback)로 못 쓰는 반복이 생겨도 버틸 여유분이다.

⚠️ **비용 경고**: 기본 실행은 실 Gemini 호출 1,080회다. `GEMINI_API_KEY` 가 없으면 전부
fallback 으로 기록되고(실행은 되지만 판정에 못 쓰는 데이터), 키가 있으면 실제 과금이
발생한다. `--limit`/`--versions`/`--repeats` 로 먼저 소규모로 스모크 테스트할 것.

`aiClient.run()` 을 그대로 통과한다(AGENTS.md §2 — LLM SDK 직접 import 금지, Tool Executor
경유). `session=None` 이라 budget check·`llm_runs` INSERT 는 건너뛴다(`prompt_lab.py` 와
같은 이유 — 이건 실 사용자 트래픽이 아니라 오프라인 연구용 호출이다).

실행:
  uv run python -m scripts.l1_1_generate                      # 전체 1,080건
  uv run python -m scripts.l1_1_generate --limit 2             # 골든셋 앞 2건만 (스모크)
  uv run python -m scripts.l1_1_generate --versions 1,2 --repeats 1  # 6건만
  uv run python -m scripts.l1_1_generate --dry-run              # 호출 없이 변수 구성만 확인
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from sqlalchemy import select

from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.orchestrator.recovery import render_template, select_strategies
from reaction_backend.schemas.recovery import RecoveryProposalLLM
from scripts.l1_1_common import (
    REPEATS_PER_VERSION,
    VERSIONS,
    GenerationRow,
    load_golden_cases,
    write_generations,
)

_log = logging.getLogger(__name__)

# routes/recovery.py 와 동일 — recovery 프로덕션 호출 조건 재현 (thinking 0 + timeout 12s).
_THINKING_BUDGET = 0
_TIMEOUT_SECONDS = 12.0


def build_variables(case: dict[str, Any], catalog: list[RecoveryStrategyCatalog]) -> dict[str, str]:
    """`routes/recovery.py::generate_recovery_proposals` 의 변수 구성과 1:1.

    골든셋 필드 → 변수 매핑:
    - `failure_tags` → `select_strategies` 입력이자 `failure_type` 변수(콤마 join).
    - `context.overwhelm_level` → `select_strategies(overwhelm_level=)` — 프로덕션
      라우트는 이 인자를 **아직 안 넘긴다**(`context_snapshots` 캡처 미완, #19-B-2 유예).
      골든셋의 `boundary` 블록은 정확히 이 동적 트리거를 시험하려고 overwhelm 3/4/5
      경계 케이스를 넣어 뒀으므로, 룰 엔진의 설계된 능력을 그대로 쓴다 — 이건 "프로덕션이
      지금 보내는 값 재현"이 아니라 "룰 엔진이 받을 수 있는 입력 재현"이라 다르다.
    - `action_item.title` → `first_step`/`suspended_step` 둘 다(골든셋엔 `first_step`
      필드가 없다 — 프로덕션도 `action.first_step or action_title` 이라 보통 이 경로).
    - `confidence`/`interruption_summary` → 라우트와 동일하게 하드코딩("n/a"/"없음").
      아직 실 데이터가 없는 필드를 풍부하게 채우면 "프로덕션이 실제로 보내는 컨텍스트"가
      아니라 "언젠가 보낼 수도 있는 컨텍스트"를 재는 셈이라, L1-1 의 목적(지금 배포된
      프롬프트 버전들의 실제 격차)과 어긋난다.
    """
    failure_tags: list[str] = case["failure_tags"]
    overwhelm_level = case["context"]["overwhelm_level"]
    selected = select_strategies(failure_tags, catalog, overwhelm_level=overwhelm_level)
    if not selected:
        raise ValueError(f"case {case['case_id']!r}: select_strategies 가 후보를 못 골랐다")
    top = selected[0]

    action_title = case["action_item"]["title"]
    base_template = render_template(
        top.if_then_template, {"first_step": action_title, "suspended_step": action_title}
    )
    completion_status = case["execution"]["completion_status"]

    return {
        "strategy_label": top.label_ko,
        "strategy_group": top.option_group,
        "base_template": base_template,
        "failure_type": ", ".join(failure_tags) if failure_tags else "UNKNOWN",
        "confidence": "n/a",
        "interruption_summary": "없음",
        "context_summary": f"실행 카드: {action_title} / 결과: {completion_status}",
    }


async def _generate_one(
    case_id: str, version: str, repeat_index: int, variables: dict[str, str]
) -> GenerationRow:
    from reaction_backend.llm import aiClient

    result = await aiClient.run(
        module="recovery",
        schema=RecoveryProposalLLM,
        prompt_id=f"recovery/if_then_proposal@v{version}",
        fallback=lambda: RecoveryProposalLLM(
            strategy_code=variables["strategy_group"].lower(),
            if_clause="",
            then_clause=variables["base_template"],
            rationale="",
        ),
        timeout=_TIMEOUT_SECONDS,
        thinking_budget=_THINKING_BUDGET,
        variables=variables,
        session=None,
        user_id=None,
    )
    v = result.value
    return GenerationRow(
        case_id=case_id,
        version=version,
        repeat_index=repeat_index,
        fell_back=result.fell_back,
        reason=result.reason,
        strategy_code=v.strategy_code,
        if_clause=v.if_clause,
        then_clause=v.then_clause,
        rationale=v.rationale,
        obstacle=v.obstacle,
        coping_clause=v.coping_clause,
        acknowledgment=v.acknowledgment,
        estimated_workload_change_minutes=v.estimated_workload_change_minutes,
        failure_type=variables["failure_type"],
        strategy_label=variables["strategy_label"],
        strategy_group=variables["strategy_group"],
        base_template=variables["base_template"],
        context_summary=variables["context_summary"],
    )


async def run(
    cases: list[dict[str, Any]],
    catalog: list[RecoveryStrategyCatalog],
    *,
    versions: tuple[str, ...] = VERSIONS,
    repeats: int = REPEATS_PER_VERSION,
) -> list[GenerationRow]:
    rows: list[GenerationRow] = []
    total = len(cases) * len(versions) * repeats
    done = 0
    for case in cases:
        variables = build_variables(case, catalog)
        for version in versions:
            for repeat_index in range(repeats):
                row = await _generate_one(case["case_id"], version, repeat_index, variables)
                rows.append(row)
                done += 1
                if row.fell_back:
                    _log.warning(
                        "fallback: case=%s v%s rep=%d reason=%s",
                        case["case_id"],
                        version,
                        repeat_index,
                        row.reason,
                    )
                if done % 50 == 0 or done == total:
                    _log.info("progress %d/%d", done, total)
    return rows


async def _load_catalog() -> list[RecoveryStrategyCatalog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(RecoveryStrategyCatalog))
        return list(result.scalars().all())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="l1_1_generate",
        description="L1-1 프롬프트 v1/v2/v3 생성 하네스 (사전등록 §2).",
    )
    parser.add_argument("--limit", type=int, default=None, help="골든셋 앞 N건만 (스모크 테스트)")
    parser.add_argument("--versions", default=",".join(VERSIONS), help="예: 1,2,3 (기본 전체)")
    parser.add_argument("--repeats", type=int, default=REPEATS_PER_VERSION, help="버전별 반복 횟수")
    parser.add_argument(
        "--dry-run", action="store_true", help="LLM 호출 없이 변수 구성만 확인하고 종료"
    )
    parser.add_argument(
        "--output", default=None, help="출력 JSONL 경로 (기본: eval/l1_1_generations.jsonl)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    versions = tuple(v.strip() for v in args.versions.split(",") if v.strip())
    cases = load_golden_cases(limit=args.limit)
    _log.info("골든셋 %d건 로드", len(cases))

    catalog = asyncio.run(_load_catalog())
    _log.info("카탈로그 %d개 전략 로드", len(catalog))

    if args.dry_run:
        for case in cases:
            variables = build_variables(case, catalog)
            _log.info("case=%s variables=%s", case["case_id"], variables)
        total = len(cases) * len(versions) * args.repeats
        _log.info(
            "dry-run 완료 — 실 호출 %d건이 생성될 예정이었다 (버전=%s, 반복=%d)",
            total,
            versions,
            args.repeats,
        )
        return 0

    rows = asyncio.run(run(cases, catalog, versions=versions, repeats=args.repeats))

    from pathlib import Path

    from scripts.l1_1_common import GENERATIONS_PATH

    output_path = GENERATIONS_PATH if args.output is None else Path(args.output)
    write_generations(rows, output_path)
    n_fallback = sum(1 for r in rows if r.fell_back)
    _log.info("완료: %d건 생성 (%d건 fallback) → %s", len(rows), n_fallback, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
