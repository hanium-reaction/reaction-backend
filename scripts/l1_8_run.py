"""L1-8 하네스 — 실행 이력이 계획을 바꾸는가 (실 LLM 호출).

`docs/experiments/experiment-plan-v1.md` §2 L1-8 을 실행한다. 가설:

> **H1-8**: 이력을 주면 ① 계획 분량이 그 이력이 가리키는 방향으로 움직이고
> ② 이력 문구는 사용자에게 **드러나지 않는다**.

입력은 `eval/golden_history_cases.jsonl` 42건(목표 6 × 블록 7). 지표 정의는 실험계획서
§5 의 M36~M40 이 단일 진실 소스이고 여기서 재정의하지 않는다.

## 이 하네스가 지키는 두 가지

1. **프롬프트 변수를 손으로 조립하지 않는다.** `first_plan_adapter.context_from_outcome`
   과 `first_plan._replan_feedback`/`_format_milestones` 를 그대로 부른다. L1-7 하네스가
   1차 실행에서 `review_feedback` 하나를 빼먹어 34호출 전부가 **프로덕션이 내지 않는
   프롬프트**였던 전례가 있다.
2. **이력도 프로덕션 경로로 넣는다.** 케이스의 `failure_contexts`/`recovery_contexts` 를
   문자열로 미리 만들어 두지 않고, 프로덕션이 쓰는 `context_from_outcome(...)` 인자로
   넘겨 `_failure_summary`/`_recovery_summary` 가 포맷하게 한다. 감쇠도 마찬가지로
   `dampened_density` 를 부른다 — 하네스가 자기 포맷을 쓰면 재는 대상이 프로덕션이 아니다.

## 채점은 LLM 없이 결정적이다

M36~M40 은 전부 계획 JSON 과 케이스만 있으면 계산된다(사람 라벨·심판 LLM 없음).
그래서 `score_case`/`summarize` 는 순수 함수이고 `tests/test_l1_8_scoring.py` 가 합성
계획으로 직접 검증한다 — 실행 없이도 채점 코드의 회귀를 잡는다.

## 실행

    uv run python -m scripts.l1_8_run --dry-run        # LLM 없이 프롬프트 구성만 확인
    uv run python -m scripts.l1_8_run --limit 4        # 스모크
    uv run python -m scripts.l1_8_run --repeats 3      # 본 실행 (42 × 3 = 126 호출)
    uv run python -m scripts.l1_8_run --summarize-only # 저장된 원자료만 다시 채점

⚠️ `python scripts/l1_8_run.py` 가 아니라 **`-m`** 이다 — 골든셋 생성기와 `BLAME_MARKERS`
를 공유하므로 레포 루트가 `sys.path` 에 있어야 한다(생성기들과 같은 호출 관례).

원자료는 `eval/l1_8_results.jsonl` (비결정적 실 LLM 결과라 커밋하지 않는다).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from reaction_backend.orchestrator import first_plan, first_plan_adapter
from reaction_backend.schemas.common import KST
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import GoalDecomposition, GoalNodeDraft
from scripts.build_golden_history_cases import BLAME_MARKERS

_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = _ROOT / "eval" / "golden_history_cases.jsonl"
RESULTS_DIR = _ROOT / "eval"
RESULTS_GLOB = "l1_8_results*.jsonl"


def results_path(stamp: str | None = None) -> Path:
    """실행마다 **새 파일**에 쓴다.

    2차 실행이 1차 원자료를 통째로 덮어써서 1차를 재감사할 수 없게 됐다 — 그런데 하필
    두 실행의 결론이 갈렸다(재현 실패). 원문을 남기는 이유가 재감사인데 덮어쓰면 그 이유가
    사라진다. `--summarize-only` 는 가장 최근 파일을 고른다.
    """
    return (
        RESULTS_DIR / f"l1_8_results_{stamp}.jsonl" if stamp else RESULTS_DIR / "l1_8_results.jsonl"
    )


def latest_results_path() -> Path | None:
    files = sorted(RESULTS_DIR.glob(RESULTS_GLOB))
    return files[-1] if files else None


CONTROL_BLOCK = "no_history"

_FALLBACK_NODE = GoalNodeDraft(
    node_id="fallback-root",
    parent_id=None,
    title="(폴백)",
    node_type="root",
    order_index=0,
    is_leaf=False,
)


def load_cases(limit: int | None = None, blocks: list[str] | None = None) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if blocks:
        rows = [c for c in rows if c["block"] in blocks]
    return rows[:limit] if limit else rows


def build_outcome(case: dict[str, Any], *, today: date) -> InterviewOutcome:
    """케이스의 목표 슬롯 → `InterviewOutcome`.

    ⚠️ **빈도(`frequency_per_week`)를 채우지 않는다.** 골든셋이 일부러 안 담고 있고, 여기서
    넣으면 `target_sessions_per_week` 우선순위 1 이 걸려 분량 감쇠가 무효가 된다
    (`scripts/build_golden_history_cases.py` 의 「왜 목표가 빈도를 말하지 않는가」).

    마감은 상대 오프셋이라 실행일 기준으로 되짚는다 — 절대 날짜였다면 하루만 지나도
    '마감 임박' 이 '마감 지남' 이 된다.
    """
    goal = case["goal"]
    deadline = (today + timedelta(days=goal["deadline_offset_days"])).isoformat()
    return InterviewOutcome(
        session_id=f"l1-8-{case['case_id']}",
        generated_at=datetime(today.year, today.month, today.day, 9, 0, tzinfo=KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대학생", season="학기중"),
        core_goals=[
            GoalCandidate(
                title=goal["title"],
                category=goal["category"],
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
                deadline=deadline,
                success_image=goal["success_image"],
                current_level=goal["current_level"],
                session_length_min=goal["session_length_minutes"],
                weekly_hours=goal["weekly_hours"],
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["저녁"]
        ),
        preferences=PreferenceProfile(
            recovery_tone="담백", rest_ok=True, downscope_unit_min=15, focus_duration_min=None
        ),
        horizon=deadline,
    )


def prompt_vars_for(case: dict[str, Any], *, today: date) -> dict[str, Any]:
    """케이스 → 프로덕션 프롬프트 변수. **이력도 프로덕션 포맷터를 통과한다.**

    `failure_contexts`/`recovery_contexts` 는 repo 가 돌려주는 dataclass 와 같은 모양이라
    가벼운 shim 으로 감싸 그대로 넘긴다 — 문자열을 여기서 만들면 `_failure_summary` 의
    표본 게이트(2회 미만이면 '(없음)')와 범위 접두어를 하네스가 우회하게 된다.
    """
    from reaction_backend.repositories.recovery_repo import RecoveryOutcomeContext
    from reaction_backend.repositories.review_repo import TopFailureContext

    history = case["history"]
    density = first_plan_adapter.dampened_density(
        "standard", consecutive_goal_failures=history["consecutive_goal_failures"]
    )
    ctx = first_plan_adapter.context_from_outcome(
        build_outcome(case, today=today),
        density=density,
        target_date=today,
        failure_contexts=[TopFailureContext(**row) for row in history["failure_contexts"]],
        failure_scope=history["failure_scope"],
        recovery_contexts=[RecoveryOutcomeContext(**row) for row in history["recovery_contexts"]],
    )
    return {"density": density, "prompt_vars": ctx["prompt_vars"]}


# ─────────────────────────────────────────────────────────────────────────────
# 채점 — 순수 함수 (LLM 없이 테스트된다)
# ─────────────────────────────────────────────────────────────────────────────


def plan_text(plan: dict[str, Any]) -> str:
    """사용자가 보게 되는 텍스트 전부 — 누출·비난 판정의 대상."""
    parts: list[str] = []
    for node in plan.get("goal_nodes", []):
        parts.append(str(node.get("title", "")))
    for item in plan.get("action_items", []):
        parts.append(str(item.get("title", "")))
        parts.append(str(item.get("first_step") or ""))
    return "\n".join(parts)


def score_case(case: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """한 케이스의 결정적 채점 — 평균 세션 분 + 누출/비난 판정.

    `mean_minutes` 는 M36 의 **재료**다(차이는 짝 단위로 뺀다 — `summarize`).
    """
    text = plan_text(plan)
    minutes = [
        int(a["estimated_minutes"])
        for a in plan.get("action_items", [])
        if a.get("estimated_minutes")
    ]
    return {
        # M36 의 재료 — **총 분량**이다. 평균 세션 분(M41)이 아니다: 세션 길이는 사용자가
        # 말한 값이라 프롬프트가 고정하고, 모델은 길이를 지키며 **개수**를 바꾼다
        # (스모크 실측: 평균은 44.8~49.0 으로 붙어 있는데 총량은 450~980 으로 벌어졌다).
        "total_minutes": sum(minutes) if minutes else None,
        "mean_minutes": statistics.fmean(minutes) if minutes else None,
        "sessions": len(plan.get("action_items", [])),
        "leaked": sorted({p for p in case["assertions"]["must_not_contain"] if p and p in text}),
        "blamed": sorted({m for m in BLAME_MARKERS if m in text}),
    }


def rescore(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """저장된 **계획 원문**으로 채점을 다시 한다 — `--summarize-only` 의 핵심.

    저장된 `score` 를 그대로 재사용하면, 채점 코드를 고쳐도 옛 원자료에는 영원히 반영되지
    않는다. 그러면 "모델 출력을 그대로 남긴다"(L1-7 1차의 교훈)는 원칙이 절반만 지켜진다 —
    남기기는 하는데 다시 쓸 수가 없다. 실제로 M36 정의를 총 분량으로 바꾸자 옛 행의
    `score` 에 `total_minutes` 가 없어 집계가 통째로 비었다.

    계획이 없는 행(폴백)은 그대로 둔다 — 채점 대상이 아니다.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("plan") and row.get("case"):
            row = {**row, "score": score_case(row["case"], row["plan"])}
        out.append(row)
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """M36~M39 집계. ⚠️ **블록 평균끼리 빼지 않는다 — 짝(`pair_id`) 단위로 뺀다.**

    반복이 있으면 케이스별로 먼저 평균 낸다. 반복은 같은 케이스 안에서 상관이 있어 독립
    표본이 아니므로 **분모는 케이스 수**다(M29 가 겪은 함정).
    """
    by_case: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    # 예산 대비 채움율(M42)의 재료 — 요구 분량은 계획이 아니라 **행**에 있다(케이스마다,
    # 그리고 감쇠 블록에서는 블록마다 다르다).
    for row in rows:
        if row.get("score"):
            by_case[(row["block"], row["pair_id"])].append(
                {**row["score"], "_asked": float(row.get("total_minutes_asked") or 0) or None}
            )

    def _by_case(field: str) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for key, scores in by_case.items():
            vals = [s[field] for s in scores if s.get(field) is not None]
            if vals:
                out[key] = statistics.fmean(vals)
        return out

    def _deltas(values: dict[tuple[str, str], float]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = defaultdict(list)
        for (block, pair), value in values.items():
            if block == CONTROL_BLOCK:
                continue
            control = values.get((CONTROL_BLOCK, pair))
            if control is not None:
                out[block].append(value - control)
        return out

    total_minutes = _by_case("total_minutes")
    mean_minutes = _by_case("mean_minutes")

    # M42 — 요구 예산 대비 채움율. 목표마다 대조군이 남긴 **여유**가 다르고(3%~31%), 1차
    # 실행에서 처치 델타가 그 여유와 r = 0.92 로 붙었다. 원 분량(M36)만 보면 여유가 큰
    # 목표의 수치가 블록 평균을 끌고 간다. 감쇠 블록은 요구 예산 자체가 달라 **이 축이
    # 유일하게 비교 가능한 축**이기도 하다.
    fill: dict[tuple[str, str], float] = {}
    for key, scores in by_case.items():
        vals = [
            s["total_minutes"] / s["_asked"]
            for s in scores
            if s.get("total_minutes") is not None and s.get("_asked")
        ]
        if vals:
            fill[key] = statistics.fmean(vals)

    deltas = _deltas(total_minutes)
    length_deltas = _deltas(mean_minutes)
    fill_deltas = _deltas(fill)

    treated = [r for r in rows if r.get("score") and r["block"] != CONTROL_BLOCK]
    with_history = [r for r in treated if r["case"]["assertions"]["must_not_contain"]]

    # ⚠️ **동점은 승리가 아니다.** 처음엔 `>=` 였는데, 스모크에서 두 블록의 델타가 정확히
    # 같게 나오자(둘 다 +0.2분) M39 가 1.0 을 냈다 — "범위 접두어를 읽었다" 는 증거가 0 인데
    # 만점이 나온 것이다. 동점은 분자에서 빼고 **따로 센다**(조용히 사라지면 그것대로 오독).
    scope_wins = 0
    scope_ties = 0
    scope_pairs = 0
    for (block, pair), _ in total_minutes.items():
        if block != "failure_goal_scoped":
            continue
        control = total_minutes.get((CONTROL_BLOCK, pair))
        user = total_minutes.get(("failure_user_scoped", pair))
        goal = total_minutes.get(("failure_goal_scoped", pair))
        if control is None or user is None or goal is None:
            continue
        scope_pairs += 1
        if abs(goal - control) > abs(user - control):
            scope_wins += 1
        elif abs(goal - control) == abs(user - control):
            scope_ties += 1

    return {
        "M36_volume_delta_min": {b: round(statistics.fmean(v), 1) for b, v in deltas.items()},
        "M36_pairs": {b: len(v) for b, v in deltas.items()},
        "M42_budget_fill_delta": {b: round(statistics.fmean(v), 4) for b, v in fill_deltas.items()},
        "M41_session_length_delta_min": {
            b: round(statistics.fmean(v), 1) for b, v in length_deltas.items()
        },
        "M37_leak_rate": _rate([r for r in with_history if r["score"]["leaked"]], with_history),
        "M37_leaks": sorted({p for r in with_history for p in r["score"]["leaked"]}),
        "M38_blame_rate": _rate([r for r in treated if r["score"]["blamed"]], treated),
        "M39_scope_sensitivity": _rate_num(scope_wins, scope_pairs),
        "M39_ties": scope_ties,
    }


def _rate(hits: list[Any], total: list[Any]) -> float | None:
    return _rate_num(len(hits), len(total))


def _rate_num(hits: int, total: int) -> float | None:
    return None if total == 0 else round(hits / total, 4)


# ─────────────────────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────────────────────


async def run_case(
    case: dict[str, Any], repeat: int, *, today: date, dry_run: bool
) -> dict[str, Any]:
    from reaction_backend.config import get_settings
    from reaction_backend.llm import aiClient

    built = prompt_vars_for(case, today=today)
    prompt_vars = built["prompt_vars"]
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "block": case["block"],
        "pair_id": case["pair_id"],
        "repeat": repeat,
        "density": built["density"],
        "failure_summary": prompt_vars["failure_summary"],
        "recovery_summary": prompt_vars["recovery_summary"],
        "total_minutes_asked": prompt_vars["total_minutes"],
        "case": case,
    }
    if dry_run:
        return row

    settings = get_settings()
    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        prompt_id="planning/goal_decompose",
        fallback=lambda: GoalDecomposition(
            goal_nodes=[_FALLBACK_NODE], action_items=[], policy_violations=[]
        ),
        timeout=settings.llm_planning_timeout_seconds,
        thinking_budget=settings.llm_planning_thinking_budget,
        # 프로덕션(`decompose_goal`)이 넘기는 세 변수를 같은 함수로 만든다 — 손으로 쓰면
        # 렌더가 조용히 실패하거나 프로덕션이 안 내는 프롬프트를 재게 된다(L1-7 1차 전례).
        variables={
            **prompt_vars,
            "review_feedback": first_plan._replan_feedback({"review": None}),
            "milestones": first_plan._format_milestones([]),
            "out_of_cycle": "(없음)",
        },
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
        plan = {
            "goal_nodes": [n.model_dump(mode="json") for n in result.value.goal_nodes],
            "action_items": [a.model_dump(mode="json") for a in result.value.action_items],
        }
        # 모델 출력을 그대로 남긴다 — 집계만 저장하면 재감사가 불가능하다(L1-7 1차 전례).
        row["plan"] = plan
        row["score"] = score_case(case, plan)
    return row


