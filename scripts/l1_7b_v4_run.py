"""L1-7B v4 하네스 — `plan_quality_eval.v4` **평가 후보** 단독 측정 (실 LLM 호출).

`scripts/l1_7b_run.py`(v3 기준선)와 **별도 파일**이다. 그쪽은 손대지 않는다 — v3 결과가
비교 기준선이고, 한 파일에서 두 계약을 분기시키면 어느 쪽 수치인지 나중에 못 가른다.

## 이 실행이 재는 것 — 정확히 이 문장으로 보고한다

> **`plan_quality_eval.v4` 가, ③층 보정을 거쳐 이미 정상인 고정 계획 50건에 대해,
> 루브릭 §2 의 D1~D5 를 얼마나 잘 지목하는가.**

⚠️ **"일반 결함 발견 능력" 이 아니다.** 이 하네스가 재는 것은 **이 루브릭 준수도**다.
심은 결함은 루브릭 §2 의 앵커를 그대로 따라 만들어졌고, v4 프롬프트도 같은 앵커를 싣는다.
루브릭이 못 적은 실패 유형은 **여기서 영원히 측정되지 않는다**(실험계획서 §2 "결함 설계의
순환성").

⚠️ **v4 는 평가 후보이지 프로덕션이 아니다.** ④층 `review_plan` 은 여전히 v3 를 부른다.
승격 조건은 `docs/experiments/l1-7b-v4-results.md` 에 적는다.

## v3 와 무엇이 다른가

| | v3 (기준선) | v4 (이 하네스) |
|---|---|---|
| 출력 | `{approved, feedback: list[str]}` | `{findings: [{defect, severity, node_id, message}]}` |
| 승인 판정 | **LLM 이 한다** | **코드가 한다** — `severity >= 2` 존재 여부 |
| 넘기는 변수 | 6종 (`focus_capacity`·`session_length` 포함) | **2종** — 계획 트리와 카드뿐 |
| 낼 수 있는 지표 | M27a · M29 · M32 | **+ M27b · M27gap · M28a · M28b** |

`focus_capacity`·`session_length` 를 뺀 이유는 루브릭 §1.2 다 — ③층이 이미 불변식으로
보장해 상한 초과는 보정 후 **0/1,620** 이고, 그 항목의 반려는 정의상 전부 오탐이다.
**변수를 주고 "보지 마라" 고 쓰는 것으로는 지킨다는 보장이 없어** 변수 자체를 넘기지 않는다.

## 표본 단위 — v3 와 같은 규칙

주 지표의 1차 추정은 **사전 지정된 `PRIMARY_REPEAT` 의 고유 케이스**다. 반복 3회를 150개
독립 표본으로 세지 않는다 — 같은 케이스의 반복은 상관이 있어 신뢰구간이 거짓으로 좁아진다.

## M27b·M28a·M28b 는 `easy` 에서만 낸다

`boundary` 는 **통과가 정답**인 수준이다(루브릭 §2). 거기서 recall·위치지목을 재면 "맞히면
안 되는 것을 맞혔는가" 를 세게 된다. boundary 는 M27a 의 **오탐** 쪽으로만 보고한다.

## 실행

    uv run python scripts/l1_7b_v4_run.py --dry-run          # LLM 없이 구성 확인
    uv run python scripts/l1_7b_v4_run.py --limit 3          # 스모크 (본 실행 전 필수)
    uv run python scripts/l1_7b_v4_run.py --repeats 3        # 본 실행
    uv run python scripts/l1_7b_v4_run.py --summarize-only   # 저장된 원자료 재집계

원자료 `eval/l1_7b_v4_results.jsonl` (비결정적이라 `.gitignore`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # `python scripts/...` 직접 실행 — pytest 는 이미 잡혀 있다
    sys.path.insert(0, str(_ROOT))

from reaction_backend.schemas.planning import (  # noqa: E402
    REJECT_SEVERITY_THRESHOLD,
    PlanFinding,
    PlanReviewV4,
    approved_from_findings,
)
from scripts.check_seeded_defect_shortcuts import m28_leaked_case_ids  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CASES_PATH = _ROOT / "eval" / "golden_first_plan_cases.jsonl"
RESULTS_PATH = _ROOT / "eval" / "l1_7b_v4_results.jsonl"

PROMPT_ID = "planning/plan_quality_eval@v4"
"""**버전을 고정해 부른다.** 레지스트리의 `latest()` 는 최고 버전을 자동 선택하므로,
버전을 빼면 v5 를 만드는 순간 이 하네스가 조용히 다른 것을 재게 된다."""

# 주 지표의 1차 추정에 쓰는 반복 회차. **사전 지정**이다 — 결과를 보고 고르면 안 된다.
PRIMARY_REPEAT = 0


def one_sided_upper_95(k: int, n: int) -> float:
    """Clopper-Pearson 단측 95% 상한 — scipy 없이.

    `k=0` 이면 닫힌 형태 `1 − 0.05^(1/n)`. 그 외에는 이분 탐색으로
    `P(X ≤ k | n, p) = 0.05` 인 p 를 찾는다(정규 근사를 쓰지 않는다).

    ⚠️ `scripts/l1_7b_run.py` 와 **같은 구현이 두 벌 있다.** 하네스는 서로 import 하지
    않는다 — v3 기준선 파일을 이 작업에서 건드리지 않기로 했기 때문이다. 값이 갈리지
    않는지는 `tests/test_plan_quality_v4.py` 가 두 구현을 대조해 지킨다.
    """
    if n <= 0:
        return 1.0
    if k <= 0:
        return 1.0 - 0.05 ** (1 / n)
    if k >= n:
        return 1.0

    def cdf(p: float) -> float:
        total, comb = 0.0, 1.0
        for i in range(k + 1):
            if i:
                comb = comb * (n - i + 1) / i
            total += comb * (p**i) * ((1 - p) ** (n - i))
        return total

    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if cdf(mid) > 0.05:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def load_cases(limit: int | None = None, blocks: list[str] | None = None) -> list[dict[str, Any]]:
    """`verify` 케이스만 읽는다 — 계획이 이미 고정돼 있는 것들.

    ⚠️ `limit` 은 **블록마다 앞 N건**이다. 단순 head 슬라이스로 두면 골든셋 파일 순서상
    `seeded_defect` 가 앞이라 `--limit 3` 이 `defect_free_control` 30건을 통째로 건너뛴다.
    그러면 **사전등록의 유일한 절대 임계값이자 최우선 감시인 M29 경로가 스모크에서 한 번도
    실행되지 않는다** — 본 실행에서 처음 도는 코드가 생긴다.
    """
    rows = [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = [c for c in rows if c["kind"] == "verify"]
    if blocks:
        cases = [c for c in cases if c["block"] in blocks]
    if limit is None:
        return cases
    taken: dict[str, int] = defaultdict(int)
    out = []
    for c in cases:
        if taken[c["block"]] < limit:
            taken[c["block"]] += 1
            out.append(c)
    return out


# ③층이 이미 보장한 값이라 ④층이 보면 안 되는 카드 필드 (루브릭 §1.2).
_STRIPPED_ITEM_FIELDS = ("estimated_minutes",)


def review_variables_v4(case: dict[str, Any]) -> dict[str, str]:
    """v4 변수 계약 — **2종뿐이고, 카드에서 분량을 지운다.**

    ⚠️ `focus_capacity`·`session_length` 는 변수로 넘기지 않는다(루브릭 §1.2).

    ⚠️ **그것만으로는 부족하다.** `action_items` 의 각 카드가 `estimated_minutes` 를 싣고
    있으면 상한값은 사라져도 **실현된 분량은 그대로 보인다** — "카드가 전부 120분이다",
    "길이가 제각각이다" 는 이 값만으로 판단 가능하다. 그러면 §1.2 의 강제 방식("프롬프트에
    '보지 마라'가 아니라 변수 자체를 안 넘긴다")이 파생값에서 깨진다. D1~D5 어느 유형도
    분량을 요구하지 않으므로 **필드를 지운다.**

    같은 조치가 **M28b 위치 누출도 닫는다.** 누출의 정체는 "심은 카드가 계획의 유일한 비최빈
    분량이라 `argmin(분량)` 으로 정답 키가 나온다" 인데(`_is_unique_non_modal_minutes`),
    분량을 안 보여주면 그 경로 자체가 없다.

    `conflict_report`·`time_policy_summary` 도 넘기지 않는다 — v4 출력 계약에는 D1~D5 코드가
    붙은 finding 밖에 없어 **충돌을 전달할 통로가 없다.** 넘기면 잘못된 코드가 붙은 finding
    으로만 나올 수 있다.
    """
    plan = case["plan"]
    items = [
        {k: v for k, v in item.items() if k not in _STRIPPED_ITEM_FIELDS}
        for item in plan["action_items"]
    ]
    return {
        "goal_nodes_json": json.dumps(plan["goal_nodes"], ensure_ascii=False),
        "action_items_json": json.dumps(items, ensure_ascii=False),
    }


def plan_leaf_ids(case: dict[str, Any]) -> set[str]:
    """검토기가 지목해도 되는 `node_id` 전체 — 채움 카드(`tmp-continue-*`)도 실재한다."""
    return {a["node_id"] for a in case["plan"]["action_items"]}


async def run_case(case: dict[str, Any], repeat: int, *, dry_run: bool) -> dict[str, Any]:
    from reaction_backend.config import get_settings
    from reaction_backend.llm import aiClient

    variables = review_variables_v4(case)
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "block": case["block"],
        "repeat": repeat,
        "expected_approved": case["expected"]["approved"],
    }
    if case["block"] == "seeded_defect":
        row["defect"] = case["seeded"]["defect"]
        row["level"] = case["seeded"]["level"]
    if dry_run:
        row["vars"] = {k: v[:60] for k, v in variables.items()}
        return row

    settings = get_settings()
    result = await aiClient.run(
        module="planning",
        schema=PlanReviewV4,
        prompt_id=PROMPT_ID,
        # ⚠️ 룰 폴백은 "검토기가 뭘 했나" 에 대해 아무것도 말하지 않는다 — 집계에서 뺀다.
        fallback=lambda: PlanReviewV4(findings=[]),
        timeout=settings.llm_planning_timeout_seconds,
        thinking_budget=settings.llm_planning_thinking_budget,
        variables=variables,
        session=None,
        user_id=None,
    )
    row.update(
        fell_back=result.fell_back,
        reason=result.reason,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        latency_ms=result.latency_ms,
    )
    if not result.fell_back:
        # 모델 출력을 그대로 남긴다 — 임계값·집계 정의가 바뀌어도 재호출 없이 되살린다.
        row["findings"] = [f.model_dump() for f in result.value.findings]
    return row


def _pct(num: int, den: int) -> str:
    return "—" if den == 0 else f"{num / den:.3f} ({num}/{den})"


def _findings(row: dict[str, Any]) -> list[PlanFinding]:
    return [PlanFinding.model_validate(f) for f in row.get("findings", [])]


def classify_findings(
    row: dict[str, Any], case: dict[str, Any], *, threshold: int
) -> dict[str, Any]:
    """한 행의 finding 들을 계획과 대조해 판정 재료를 만든다.

    **없는 `node_id` 를 조용히 받아들이지 않는다.** 계획에 없는 id 는 `invalid` 로 세고,
    ① 그 finding 은 M28b(위치 지목)의 **분자에서 빠지지만 분모에는 남는다** — 지어낸 노드는
    위치 지목 실패이지 면제 사유가 아니다 ② 행 단위 `has_invalid_node` 로 표시해 스키마
    준수율을 따로 보고한다.

    ⚠️ **유형 지표(M27b·M28a)는 `node_id` 유효성을 보지 않는다.** 그건 "무엇을 짚었나" 를
    묻는 지표이고 "어디를 짚었나" 는 M28b 의 몫이라 정의상 분리돼 있다. 다만 그 결과
    **존재하지 않는 카드를 지목한 finding 도 유형 적중으로는 계상된다** — 그래서 스키마
    준수율(`has_invalid_node`)을 지표들보다 **먼저** 출력한다.
    """
    valid_ids = plan_leaf_ids(case)
    findings = _findings(row)
    # 반려 판정은 스키마 모듈의 단일 구현을 쓴다 — 여기서 다시 쓰면 두 곳이 갈린다.
    approved = approved_from_findings(findings, threshold=threshold)
    rejecting = [f for f in findings if f.severity >= threshold]
    invalid = [f for f in findings if f.node_id not in valid_ids]
    targets = set(case.get("seeded", {}).get("target_node_ids", []))
    seeded_defect = case.get("seeded", {}).get("defect")
    # M27b·M28a·M28b 는 **모두 `rejecting` 만 본다.** 셋이 서로 다른 severity 규칙을 쓰면
    # 같은 파일 안에서 지표가 갈린다(독립 검증 지적). 반려를 만들지 않은 severity 1 메모는
    # 어떤 적중으로도 세지 않는다.
    return {
        "approved": approved,
        "n_findings": len(findings),
        "n_invalid_nodes": len(invalid),
        "has_invalid_node": bool(invalid),
        "hit_type": bool(seeded_defect) and any(f.defect == seeded_defect for f in rejecting),
        "hit_node": bool(targets) and any(f.node_id in targets for f in rejecting),
        "defect_codes": sorted({f.defect for f in rejecting}),
    }


def compute_metrics(rows: list[dict[str, Any]], *, threshold: int) -> dict[str, Any]:
    """원자료 → 지표 dict. **순수 함수라 테스트가 닿는다.**

    ⚠️ 출력(`summarize`)과 계산을 나눈 이유 — 독립 검증이 집계 함수에 테스트가 한 줄도 안
    닿는 것을 뮤테이션으로 증명했다(분모·easy 한정·누출 제외를 동시에 뒤집어도 전 테스트
    초록). 분모를 정하는 코드가 이 작업의 유일한 산출물인데 검증 밖에 있었다.

    지표 정의는 `docs/experiments/experiment-plan-v1.md` §5 가 단일 진실 소스다. 이 함수가
    §5 와 갈리면 **§5 를 고치고 여기를 맞춘다** — 반대가 아니다.
    """
    cases = {c["case_id"]: c for c in load_cases()}
    leaked = set(m28_leaked_case_ids(list(cases.values())))

    usable = [r for r in rows if not r.get("fell_back") and "findings" in r]
    cls = {
        (r["case_id"], r["repeat"]): classify_findings(r, cases[r["case_id"]], threshold=threshold)
        for r in usable
    }

    def c(r: dict[str, Any]) -> dict[str, Any]:
        return cls[(r["case_id"], r["repeat"])]

    m: dict[str, Any] = {
        "threshold": threshold,
        "n_rows": len(rows),
        "n_usable": len(usable),
        "n_fallback": sum(1 for r in rows if r.get("fell_back")),
        "fallback_reasons": dict(
            Counter(r.get("reason") or "?" for r in rows if r.get("fell_back"))
        ),
    }
    if not usable:
        return m

    m["schema"] = {
        "rows_with_invalid": sum(1 for r in usable if c(r)["has_invalid_node"]),
        "invalid_findings": sum(c(r)["n_invalid_nodes"] for r in usable),
        "total_findings": sum(c(r)["n_findings"] for r in usable),
        "cases": sorted({r["case_id"] for r in usable if c(r)["has_invalid_node"]}),
    }
    # 전 반복에 걸친 대조군 finding — repeat 0 만 보는 M29 보다 강한 관찰이다.
    ctl_all = [r for r in usable if r["block"] == "defect_free_control"]
    m["control_findings_all_repeats"] = sum(c(r)["n_findings"] for r in ctl_all)
    m["control_rows_all_repeats"] = len(ctl_all)

    primary = [r for r in usable if r["repeat"] == PRIMARY_REPEAT]

    ctl = [r for r in primary if r["block"] == "defect_free_control"]
    if ctl:
        rej = [r for r in ctl if not c(r)["approved"]]
        m["m29"] = {
            "k": len(rej),
            "n": len(ctl),
            "upper": one_sided_upper_95(len(rej), len(ctl)),
            "rejected": sorted(r["case_id"] for r in rej),
        }

    seeded = [r for r in primary if r["block"] == "seeded_defect"]
    if seeded:
        by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in seeded:
            by[r["defect"]].append(r)
        m["m27a"] = {
            d: {
                lvl: {
                    "rej": sum(1 for r in sub if not c(r)["approved"]),
                    "n": len(sub),
                }
                for lvl in ("easy", "boundary")
                for sub in [[r for r in by[d] if r["level"] == lvl]]
            }
            for d in sorted(by)
        }
        # ⚠️ M27b·M27gap·M28a·M28b 는 **easy 에서만** 낸다. `boundary` 는 통과가 정답인
        # 수준이라(루브릭 §2) 거기서 recall·위치지목을 재면 "맞히면 안 되는 것을 맞혔는가"
        # 를 세게 된다. 이 한정은 §5 에 기록돼 있다.
        easy_all = [r for r in seeded if r["level"] == "easy"]
        m["m27b"] = {
            d: {
                "hit": sum(1 for r in by[d] if r["level"] == "easy" and c(r)["hit_type"]),
                "n": sum(1 for r in by[d] if r["level"] == "easy"),
            }
            for d in sorted(by)
        }
        rejected_easy = [r for r in easy_all if not c(r)["approved"]]
        gap = [r for r in rejected_easy if not c(r)["hit_type"]]
        m["m27gap"] = {
            "k": len(gap),
            "n": len(rejected_easy),
            "cases": [
                {"case_id": r["case_id"], "seeded": r["defect"], "named": c(r)["defect_codes"]}
                for r in sorted(gap, key=lambda x: x["case_id"])
            ],
        }
        m["m28a"] = {
            "k": sum(1 for r in rejected_easy if c(r)["hit_type"]),
            "n": len(rejected_easy),
        }
        clean = [r for r in rejected_easy if r["case_id"] not in leaked]
        m["m28b"] = {
            "k": sum(1 for r in clean if c(r)["hit_node"]),
            "n": len(clean),
            # 제외 없이 센 값 — `estimated_minutes` 를 안 넘기면서 누출 경로가 닫혔으므로
            # 이 값도 이제 해석 가능하다. 그래도 **주 지표는 제외 후**로 둔다.
            "full_k": sum(1 for r in rejected_easy if c(r)["hit_node"]),
            "full_n": len(rejected_easy),
            "excluded": sorted({r["case_id"] for r in rejected_easy if r["case_id"] in leaked}),
            "leak_list": sorted(leaked),
        }

    reps = sorted({r["repeat"] for r in usable})
    if len(reps) > 1:
        by_case: dict[str, list[bool]] = defaultdict(list)
        for r in usable:
            by_case[r["case_id"]].append(bool(c(r)["approved"]))
        pairs = agree = 0
        flips = []
        for cid, vs in by_case.items():
            for i in range(len(vs)):
                for j in range(i + 1, len(vs)):
                    pairs += 1
                    agree += vs[i] == vs[j]
            if len(set(vs)) > 1:
                flips.append(cid)
        m["m32"] = {
            "agree": agree,
            "pairs": pairs,
            "flips": sorted(flips),
            "n_cases": len(by_case),
            "dist": dict(
                Counter(
                    "all_approve" if all(v) else "all_reject" if not any(v) else "split"
                    for v in by_case.values()
                )
            ),
            "repeats": len(reps),
        }

    sweep: dict[int, dict[str, int]] = {}
    for t in (2, 3):
        c2 = {
            (r["case_id"], r["repeat"]): classify_findings(r, cases[r["case_id"]], threshold=t)
            for r in primary
        }
        ctl2 = [r for r in primary if r["block"] == "defect_free_control"]
        ez2 = [r for r in primary if r["block"] == "seeded_defect" and r.get("level") == "easy"]
        sweep[t] = {
            "m29_k": sum(1 for r in ctl2 if not c2[(r["case_id"], r["repeat"])]["approved"]),
            "m29_n": len(ctl2),
            "m27b_k": sum(1 for r in ez2 if c2[(r["case_id"], r["repeat"])]["hit_type"]),
            "m27b_n": len(ez2),
        }
    m["sweep"] = sweep

    lat = sorted(r["latency_ms"] for r in usable if r.get("latency_ms"))
    if lat:
        m["latency"] = {
            "median": statistics.median(lat),
            "p95": lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)],
            "tokens_in": sum(r.get("tokens_in") or 0 for r in usable),
            "tokens_out": sum(r.get("tokens_out") or 0 for r in usable),
        }
    return m


def summarize(rows: list[dict[str, Any]], *, threshold: int) -> None:
    """`compute_metrics` 의 결과를 사람이 읽는 형태로 찍는다 — 계산은 여기서 안 한다."""
    m = compute_metrics(rows, threshold=threshold)
    print(f"\n{'=' * 76}\nL1-7B v4 — `{PROMPT_ID}` (평가 후보 · 프로덕션 아님)")
    print(f"반려 임계값 severity >= {threshold}  (코드가 정한다 — LLM 이 아니라)")
    print(f"실행 {m['n_rows']}건 / 집계 {m['n_usable']}건 / 룰 폴백 {m['n_fallback']}건(집계 제외)")
    if m["n_fallback"]:
        print("  폴백 사유:", m["fallback_reasons"])
    if not m["n_usable"]:
        print("  집계할 것이 없다.")
        return

    sc = m["schema"]
    print("\n── 스키마 준수 (계약을 지켰는가)")
    print(f"   계획에 없는 node_id 를 낸 행 {_pct(sc['rows_with_invalid'], m['n_usable'])}")
    print(
        f"   그런 finding {_pct(sc['invalid_findings'], sc['total_findings'])}"
        f"   · finding 총 {sc['total_findings']}건"
    )
    if sc["cases"]:
        print(f"   ⚠️ 해당 케이스: {', '.join(sc['cases'][:10])}")
        print("   ⚠️ 지어낸 노드는 M28b 분자에서 빠지고 **분모에는 남는다**(면제 아님).")
        print("   ⚠️ 다만 유형 지표(M27b·M28a)는 위치를 안 보므로 그대로 계상된다.")

    if "m29" in m:
        d = m["m29"]
        verdict = "임계값 아래" if d["upper"] <= 0.10 else "**임계값을 확인할 수 없다**"
        print(f"\n── M29 `false_reject_rate` (repeat {PRIMARY_REPEAT} 의 고유 {d['n']}건)")
        print(
            f"   오반려 {_pct(d['k'], d['n'])}   단측 95% 상한 {d['upper']:.3f}"
            f"  → 사전등록 ≤0.10 대비 {verdict}"
        )
        if d["rejected"]:
            print(f"   반려된 대조군: {', '.join(d['rejected'])}")
        print(
            f"   [참고] 전 반복 대조군 {m['control_rows_all_repeats']}행에서 나온 finding "
            f"{m['control_findings_all_repeats']}건"
        )

    if "m27a" in m:
        print(f"\n── M27a `verifier_rejection_rate` (repeat {PRIMARY_REPEAT}, **유형별**)")
        print("   ⚠️ '의미를 이해해 탐지했다' 가 아니라 **'이 고정 골든셋에서 반려했다'** 까지만.")
        for d, v in m["m27a"].items():
            print(
                f"   {d}  easy 반려 {_pct(v['easy']['rej'], v['easy']['n'])} (정답=반려) · "
                f"boundary 반려 {_pct(v['boundary']['rej'], v['boundary']['n'])} "
                f"(정답=통과 — 반려는 오탐)"
            )

    if "m27b" in m:
        tot = sum(v["n"] for v in m["m27b"].values())
        print(f"\n── M27b `verifier_recall` (strict) — **easy {tot}건에서만**")
        print(
            "   심은 유형을 `severity >= 임계값` 으로 지목한 건. boundary 는 통과가 정답이라 뺀다."
        )
        print("   ⚠️ 유형별 n=2 다. 신뢰구간을 붙이지 않는다 — 점추정으로만 읽을 것.")
        for d, v in m["m27b"].items():
            print(f"   {d}  M27b {_pct(v['hit'], v['n'])}")

    if "m27gap" in m:
        g = m["m27gap"]
        print("\n── M27gap `wrong_reason_rate` — 반려했으나 M27b 불만족")
        print(f"   {_pct(g['k'], g['n'])}  (분모 = easy 반려 {g['n']}건)")
        for row in g["cases"]:
            print(f"     {row['case_id']}: 심은 {row['seeded']} vs 지목 {row['named']}")

    if "m28a" in m and m["m28a"]["n"]:
        print(f"\n── M28a `verifier_type_id` — 분모 = easy 반려 {m['m28a']['n']}건")
        print(f"   {_pct(m['m28a']['k'], m['m28a']['n'])}")
        b = m["m28b"]
        print("\n── M28b `verifier_localization` — **누출 제외 후 지표**")
        print(f"   {_pct(b['k'], b['n'])}  (분모 = easy 반려 중 위치 누출을 뺀 것)")
        print(f"   [참고] 제외 없이 세면 {_pct(b['full_k'], b['full_n'])}")
        print(
            "   제외 기준: 심은 카드가 계획에서 유일한 비최빈 분량이라 `argmin(분량)` 하나로\n"
            "              정답 키가 나온다 "
            "(`check_seeded_defect_shortcuts.m28_leaked_case_ids`)"
        )
        print(f"   이번 분모에서 실제로 빠진 건: {', '.join(b['excluded']) or '없음'}")
        print(
            f"   ⚠️ 골든셋 누출 목록은 {len(b['leak_list'])}건이지만 그중 easy 만 이 분모에\n"
            f"      들어올 수 있다 — boundary 누출은 애초에 분모 밖이다."
        )
        print(
            "   ⚠️ v4 는 `estimated_minutes` 를 넘기지 않으므로 이 누출 경로 자체가 닫혀 있다.\n"
            "      그래도 주 지표는 보수적으로 **제외 후**로 둔다."
        )

    if "m32" in m:
        v = m["m32"]
        print(f"\n── M32 `verifier_self_consistency` ({v['repeats']}회, 케이스당 판정 쌍)")
        print(
            f"   같은 판정 {_pct(v['agree'], v['pairs'])}   "
            f"판정이 갈린 케이스 {_pct(len(v['flips']), v['n_cases'])}"
        )
        print(f"   분포: {v['dist']}")
        print("   ⚠️ 승인 편향이 pairwise 일치율을 밀어 올린다 — 분포를 함께 읽을 것.")
        if v["flips"]:
            print(f"   갈린 케이스: {', '.join(v['flips'][:8])}")

    print("\n── 운영점 (같은 원자료, 임계값만 바꿔 재집계)")
    print("   severity  M29 오반려          M27b recall(easy)")
    for t, v in m["sweep"].items():
        mark = "  ←기본" if t == REJECT_SEVERITY_THRESHOLD else ""
        print(
            f"   >= {t}      {_pct(v['m29_k'], v['m29_n']):<20} "
            f"{_pct(v['m27b_k'], v['m27b_n'])}{mark}"
        )

    if "latency" in m:
        lt = m["latency"]
        print(
            f"\n── 시스템 : 지연 중앙 {lt['median']:.0f}ms · p95 {lt['p95']:.0f}ms · "
            f"토큰 in {lt['tokens_in']} / out {lt['tokens_out']}"
        )
    print(
        "\n⚠️ 이 수치는 **이 루브릭(§2 D1~D5) 준수도**이지 일반 결함 발견 능력이 아니다.\n"
        "⚠️ 유형별 n=2 라 **계산은 되지만 추정은 안 된다** — 점추정으로만 읽을 것.\n"
        "⚠️ v4 는 **평가 후보**다. 프로덕션 ④층은 여전히 v3 를 부른다.\n"
        f"{'=' * 76}"
    )


async def main_async(args: argparse.Namespace) -> None:
    if args.summarize_only:
        rows = [
            json.loads(line)
            for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        print(f"저장된 원자료 재집계: {RESULTS_PATH.relative_to(_ROOT)} ({len(rows)}행)")
        summarize(rows, threshold=args.severity_threshold)
        return

    cases = load_cases(limit=args.limit, blocks=args.blocks)
    print(
        f"케이스 {len(cases)}건 × 반복 {args.repeats}회 = 호출 {len(cases) * args.repeats}건"
        f"{' (dry-run)' if args.dry_run else ''}   프롬프트 {PROMPT_ID}"
    )
    rows: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for case in cases:
            row = await run_case(case, repeat, dry_run=args.dry_run)
            rows.append(row)
            print("!" if row.get("fell_back") else ".", end="", flush=True)
    print()
    if not args.dry_run:
        RESULTS_PATH.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        print(f"원자료: {RESULTS_PATH.relative_to(_ROOT)} ({len(rows)}행)")
    summarize(rows, threshold=args.severity_threshold)


def main() -> None:
    parser = argparse.ArgumentParser(description="L1-7B v4 — 평가 후보 검토기 (실 LLM 호출)")
    parser.add_argument("--limit", type=int, default=None, help="앞 N건만 (스모크)")
    parser.add_argument("--repeats", type=int, default=1, help="케이스당 반복 횟수")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 구성만 확인")
    parser.add_argument("--summarize-only", action="store_true", help="저장된 원자료만 다시 집계")
    parser.add_argument(
        "--severity-threshold",
        type=int,
        default=REJECT_SEVERITY_THRESHOLD,
        help="반려 임계값 (기본 2). 저장된 원자료에 다시 적용해 운영점을 고를 수 있다",
    )
    parser.add_argument(
        "--blocks", nargs="*", default=None, help="블록 필터 (defect_free_control / seeded_defect)"
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
