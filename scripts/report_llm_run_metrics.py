"""LLM 호출 시스템 지표 — p95 지연·fallback 원인별 분해·비용, 읽기 전용 (L1-4 예비 실행).

`docs/experiments/experiment-plan-v1.md` §2 L1-4 는 프롬프트 v1/v2/v3 비교 실험(3버전 ×
120건 × 5회 = 1,800 호출)의 시스템 지표를 요구한다. v3 가 아직 없어 그 비교 자체는 못 하지만,
**지금 있는 v2 트래픽만으로 기준선(baseline)을 먼저 뽑는 것**이 §11 이번 주 우선순위의
"L1-4 예비 실행" 항목이다 — v3 가 생겼을 때 "얼마나 늘었나"를 잴 대조군이 없으면 그 비교
자체가 성립하지 않는다.

데이터 출처는 `llm_runs`(이미 존재) 하나뿐이고, 이 스크립트는 그 위에서 **집계만** 한다.
신규 테이블도, 쓰기도 없다.

## fallback 원인 분해 — 계획서의 "3분해"와 실제 `reason` 코드의 대응

계획서 §2 L1-4 는 "timeout / 형식검증 실패 / 톤게이트 거부" 3분해를 요구하며 이를 위해
"`tone_gate_rejected` 를 별도 컬럼으로"를 전제했다. **별도 컬럼 대신 이미 있던
`llm_runs.reason` 문자열 컬럼에 새 값을 추가하는 쪽을 택했다**(`safety/tone_gate.py`,
S6 톤·구조 게이트 구현) — reason 컬럼이 이미 이 3분해를 포함하는 상위 집합이라, 의미가
겹치는 두 번째 컬럼을 또 만들면 어느 쪽이 진실인지 갈라질 수 있기 때문이다. 지금
`llm_runs.reason`(9종: `rate_limited`/`timeout`/`validation`/`budget`/`banned`/
`tone_gate`/`unavailable`/`no_prompt`/`provider_error`)의 대응표:

| 계획서 범주 | 지금의 `reason` 코드 |
|---|---|
| timeout | `timeout` |
| 형식검증 실패 | `validation` |
| 톤게이트 거부 | **`tone_gate`** — `safety/tone_gate.py::check_structured()`, 사람 귀인·
  자존감 부양 마커 검출 시. `banned`(명사 1:1 치환이 실패한 경우, `HARD_BLOCK_TERMS`
  비어 있어 사실상 거의 안 남)와는 이제 분리된 값이다 |
| (계획서에 없음) | `rate_limited`/`budget`/`unavailable`/`no_prompt`/`provider_error` —
  계획서가 예상 못 한 나머지 원인. 3분해보다 세분화됐을 뿐 상위 호환이다 |

## 지연 지표의 한계 (정직하게 밝힘)

계획서는 "카드 1장 완성까지의 end-to-end(재생성 포함)"를 요구한다 — 톤 게이트가 위반 시
**동기 재생성**을 한다면 최악 2배가 요청 경로에 들어가는데, 단발 호출만 재면 그걸 놓친다는
경고다. **이 경고는 여전히 발동하지 않는다**: `safety.banned_words.enforce_structured`
는 치환, `safety.tone_gate.check_structured`(S6, 이제 구현됨)는 **재생성이 아니라 즉시
거부(reject)** 다 — 둘 다 재생성 스텝이 없다(`llm/tool_executor.py` §4-5). 그래서 지금의
`latency_ms`(재시도 루프 포함, 단일 `aiClient.run()` 호출 전체)가 여전히 "카드 1장
완성까지"와 같다. **톤 게이트가 나중에 재생성 방식으로 바뀌면 이 문장부터 재검증할 것.**

**아무것도 쓰지 않는다** — SELECT 뿐. `--apply` 같은 옵션 자체가 없다
(선례: `report_recovery_followthrough.py`).

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.report_llm_run_metrics
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.db.session import get_sessionmaker
from reaction_backend.schemas.common import now_kst, to_kst

# 계획서 §2 L1-4 의 "3분해"가 매핑되는 reason 코드 — 나머지는 "기타"로 묶어 보고한다.
_PLANNED_REASON_LABELS: dict[str, str] = {
    "timeout": "timeout",
    "validation": "형식검증 실패",
    "tone_gate": "톤게이트 거부",
    "banned": "금지어 치환 실패(HARD_BLOCK_TERMS)",
}


class RunRow(NamedTuple):
    """LlmRun 에서 집계에 필요한 컬럼만 뽑은 것."""

    module: str
    prompt_version: str
    model: str
    latency_ms: int
    tokens_in: int
    tokens_out: int
    cost_micro_usd: int
    grounding_requests: int
    success: bool
    fell_back: bool
    reason: str | None


async def _fetch_runs(session: AsyncSession) -> list[RunRow]:
    stmt = select(
        LlmRun.module,
        LlmRun.prompt_version,
        LlmRun.model,
        LlmRun.latency_ms,
        LlmRun.tokens_in,
        LlmRun.tokens_out,
        LlmRun.cost_micro_usd,
        LlmRun.grounding_requests,
        LlmRun.success,
        LlmRun.fell_back,
        LlmRun.reason,
    )
    rows = (await session.execute(stmt)).all()
    return [RunRow(*r) for r in rows]


def _percentile(values: list[int], pct: float) -> float:
    """최근접 순위(nearest-rank) 백분위수. 빈 리스트는 0.0(호출자가 count==0 을 먼저 본다)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return float(ordered[rank])