def _print_summary(rows: list[dict[str, Any]]) -> None:
    result = summarize(rows)
    print(
        "\n[L1-8] M36 총 분량 델타(분) / M41 평균 세션 분 / M42 예산 채움율 — 전부 대조군 대비 짝 단위"
    )
    for block, delta in sorted(result["M36_volume_delta_min"].items()):
        pairs = result["M36_pairs"][block]
        length = result["M41_session_length_delta_min"].get(block, 0.0)
        fill = result["M42_budget_fill_delta"].get(block, 0.0)
        print(
            f"  {block:24s} {delta:+8.1f}  (짝 {pairs})   "
            f"세션길이 {length:+6.1f}   예산채움 {fill:+.1%}"
        )
    print(f"\n[L1-8] M37 누출률 {result['M37_leak_rate']}  (기대 0)")
    if result["M37_leaks"]:
        print(f"        누출 문구: {result['M37_leaks']}")
    print(f"[L1-8] M38 비난 출현율 {result['M38_blame_rate']}  (기대 0)")
    print(
        f"[L1-8] M39 범위 민감도 {result['M39_scope_sensitivity']}  (동점 {result['M39_ties']} — 승리 아님)"
    )
    fell = [r for r in rows if r.get("fell_back")]
    print(f"\n폴백 {len(fell)}/{len(rows)} — 폴백이 있으면 그 행은 채점에서 빠진다")
    print("[!] 전 케이스 synthetic. M36 은 단발로 읽지 말 것 (분모는 케이스 수)")


