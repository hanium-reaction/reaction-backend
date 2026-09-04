"""M33 3-arm 하네스 — **④층이 순이득인가** (실 LLM 호출).

설계는 [`docs/experiments/m33-3arm-design.md`](../docs/experiments/m33-3arm-design.md) 가
**실행 전에** 고정했다. 이 파일은 그것을 그대로 집행한다.

## ⚠️ 프로덕션 노드를 **그대로 부른다** — 옮겨 적지 않는다

`first_plan.validate_inputs` · `decompose_goal` · `review_plan` · `should_replan` ·
`_replan_feedback` 을 **직접 호출**한다. `src/` 는 한 줄도 안 바꾼다.

DB·tone 은 `config = {}` 로 비운다 — `_session`/`_tone_mode` 가 `config.get("configurable", {})`
로 읽어서 `None` 이 되고, LLM 클라이언트는 세션이 없으면 `llm_runs` 기록만 건너뛴다.

**프롬프트를 복사하지 않는 것이 이 설계의 핵심이다.** `_review_variables` 를 옮겨 적었다가
34호출을 통째로 버린 전례가 있다(`l1-7-results.md` §5).

## 세 arm — 차이는 `review` 하나뿐

```
공통   validate_inputs → decompose_goal → review_plan     (한 번만)
       ↓ 검토가 승인이면 → A·B·C 모두 초기 계획 그대로 (셋이 동일)
       ↓ 검토가 반려이면
A      재분해 없음 — 초기 계획 유지
B      review 그대로 재분해        → _replan_feedback 이 feedback[] 전문을 싣는다
C      review.feedback 을 비우고 재분해 → _replan_feedback 이 "(첫 분해 …)" 를 낸다
```

**C 도 프로덕션 함수가 만든다.** `_replan_feedback` 은 `review.feedback` 이 비면 빈 신호를
돌려주므로, **같은 review 에서 리스트만 비우면** 빈 피드백 arm 이 된다 — 프롬프트를 따로
쓰지 않는다.

## 실행

    --stratum X --dry-run                    LLM 없이 구성 확인
    --stratum X --limit 2 --repeats 1        **스모크 — 이 형태만 쓴다**
    --stratum general   --repeats 3          본 실행 (사전등록: 케이스당 3회)
    --stratum challenge --repeats 3          〃
    --stratum X --summarize-only [--run 경로]  저장된 원자료 재집계

⚠️ **스모크는 `--limit 2 --repeats 1` 로만 한다.** 전체를 `--repeats 1` 로 먼저 돌려
반려율을 보고 3회 여부를 정하면, **이미 고정한 "케이스당 3회" 를 결과를 보고 바꾸는 것**이
된다(설계 §4.3). 스모크 결과는 **본 실험 표본에서 제외**한다 — 별도 run 파일로 남는다.

⚠️ 비용 때문에 회차를 줄이려면 **전체 실행 전에** 설계 변경으로 문서에 남긴다.

⚠️ **일반과 도전을 섞어 한 수치로 내지 않는다.** `--stratum` 이 필수인 이유다.

원자료 `eval/m33/{stratum}/{run_id}.jsonl` + `{run_id}.meta.json` (비결정적이라 `.gitignore`).

⚠️ **실행마다 새 파일**이다 — 덮어쓰면 문서가 인용한 수치를 다시 못 만든다. manifest 에
기준일 · git SHA · 골든 파일 SHA-256 · 실행 시각 · repeats · 사전등록 상수를 남긴다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Final

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from reaction_backend.orchestrator import first_plan as FP  # noqa: E402
from reaction_backend.schemas.planning import MilestoneDraft, PlanReview  # noqa: E402
from scripts.l1_7_run import build_outcome  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STRATUM_PATHS: Final = {
    "general": _ROOT / "eval" / "golden_first_plan_cases.jsonl",
    "challenge": _ROOT / "eval" / "golden_challenge_stratum.jsonl",
}
ARMS: Final = ("A_none", "B_feedback", "C_retry")
PRIMARY_REPEAT: Final = 0
"""주지표의 1차 추정 회차. **사전 지정**이다 — 결과를 보고 고르면 안 된다."""

BOOTSTRAP_N: Final = 10_000
BOOTSTRAP_SEED: Final = 42
"""설계 §4.2 가 **실행 전에** 고정한 값. 여기서 바꾸면 사전등록을 어긴다."""


RUNS_DIR = _ROOT / "eval" / "m33"


def run_dir(stratum: str) -> Path:
    return RUNS_DIR / stratum


RUN_NAME_RE = re.compile(r"^\d{8}T\d{6}(-\d+)?$")
"""실행 파일명은 타임스탬프뿐이다. 이 필터가 없으면 누가 `smoke.jsonl` 을 놓았을 때
문자열 정렬에서 그게 이겨 **기본 재집계 대상**이 된다."""


def latest_run(stratum: str) -> Path | None:
    """가장 최근 실행의 원자료. 없으면 `None`."""
    d = run_dir(stratum)
    files = sorted(f for f in d.glob("*.jsonl") if RUN_NAME_RE.match(f.stem)) if d.exists() else []
    return files[-1] if files else None


def _rel(path: Path) -> str:
    """표시용 상대 경로. 레포 밖이거나 상대 경로면 그대로 보여준다(`relative_to` 는 던진다)."""
    try:
        return str(path.resolve().relative_to(_ROOT))
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(
    out: Path, *, stratum: str, today: date, repeats: int, limit: int | None, n_rows: int
) -> None:
    """실행 provenance — **어느 실행의 수치인지 특정할 수 있어야 한다.**

    원자료는 비결정적이라 커밋하지 않는다(`.gitignore`). 그래서 문서가 인용하는 **그 실행**을
    지목할 수단이 manifest 뿐이다.
    """
    import subprocess
    from datetime import datetime

    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=_ROOT, timeout=10
        ).stdout.strip()
        # ⚠️ HEAD 만으로는 부족하다 — 커밋하지 않은 변경으로 돌리면 manifest 의 SHA 가
        # **실행된 코드가 아니다.** 실제로 그런 원자료가 한 번 생겼다.
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=_ROOT,
                timeout=10,
            ).stdout.strip()
        )
    except Exception:  # pragma: no cover - git 이 없어도 실행은 되어야 한다
        sha, dirty = "(unknown)", True
    meta = {
        "stratum": stratum,
        "target_date": today.isoformat(),
        "repeats": repeats,
        "limit": limit,
        "rows": n_rows,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_sha": sha,
        "git_dirty": dirty,
        "golden_path": str(STRATUM_PATHS[stratum].relative_to(_ROOT)),
        "golden_sha256": _sha256(STRATUM_PATHS[stratum]),
        "raw_sha256": _sha256(out),
        "raw_bytes": out.stat().st_size,
        # 사전등록 상수 — 실행 시점 값을 함께 남긴다(나중에 바뀌면 대조된다).
        "primary_repeat": PRIMARY_REPEAT,
        "bootstrap_n": BOOTSTRAP_N,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "arms": list(ARMS),
    }
    out.with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def check_manifest(src: Path, *, stratum: str, allow_smoke: bool) -> None:
    """재집계 전 provenance 대조. **기준일 고정만으로는 절반밖에 못 막는다.**

    `summarize` 는 골든셋을 **재집계 시점에 다시 읽어** 마감·주기창·채점 입력을 만든다.
    그래서 원자료를 한 글자도 안 바꿔도 **골든이 바뀌면 M33 이 바뀐다** — 실측으로
    `M33 = -0.0625` 가 `+0.0000` 이 됐다. 기준일 버그와 같은 종류이고, manifest 에
    `golden_sha256` 을 적어놓고 **대조하지 않으면 적은 의미가 없다.**

    스모크(`--limit`)는 설계 §4.3 이 **본 실험 표본에서 제외**한다고 고정했다. 문서로만
    적어두면 `--summarize-only` 의 기본값이 스모크 파일을 골라 그대로 집계된다.
    """
    meta_path = src.with_suffix(".meta.json")
    if not meta_path.exists():
        raise SystemExit(
            f"manifest 가 없다: {_rel(meta_path)} — 어느 골든·어느 커밋으로 만든 원자료인지 "
            "특정할 수 없다. 재집계를 거부한다(다시 실행해서 새로 만들어라)."
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("stratum") != stratum:
        raise SystemExit(
            f"층이 다르다: 원자료는 [{meta.get('stratum')}] 인데 --stratum {stratum} 로 불렀다"
        )
    now = _sha256(STRATUM_PATHS[stratum])
    if meta.get("golden_sha256") != now:
        raise SystemExit(
            "\n".join(
                [
                    "골든셋이 실행 당시와 다르다 — 재집계는 **지금** 골든으로 채점하므로 "
                    "같은 원자료의 M33 이 달라진다.",
                    f"  실행 당시 {meta.get('golden_sha256', '(없음)')[:16]}",
                    f"  현재      {now[:16]}",
                    f"  → 그 커밋({meta.get('git_sha', '?')[:7]})의 골든으로 되돌리거나 "
                    "다시 실행해라.",
                ]
            )
        )
    if meta.get("limit") is not None and not allow_smoke:
        raise SystemExit(
            f"이 원자료는 스모크다(limit={meta['limit']}, repeats={meta.get('repeats')}) — "
            "설계 §4.3 이 **본 실험 표본에서 제외**한다고 고정했다. 본 실험 원자료를 "
            "`--run` 으로 지정해라(정말 스모크를 보려면 `--allow-smoke`)."
        )
    if meta.get("git_dirty"):
        print(f"⚠️ 이 실행은 커밋하지 않은 변경 위에서 돌았다 (git_sha={meta.get('git_sha')})")


def load_stratum(stratum: str, limit: int | None = None) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in STRATUM_PATHS[stratum].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = [c for c in rows if c["kind"] == "decompose"]
    return cases[:limit] if limit else cases


def _milestones(case: dict[str, Any]) -> list[MilestoneDraft] | None:
    raw = case["interview"].get("milestones") or []
    return [MilestoneDraft(title=m["title"], summary=m["summary"]) for m in raw] or None


async def run_case(case: dict[str, Any], repeat: int, *, today: date) -> dict[str, Any]:
    """한 케이스의 세 arm. **초기 계획과 검토 판정을 공유한다.**"""
    outcome = build_outcome(case, today=today)
    cfg: Any = {}  # DB·tone 없음 — `_session`/`_tone_mode` 가 None 을 돌려준다
    state = FP.initial_state(
        user_id=uuid.uuid4(),
        outcome=outcome,
        target_date=today.isoformat(),
        milestones=_milestones(case),
        milestone_cursor=case["interview"].get("milestone_cursor", 0),
    )
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "block": case["block"],
        "repeat": repeat,
        "axes": case.get("axes"),
        # ⚠️ **재집계는 이 값을 쓴다.** `date.today()` 로 다시 채점하면 D 에 만든 계획을
        # D+1 의 마감·주기창으로 재구성하게 돼 **같은 원자료의 M33 이 날짜마다 달라진다.**
        "target_date": today.isoformat(),
    }

    # ── 공통: 초기 분해 + 검토 (한 번만) ──────────────────────────────────
    state = await FP.validate_inputs(state, cfg)
    state = await FP.decompose_goal(state, cfg)
    if state["goal_plan"] is None or state["used_fallback"]:
        row["fell_back"] = True
        row["stage"] = "decompose"
        return row
    initial = state["goal_plan"]
    state = await FP.review_plan(state, cfg)
    review = state["review"]
    # ⚠️ `review` 는 **절대 None 이 아니다.** 검토 LLM 이 실패하면 `_rule_review` 가
    # `PlanReview(approved=True, feedback=[])` 를 돌려주기 때문이다(무한 cycle 방지용
    # 프로덕션 규칙). `review is None` 만 보면 **LLM 타임아웃이 "승인" 으로 집계돼**
    # 반려 집합(= Δ 가 0 이 아닌 유일한 집합)이 조용히 줄고 M33 이 0 쪽으로 편향된다.
    # `used_fallback` 을 봐야 한다 — 분해 폴백은 위에서 이미 걸러졌으므로 여기서 참이면
    # 검토가 폴백한 것이다.
    if review is None or state["used_fallback"]:
        row["fell_back"] = True
        row["stage"] = "review"
        row["review_fell_back"] = True
        return row

    rejected = FP.should_replan(state) == "replan"
    row.update(
        fell_back=False,
        review_fell_back=False,
        approved=review.approved,
        rejected=rejected,
        feedback=list(review.feedback),
        plans={"A_none": _dump(initial)},
    )

    # ── 승인이면 세 arm 이 같다 (설계 §1.1) ───────────────────────────────
    if not rejected:
        row["plans"]["B_feedback"] = row["plans"]["C_retry"] = _dump(initial)
        return row

    # ── B: review 그대로 재분해 ──────────────────────────────────────────
    b = await FP.decompose_goal(dict(state), cfg)  # type: ignore[arg-type]
    row["plans"]["B_feedback"] = _dump(b["goal_plan"]) if not b["used_fallback"] else None
    row["b_feedback_sent"] = FP._replan_feedback(state)

    # ── C: 같은 review 에서 feedback 만 비운다 ────────────────────────────
    # `_replan_feedback` 이 빈 리스트를 보면 "(첫 분해 …)" 를 낸다 — **프로덕션 함수가
    # 빈 피드백 arm 을 만든다.** 프롬프트를 따로 쓰지 않는다.
    c_state = dict(state)
    c_state["review"] = PlanReview(approved=review.approved, feedback=[])
    row["c_feedback_sent"] = FP._replan_feedback(c_state)  # type: ignore[arg-type]
    c = await FP.decompose_goal(c_state, cfg)  # type: ignore[arg-type]
    row["plans"]["C_retry"] = _dump(c["goal_plan"]) if not c["used_fallback"] else None
    return row


def _dump(plan: Any) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "goal_nodes": [n.model_dump() for n in plan.goal_nodes],
        "action_items": [a.model_dump() for a in plan.action_items],
    }


async def main_async(args: argparse.Namespace) -> None:
    if args.summarize_only:
        src = Path(args.run) if args.run else latest_run(args.stratum)
        if src is None:
            raise SystemExit(f"[{args.stratum}] 실행 원자료가 없다 — 먼저 실행해라")
        check_manifest(src, stratum=args.stratum, allow_smoke=args.allow_smoke)
        rows = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines() if x.strip()]
        print(f"저장된 원자료 재집계: {_rel(src)} ({len(rows)}행)")
        summarize(rows, stratum=args.stratum)
        return

    from reaction_backend.config import get_settings

    today = date.today()
    cases = load_stratum(args.stratum, args.limit)
    n = len(cases) * args.repeats
    # 승인이면 분해1 + 검토1 = 2, 반려면 재분해 2회가 더 붙어 4.
    print(
        f"[{args.stratum}] 케이스 {len(cases)}건 × 반복 {args.repeats}회 "
        f"→ 노드 호출 {2 * n}~{4 * n} (승인 2 / 반려 4)"
        f" · 재시도 포함 실제 API 최대 {4 * n * get_settings().llm_max_retries}"
        f"{' (dry-run)' if args.dry_run else ''}"
    )
    if args.dry_run:
        for c in cases:
            print(f"  {c['case_id']:<16} 마일스톤 {len(c['interview'].get('milestones') or [])}개")
        return

    rows: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for case in cases:
            row = await run_case(case, repeat, today=today)
            rows.append(row)
            print(
                "!" if row.get("fell_back") else ("R" if row.get("rejected") else "."),
                end="",
                flush=True,
            )
    print()
    # ⚠️ **실행마다 새 파일이다.** 한 경로에 덮어쓰면 이전 실행이 사라지고, 문서가 인용한
    # 수치를 다시 만들 수 없다.
    from datetime import datetime as _dt

    d = run_dir(args.stratum)
    d.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%dT%H%M%S")
    out_path = d / f"{stamp}.jsonl"
    seq = 1
    while out_path.exists():  # 같은 초에 두 번 실행해도 덮어쓰지 않는다
        out_path = d / f"{stamp}-{seq}.jsonl"
        seq += 1
    out_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    write_manifest(
        out_path,
        stratum=args.stratum,
        today=today,
        repeats=args.repeats,
        limit=args.limit,
        n_rows=len(rows),
    )
    print(f"원자료: {_rel(out_path)} ({len(rows)}행)")
    print(f"manifest: {_rel(out_path.with_suffix('.meta.json'))}")
    summarize(rows, stratum=args.stratum)


# ─────────────────────────────────────────────────────────────────────────────
# M26-core 집계 — 설계 §4
#
# ⚠️ **기준이 L1-7A 와 다르다.** 여기서 채점하는 것은 `decompose_goal` 이 ③층까지 돌린
# **최종 계획**이다(노드가 원안을 state 에 남기지 않는다). L1-7A 의 M26-core 0.794 는
# **③층 보정 전 원안** 기준이므로 **절대값을 비교하면 안 된다** — 이 실험 안의 ΔM26-core
# 만 의미가 있다.
#
# ⚠️ 지표 계산은 `l1_7_run.score_raw` 와 `l1_7_run.core_verdicts` 를 **그대로 쓴다.**
# 옮겨 적으면 두 실험이 다른 것을 재게 된다.
# ─────────────────────────────────────────────────────────────────────────────


def _arm_verdicts(
    case: dict[str, Any] | None, plan_dump: dict[str, Any] | None, *, today: date
) -> Any:
    """한 arm 의 계획 → `core_verdicts` 판정. 계획이 없으면 `None`."""
    if plan_dump is None or case is None:
        return None
    from reaction_backend.schemas.planning import GoalDecomposition
    from scripts import l1_7_run as R
    from scripts import l1_7_schedule_eval as SE

    outcome = build_outcome(case, today=today)
    plan = GoalDecomposition.model_validate(plan_dump)
    window = R.cycle_window(case, outcome, today)
    row = R.score_raw(
        outcome,
        plan,
        window,
        today,
        case_milestones=len(case["interview"].get("milestones") or []),
    )
    sched = SE.evaluate_case(case, today=today, action_items=plan_dump["action_items"])
    return R.core_verdicts(row, sched)


def paired_deltas(
    rows: list[dict[str, Any]],
    cases: dict[str, dict[str, Any]],
    a: str,
    b: str,
    *,
    today: date,
) -> tuple[list[int], list[str]]:
    """케이스별 `(b 통과 − a 통과)`. **세 arm 모두 정의된 케이스만** 남긴다 (설계 §4.1).

    한 arm 에서만 N/A 가 되면 ΔM26-core 가 **서로 다른 케이스 집합의 차**가 된다.
    """
    from scripts import l1_7_run as R
    from scripts.l1_7_schedule_eval import _NotApplicable

    seen = [r["case_id"] for r in rows]
    if len(seen) != len(set(seen)):
        # docstring 이 "케이스가 표집 단위" 라고 말해도, 같은 케이스의 행이 둘 이상이면
        # 부트스트랩이 **행**을 리샘플하게 돼 상관된 표본으로 구간이 거짓으로 좁아진다.
        dup = sorted({c for c in seen if seen.count(c) > 1})
        raise SystemExit(
            f"케이스가 중복됐다: {dup[:6]} — 페어드 부트스트랩의 표집 단위는 **케이스**다. "
            f"repeat {PRIMARY_REPEAT} 한 벌만 넘겨라(여러 실행을 이어붙이지 않는다)."
        )
    deltas: list[int] = []
    dropped: list[str] = []
    for r in rows:
        vals: dict[str, bool] = {}
        for arm in ARMS:
            v = _arm_verdicts(cases.get(r["case_id"]), r["plans"].get(arm), today=today)
            if v is None:
                break
            verdict, _ = R.m26_core(v)
            if isinstance(verdict, _NotApplicable):
                break
            vals[arm] = bool(verdict)
        if len(vals) != len(ARMS):
            dropped.append(r["case_id"])
            continue
        deltas.append(int(vals[b]) - int(vals[a]))
    return deltas, dropped


def _arm_m18b(
    case: dict[str, Any] | None, plan_dump: dict[str, Any] | None, *, today: date
) -> float | None:
    """arm 별 M18b — **M26-core 의 AND 에는 없고 나란히 보고한다**(설계 §4)."""
    if case is None or plan_dump is None:
        return None
    from reaction_backend.schemas.planning import GoalDecomposition
    from scripts import l1_7_run as R

    outcome = build_outcome(case, today=today)
    plan = GoalDecomposition.model_validate(plan_dump)
    return R.score_raw(outcome, plan, [], today).get("m18b_ratio")


def paired_bootstrap_ci(deltas: list[int]) -> tuple[float, float, float]:
    """케이스 단위 **페어드** 부트스트랩 — 설계 §4.2 가 실행 전에 고정한 방법.

    `(점추정, 하한, 상한)`. 케이스를 복원 추출한다 — **행이 아니라 케이스**가 표집 단위다.
    행 단위로 재면 같은 케이스의 arm·반복이 상관돼 구간이 **거짓으로 좁아진다.**
    """
    import random

    if not deltas:
        return 0.0, 0.0, 0.0
    point = sum(deltas) / len(deltas)
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(deltas)
    samples = sorted(sum(rng.choices(deltas, k=n)) / n for _ in range(BOOTSTRAP_N))
    lo = samples[int(0.025 * BOOTSTRAP_N)]
    hi = samples[min(BOOTSTRAP_N - 1, int(0.975 * BOOTSTRAP_N))]
    return point, lo, hi


def base_date_of(rows: list[dict[str, Any]]) -> date:
    """원자료의 **기준일**. 섞여 있으면 거부한다.

    ⚠️ `date.today()` 로 재집계하면 실행일 D 에 만든 계획을 D+1 의 마감·주기창으로
    재구성하게 돼 **같은 원자료의 M33 이 날짜마다 달라진다.** 저장된 값만 쓴다.
    """
    missing = [i for i, r in enumerate(rows) if not r.get("target_date")]
    dates = {r["target_date"] for r in rows if r.get("target_date")}
    if missing and dates:
        # 옛/새 원자료를 이어붙인 경우. 조용히 건너뛰면 **옛 행이 새 기준일로 채점된다.**
        raise SystemExit(
            f"{len(missing)}/{len(rows)}행에 `target_date` 가 없다 — 기준일을 저장하기 전 "
            "형식의 행이 섞였다. 한 실행의 원자료만 집계해라."
        )
    if not dates:
        raise SystemExit(
            "원자료에 `target_date` 가 없다 — 이 파일은 기준일을 저장하기 전 형식이다. "
            "다시 실행해서 새로 만들어라(옛 원자료를 오늘 날짜로 재집계하면 안 된다)."
        )
    if len(dates) > 1:
        raise SystemExit(f"기준일이 섞여 있다: {sorted(dates)} — 한 실행의 행만 집계한다")
    return date.fromisoformat(next(iter(dates)))


def summarize(rows: list[dict[str, Any]], *, stratum: str) -> None:
    """arm 별 M26-core · M18 과 M33(ΔM26-core) 을 낸다.

    ⚠️ **채점 기준이 L1-7A 와 다르다.** 여기서 채점하는 것은 `decompose_goal` 이 ③층까지
    돌린 **최종 계획**이다(노드가 원안을 state 에 남기지 않는다). L1-7A 의 M26-core 0.794 는
    **원안 기준**이므로 **절대값을 비교하면 안 된다** — 이 실험 안의 Δ 만 의미가 있다.
    """
    import statistics as _st

    from scripts import l1_7_run as R
    from scripts.l1_7_schedule_eval import _NotApplicable

    cases = {c["case_id"]: c for c in load_stratum(stratum)}
    today = base_date_of(rows)
    ok = [r for r in rows if not r.get("fell_back")]
    fb = [r for r in rows if r.get("fell_back")]
    primary = [r for r in ok if r["repeat"] == PRIMARY_REPEAT]
    rej = [r for r in primary if r.get("rejected")]

    print(f"\n{'=' * 74}\nM33 3-arm [{stratum}]")
    print(f"실행 {len(rows)}행 / 집계 {len(ok)} / 폴백 {len(fb)}")
    if fb:
        print(f"  폴백 단계: {[r.get('stage') for r in fb][:6]}")
    print(f"\nrepeat {PRIMARY_REPEAT} 고유 {len(primary)}건 중 **반려 {len(rej)}건**")
    print("   ⚠️ 승인 케이스는 세 arm 이 같으므로 **ΔM26-core 에 0 을 기여**한다(설계 §1.1).")
    if rej:
        print(f"   반려: {', '.join(r['case_id'] for r in rej[:8])}")
        bad = [r for r in rej if not r["plans"].get("B_feedback") or not r["plans"].get("C_retry")]
        if bad:
            print(f"   ⚠️ 재분해가 폴백한 건 {len(bad)}건 — 페어링에서 빠진다")
    if not primary:
        print("=" * 74)
        return

    # ── arm 별 M26-core ──────────────────────────────────────────────────
    print("\n── M26-core (arm 별) · **최종 계획 기준**")
    for arm in ARMS:
        p = f = na = 0
        for r in primary:
            v = _arm_verdicts(cases.get(r["case_id"]), r["plans"].get(arm), today=today)
            if v is None:
                na += 1
                continue
            verdict, _ = R.m26_core(v)
            if isinstance(verdict, _NotApplicable):
                na += 1
            elif verdict:
                p += 1
            else:
                f += 1
        den = p + f
        print(f"   {arm:<12} {(f'{p / den:.3f}' if den else '—'):>6} ({p}/{den})   N/A {na}")

    # ── M33 = ΔM26-core (B − A) ──────────────────────────────────────────
    print("\n── M33 = ΔM26-core (B − A) · 케이스 단위 페어드 부트스트랩")
    deltas, dropped = paired_deltas(primary, cases, "A_none", "B_feedback", today=today)
    if deltas:
        pt, lo, hi = paired_bootstrap_ci(deltas)
        if lo > 0:
            verdict = "④층 유지"
        elif hi < 0:
            verdict = "**이 층을 룰로 대체하거나 걷어내자**"
        else:
            verdict = "부호 미결정 — **억지로 만들지 않는다**"
        print(f"   M33 = {pt:+.4f}   95% CI [{lo:+.4f}, {hi:+.4f}]   n={len(deltas)}")
        print(f"   → {verdict}   ({BOOTSTRAP_N}회 · 시드 {BOOTSTRAP_SEED})")
    else:
        print("   페어링 가능한 케이스가 없다")
    if dropped:
        print(f"   ⚠️ 페어링에서 빠진 {len(dropped)}건: {', '.join(dropped[:6])}")
        print("      한 arm 에서만 N/A 면 **서로 다른 케이스 집합의 차**가 된다(설계 §4.1)")

    # ── M18 은 AND 에 없고 **나란히** 본다 ───────────────────────────────
    print("\n── M18b (arm 별 분포) — **M26-core 와 나란히 본다**")
    for arm in ARMS:
        vals = [
            x
            for r in primary
            for x in [_arm_m18b(cases.get(r["case_id"]), r["plans"].get(arm), today=today)]
            if x is not None
        ]
        if vals:
            print(
                f"   {arm:<12} 중앙 {_st.median(vals):.3f} · "
                f"미달 {sum(1 for x in vals if x < 1)}/{len(vals)}"
            )

    print(
        f"\n⚠️ 일반/도전을 **섞지 않는다** — 이 파일은 [{stratum}] 전용이다.\n"
        "⚠️ 채점은 **최종 계획** 기준이라 L1-7A 의 원안 기준 M26-core 와 **절대값을 비교하지\n"
        f"   않는다** — 이 실험 안의 Δ 만 의미가 있다.\n{'=' * 74}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="M33 3-arm (실 LLM 호출)")
    p.add_argument(
        "--stratum",
        choices=("general", "challenge"),
        required=True,
        help="**필수** — 일반과 도전을 섞어 한 수치로 내지 않는다(설계 §3.4)",
    )
    p.add_argument("--limit", type=int, default=None, help="앞 N건만 (스모크)")
    p.add_argument("--repeats", type=int, default=1, help="케이스당 반복")
    p.add_argument("--dry-run", action="store_true", help="LLM 없이 구성만")
    p.add_argument("--summarize-only", action="store_true", help="저장된 원자료만 재집계")
    p.add_argument("--run", default=None, help="재집계할 원자료 경로 (기본: 가장 최근 실행)")
    p.add_argument(
        "--allow-smoke",
        action="store_true",
        help="스모크(--limit) 원자료도 재집계 — **본 실험 표본이 아니다**(설계 §4.3)",
    )
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