class Summary(NamedTuple):
    """한 그룹(전체 또는 module×prompt_version)의 집계 결과."""

    count: int
    success_n: int
    fallback_n: int
    fallback_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    mean_latency_ms: float
    total_tokens_in: int
    total_tokens_out: int
    total_cost_micro_usd: int
    total_grounding_requests: int
    fallback_reasons: Counter[str]


def _summarize(rows: list[RunRow]) -> Summary:
    count = len(rows)
    success_n = sum(1 for r in rows if r.success)
    fallback_n = sum(1 for r in rows if r.fell_back)
    latencies = [r.latency_ms for r in rows]
    reasons = Counter(r.reason for r in rows if r.fell_back and r.reason is not None)

    return Summary(
        count=count,
        success_n=success_n,
        fallback_n=fallback_n,
        fallback_rate=(fallback_n / count) if count else 0.0,
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        mean_latency_ms=(sum(latencies) / count) if count else 0.0,
        total_tokens_in=sum(r.tokens_in for r in rows),
        total_tokens_out=sum(r.tokens_out for r in rows),
        total_cost_micro_usd=sum(r.cost_micro_usd for r in rows),
        total_grounding_requests=sum(r.grounding_requests for r in rows),
        fallback_reasons=reasons,
    )


def _print_summary(label: str, s: Summary) -> None:
    print(f"■ {label} — {s.count}건")
    if s.count == 0:
        print("  (데이터 없음)")
        return
    print(f"  성공 {s.success_n} / fallback {s.fallback_n} (fallback rate {s.fallback_rate:.1%})")
    print(
        f"  지연(ms) p50={s.p50_latency_ms:.0f} p95={s.p95_latency_ms:.0f} "
        f"mean={s.mean_latency_ms:.0f}"
    )
    avg_in = s.total_tokens_in / s.count
    avg_out = s.total_tokens_out / s.count
    print(
        f"  토큰 합계 in={s.total_tokens_in} out={s.total_tokens_out} "
        f"(평균 in={avg_in:.0f} out={avg_out:.0f})"
    )
    print(f"  비용 합계 ${s.total_cost_micro_usd / 1_000_000:.4f}")
    if s.total_grounding_requests:
        # 그라운딩은 토큰과 **별도 과금**이라 위 비용 합계에 안 들어간다(#259 §3).
        # 무료 5,000건/월, 초과분 $14/1,000건 — 건수를 따로 보여줘야 장부가 맞는다.
        over_free_usd = s.total_grounding_requests * 14 / 1000
        print(
            f"  그라운딩 요청 {s.total_grounding_requests}건 "
            f"(무료분 초과 시 ~${over_free_usd:.2f} — 위 비용 합계에 미포함)"
        )
    if s.fallback_reasons:
        print("  fallback 원인:")
        for reason, n in sorted(s.fallback_reasons.items(), key=lambda kv: -kv[1]):
            planned = _PLANNED_REASON_LABELS.get(reason, "기타(계획서 3분해 밖)")
            pct = n / s.fallback_n if s.fallback_n else 0.0
            print(f"    {reason:15s} {n:4d}건 ({pct:.1%} of fallback) — {planned}")


async def _preview(session: AsyncSession) -> None:
    print(f"기준 시각: {now_kst().isoformat()}")
    print("분모: llm_runs 전체 행(module 무관). 신규 컬럼·백필 없음 — 있는 데이터만 집계.")
    print()

    rows = await _fetch_runs(session)
    if not rows:
        print("llm_runs 0건 — 잴 데이터가 없다.")
        return

    _print_summary("전체", _summarize(rows))
    print()

    by_group: dict[tuple[str, str], list[RunRow]] = defaultdict(list)
    for r in rows:
        by_group[(r.module, r.prompt_version)].append(r)

    print("■ 그룹별 (module × prompt_version)")
    for (module, version), group_rows in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
        print()
        _print_summary(f"{module} @ v{version}", _summarize(group_rows))

    print()
    print(
        "※ 이 스크립트는 아무것도 쓰지 않았다. v3 가 생기면 이 출력을 기준선으로 "
        "docs/experiments/experiment-plan-v1.md §2 L1-4 의 본 실험(1,800 호출)과 대조한다."
    )


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[report-llm-run-metrics] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