async def main_async(args: argparse.Namespace) -> None:
    today = date.today()
    cases = load_cases(limit=args.limit, blocks=args.blocks)
    rows: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for case in cases:
            row = await run_case(case, repeat, today=today, dry_run=args.dry_run)
            rows.append(row)
            mark = "dry" if args.dry_run else ("FB" if row.get("fell_back") else "ok")
            print(f"  [{mark}] {row['case_id']} r{repeat} density={row['density']}")

    if args.dry_run:
        print(f"\n{len(rows)}건 구성 확인 — LLM 호출 없음")
        return

    out = results_path(datetime.now(tz=KST).strftime("%Y%m%dT%H%M%S"))
    out.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
        newline="\n",
    )
    print(f"\n원자료 → {out}")
    _print_summary(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="L1-8 하네스 — 이력이 계획을 바꾸는가")
    parser.add_argument("--limit", type=int, default=None, help="앞 N건만 (스모크)")
    parser.add_argument("--repeats", type=int, default=1, help="케이스당 반복 횟수")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 구성만 확인")
    parser.add_argument("--blocks", nargs="*", default=None, help="블록 필터")
    parser.add_argument(
        "--summarize-only", action="store_true", help="저장된 원자료만 다시 채점 (LLM 0회)"
    )
    args = parser.parse_args()

    if args.summarize_only:
        path = latest_results_path()
        if path is None:
            print(f"원자료가 없다: {RESULTS_DIR}/{RESULTS_GLOB}", file=sys.stderr)
            raise SystemExit(1)
        print(f"원자료 ← {path}")
        rows = rescore(
            [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        )
        _print_summary(rows)
        return

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
