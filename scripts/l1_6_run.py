"""L1-6 실행 하네스 — 자료 골든셋 48건을 분해에 돌려 M13~M15 를 뽑는다.

`docs/experiments/experiment-plan-v1.md` §2 L1-6 의 사양을 구현한다. 골든셋
(`eval/golden_materials_cases.jsonl`) 각 케이스를 **프로덕션과 같은 분해 경로**에 태운다:

    골든셋 case → InterviewOutcome (결정적 조립)
                → first_plan_adapter.context_from_outcome  ← 프로덕션과 같은 함수
                → aiClient.run(planning/goal_decompose)    ← 프로덕션과 같은 호출
                → 지표 집계

**인터뷰를 건너뛰는 이유**: 이 실험이 재는 건 "자료가 분해에 어떻게 반영되는가"다. 인터뷰
16턴을 케이스마다 돌리면 케이스당 LLM 호출이 20배로 늘고, 인터뷰 정규화의 변동성이 자료
효과에 섞인다. 골든셋이 목표·자료를 이미 확정된 형태로 들고 있으므로 outcome 을 직접
조립하는 게 더 정확하고 싸다.

**`session=None`** — budget check 와 `llm_runs` INSERT 를 건너뛴다(`l1_1_generate.py` ·
`prompt_lab.py` 와 같은 이유: 실 사용자 트래픽이 아니라 오프라인 연구용 호출이다).

⚠️ **비용 경고**: 기본 실행은 실 Gemini 호출 48회다(`--repeats` 배). `GEMINI_API_KEY` 가
없으면 전부 룰 폴백으로 기록되고, 그 회차는 **지표에서 제외**된다(폴백 계획은 프롬프트가
자료를 어떻게 썼는지에 대해 아무것도 말해주지 않는다). `--limit` 로 먼저 스모크 할 것.

⚠️ **M16(되묻기)은 이 하네스로 측정할 수 없다**: 되묻기는 인터뷰 슬롯 층
(`materials_link_only_warning`)에서 일어나는데 여기선 인터뷰를 타지 않는다. 대신 그 전제
조건인 **`materials` 변수가 `(없음)` 으로 떨어졌는지**를 결정적으로 확인해 `materials_
fallback_rate` 로 보고한다. 사용자에게 실제로 되묻는지는 API 경로 검증에서 따로 봐야 한다
— 계획서 §5 의 "계산 불가 판정 (정직 표기)" 관행을 따른다.

실행:
  uv run python -m scripts.l1_6_run                  # 48건 × 1회
  uv run python -m scripts.l1_6_run --limit 3        # 앞 3건만 (스모크)
  uv run python -m scripts.l1_6_run --repeats 3      # 144 호출
  uv run python -m scripts.l1_6_run --dry-run        # LLM 호출 없이 변수 구성만 확인
  uv run python -m scripts.l1_6_run --only injection-fence-escape-stats_book --repeats 16
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import GoalDecomposition, GoalNodeDraft

_log = logging.getLogger(__name__)

RESULTS_PATH = Path(__file__).resolve().parent.parent / "eval" / "l1_6_results.jsonl"

# 프로덕션 분해 호출과 같은 조건 (`orchestrator/first_plan.py::decompose_goal`).
# 타임아웃·thinking 은 settings 에서 읽어 프로덕션과 어긋나지 않게 한다.
_MATERIALS_ABSENT = "(없음)"

# 폴백 자리표시자 — 내용은 쓰이지 않는다(집계에서 제외). 스키마의 min_length=1 을 만족시키는
# 최소 형태일 뿐이다.
_FALLBACK_NODE = GoalNodeDraft(
    node_id="fallback",
    parent_id=None,
    title="(폴백)",
    node_type="root",
    order_index=0,
    is_leaf=False,
)


def load_cases(limit: int | None = None, only: list[str] | None = None) -> list[dict[str, Any]]:
    """커밋된 골든셋 파일을 읽는다 — 생성기를 다시 부르지 않는 이유는 l1_1_common 과 동일.

    `only` 는 특정 case_id 만 골라 반복 실행할 때 쓴다(예: 인젝션 관철 사례를 가드 유무로
    각각 N 회 돌려 발생률을 비교). `--limit` 는 파일 앞부분만 잘라서 특정 블록에 못 닿는다.
    """
    from scripts.build_golden_materials_cases import OUTPUT_PATH

    with OUTPUT_PATH.open(encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]
    if only:
        known = {c["case_id"] for c in cases}
        missing = set(only) - known
        if missing:
            raise SystemExit(f"골든셋에 없는 case_id: {sorted(missing)}")
        cases = [c for c in cases if c["case_id"] in only]
    return cases[:limit] if limit is not None else cases


def build_outcome(case: dict[str, Any], *, today: date) -> tuple[InterviewOutcome, str | None]:
    """골든셋 케이스 → (InterviewOutcome, fetched_materials).

    자료의 **제공 경로**를 프로덕션과 같은 모양으로 재현한다 — 이게 이 함수의 핵심이다:

    | provenance | materials_note | fetched | 프로덕션에서 언제 |
    |---|---|---|---|
    | `pasted` | 원문 | None | 사용자가 내용을 그대로 붙여넣음 |
    | `link_fetched` | URL | 본문 | 링크만 줬고 우리가 열어서 가져옴 (#226) |
    | `none` | None | None | 자료 칸을 비움 |
    | `unfetchable` | URL | None | 링크만 줬는데 못 엶 → `(없음)` 으로 떨어져야 함 |

    `link_fetched` 에서 note 에 URL 을 넣는 게 중요하다 — note 에 본문을 넣으면
    `materials_is_link_only` 가 False 가 되어 fetch 경로가 아니라 붙여넣기 경로가 된다.
    """
    goal = case["goal"]
    materials = case["materials"]
    provenance = materials["provenance"]

    note: str | None
    fetched: str | None
    if provenance == "pasted":
        note, fetched = materials["text"], None
    elif provenance == "link_fetched":
        note, fetched = materials["url"], materials["text"]
    elif provenance == "unfetchable":
        note, fetched = materials["url"], None
    else:  # none
        note, fetched = None, None

    deadline = (today + timedelta(days=goal["deadline_offset_days"])).isoformat()
    candidate = GoalCandidate(
        title=goal["title"],
        category="study",
        is_heaviest=True,
        deadline=deadline,
        success_image=goal["success_image"],
        current_level=goal["current_level"],
        weekly_hours=goal["session_length_minutes"] * goal["sessions_per_week"] // 60,
        session_length_min=goal["session_length_minutes"],
        frequency_per_week=goal["sessions_per_week"],
        preferred_time="저녁",
        approach_note=None,
        materials_note=note,
        confidence=1.0,
    )
    outcome = InterviewOutcome(
        session_id=case["case_id"],
        generated_at=now_kst(),
        end_reason="completed",
        ambiguity_final=0.0,
        identity=IdentityContext(role="대학생", season="학기 중"),
        core_goals=[candidate],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"),
            peak_window=["저녁"],
        ),
        preferences=PreferenceProfile(
            recovery_tone="담백", rest_ok=True, downscope_unit_min=15, focus_duration_min=90
        ),
        horizon=deadline,
    )
    return outcome, fetched


def _plan_text(plan: GoalDecomposition) -> str:
    """지표 판정의 대상 — 세션 제목과 이유, 노드 제목까지 합친다.

    제목만 보면 이유 칸에 새어 나온 자료 항목·금지 주제를 놓친다(실측: 인젝션이 요구한
    문구가 제목이 아니라 이유에 실리는 경우가 있었다).
    """
    parts: list[str] = []
    for a in plan.action_items:
        parts.append(a.title)
        parts.append(getattr(a, "reason", "") or "")
    parts.extend(n.title for n in plan.goal_nodes)
    return " | ".join(p for p in parts if p)


def score(case: dict[str, Any], plan: GoalDecomposition) -> dict[str, Any]:
    """케이스 1회 실행의 지표 원자료. 비율 계산은 집계 단계에서 한다.

    문자열 포함(substring) 판정이다. 형태 변화(조사·띄어쓰기)를 놓치므로 **적중률은
    하한**으로 읽어야 한다 — 골든셋의 정답·금지 항목을 짧은 핵심어로 고른 이유다.
    """
    blob = _plan_text(plan)
    anchors = [w for w in case["anchor_items"] if w in blob]
    hit = [w for w in case["expected_items"] if w in blob]
    bad = [w for w in case["forbidden_items"] if w in blob]
    noise = [w for w in case["noise_items"] if w in blob]
    leaked = [w for w in case["assertions"]["must_not_contain"] if w in blob]
    return {
        "sessions": len(plan.action_items),
        # 자료에만 있는 고유 문자열 — 자료 있는 블록에선 "읽었다", 없는 블록에선 "아는 척".
        "anchor_total": len(case["anchor_items"]),
        "anchor_hit": len(anchors),
        "anchor_items_hit": anchors,
        "expected_total": len(case["expected_items"]),
        "expected_hit": len(hit),
        "hit_items": hit,
        "forbidden_total": len(case["forbidden_items"]),
        "forbidden_hit": len(bad),
        "forbidden_items_hit": bad,
        "noise_total": len(case["noise_items"]),
        "noise_hit": len(noise),
        "injection_leaked": leaked,
    }


async def run_case(
    case: dict[str, Any], repeat: int, *, today: date, dry_run: bool
) -> dict[str, Any]:
    from reaction_backend.config import get_settings
    from reaction_backend.llm import aiClient

    outcome, fetched = build_outcome(case, today=today)
    ctx = first_plan_adapter.context_from_outcome(
        outcome, target_date=today, fetched_materials=fetched
    )
    prompt_vars: dict[str, str] = ctx["prompt_vars"]
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "block": case["block"],
        "repeat": repeat,
        "materials_fallback": prompt_vars["materials"] == _MATERIALS_ABSENT,
    }
    if dry_run:
        row["materials_preview"] = prompt_vars["materials"][:120]
        return row

    settings = get_settings()
    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        prompt_id="planning/goal_decompose",
        # 폴백 계획은 자료를 어떻게 썼는지에 대해 아무것도 말하지 않는다 — 최소 형태만 주고
        # 집계에서 제외한다(`fell_back` 로 거른다). 프로덕션의 룰 폴백을 흉내내면 그
        # 자리표시자 세션이 지표에 섞인다.
        # ⚠️ `goal_nodes` 는 min_length=1 이라 빈 리스트를 주면 폴백 경로에서 ValidationError
        # 로 죽는다 — 실제로 전체 실행 중에 죽었다(스모크는 폴백이 안 걸려 안 드러났다).
        fallback=lambda: GoalDecomposition(
            goal_nodes=[_FALLBACK_NODE], action_items=[], policy_violations=[]
        ),
        timeout=settings.llm_planning_timeout_seconds,
        thinking_budget=settings.llm_planning_thinking_budget,
        variables={**prompt_vars, "review_feedback": "", "milestones": ""},
        session=None,
        user_id=None,
    )
    # ⚠️ **어느 프롬프트로 잰 값인지 원자료에 남긴다.** 2026-09-07 에 `goal_decompose` 가
    # 파일 추가만으로 v2 → v3 로 승격됐는데, 그때까지 원자료에 버전이 없어 기존 수치가
    # 어느 버전에서 나온 것인지 판별할 수 없었다.
    row["prompt_version"] = result.prompt_version
    row["fell_back"] = result.fell_back
    row["reason"] = result.reason
    row["tokens_in"] = result.tokens_in
    row["tokens_out"] = result.tokens_out
    row["latency_ms"] = result.latency_ms
    if not result.fell_back:
        row.update(score(case, result.value))
    return row


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def _fmt(value: float | None) -> str:
    """분모가 0 인 블록(정답·금지 항목이 없는 블록)은 0.00 이 아니라 '—' 로 찍는다."""
    return "—" if value is None else f"{value:.2f}"


def summarize(rows: list[dict[str, Any]]) -> None:
    usable = [r for r in rows if not r.get("fell_back", True)]
    fell = len(rows) - len(usable)

    print("\n" + "=" * 82)
    print(
        f"{'블록':<24}{'n':<5}{'M13a 앵커':<12}{'M13 적중':<12}"
        f"{'M14 금지':<12}{'M15 잡음':<12}{'세션'}"
    )
    print("=" * 94)
    by_block: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in usable:
        by_block[r["block"]].append(r)

    for block in (
        "grounded_clean",
        "grounded_noisy",
        "omission_probe",
        "no_material",
        "unfetchable",
        "adversarial_injection",
    ):
        rs = by_block.get(block, [])
        if not rs:
            print(f"{block:<24}{'0':<5}{'—':<12}{'—':<12}{'—':<12}{'—':<12}—")
            continue
        m13a = _ratio(sum(r["anchor_hit"] for r in rs), sum(r["anchor_total"] for r in rs))
        m13 = _ratio(sum(r["expected_hit"] for r in rs), sum(r["expected_total"] for r in rs))
        m14 = _ratio(sum(r["forbidden_hit"] for r in rs), sum(r["forbidden_total"] for r in rs))
        m15 = _ratio(sum(r["noise_hit"] for r in rs), sum(r["noise_total"] for r in rs))
        sess = sum(r["sessions"] for r in rs) / len(rs)
        print(
            f"{block:<24}{len(rs):<5}{_fmt(m13a):<12}{_fmt(m13):<12}"
            f"{_fmt(m14):<12}{_fmt(m15):<12}{sess:.1f}"
        )
    print("=" * 94)

    # 핵심 비교 — M14 는 절대값이 아니라 no_material 대비 증감으로만 읽는다(계획서 §2 L1-6).
    probe = by_block.get("omission_probe", [])
    base = by_block.get("no_material", [])
    if probe and base:
        p = _ratio(sum(r["forbidden_hit"] for r in probe), sum(r["forbidden_total"] for r in probe))
        b = _ratio(sum(r["forbidden_hit"] for r in base), sum(r["forbidden_total"] for r in base))
        if p is not None and b is not None:
            print(f"M14 핵심 비교 — omission_probe {p:.2f} vs no_material(기준선) {b:.2f}")
            print(f"  차이 {p - b:+.2f}  (음수여야 '자료가 일반론을 밀어냈다')")

    # 아는 척 — **앵커 기반**. 1차 실행에서 expected_items 로 재다가 도메인 상식과 구분이
    # 안 돼 무효였던 걸 대체한다(l1-6-results.md §3-③). 앵커는 목표 문구로 추론 불가능하다.
    for block in ("unfetchable", "no_material"):
        rs = by_block.get(block, [])
        if not rs:
            continue
        pretend = sum(1 for r in rs if r["anchor_hit"] > 0)
        hits = sorted({a for r in rs for a in r["anchor_items_hit"]})
        tail = f" — {hits}" if hits else ""
        print(f"{block:<16} 아는 척(앵커 등장) {pretend}/{len(rs)}건{tail}")
    fallback_rate = _ratio(sum(1 for r in rows if r["materials_fallback"]), len(rows))
    if fallback_rate is not None:
        print(f"materials 가 '(없음)' 으로 떨어진 비율: {fallback_rate:.2f} (M16 전제 조건)")

    leaked = [r for r in usable if r.get("injection_leaked")]
    print(
        f"인젝션 문구 유출: {len(leaked)}건 "
        + (str([r["case_id"] for r in leaked]) if leaked else "")
    )
    print(f"룰 폴백으로 제외된 회차: {fell}/{len(rows)}")
    print("[!] 전 케이스 synthetic - 보고 시 합성 비율을 명시할 것")


async def main_async(args: argparse.Namespace) -> None:
    cases = load_cases(args.limit, args.only)
    today = date.today()
    rows: list[dict[str, Any]] = []
    total = len(cases) * args.repeats
    done = 0
    for repeat in range(args.repeats):
        for case in cases:
            row = await run_case(case, repeat, today=today, dry_run=args.dry_run)
            rows.append(row)
            done += 1
            if done % 10 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
        newline="\n",
    )
    print(f"\n원자료: {RESULTS_PATH}")
    if args.dry_run:
        absent = sum(1 for r in rows if r["materials_fallback"])
        print(f"dry-run — LLM 호출 없음. materials='(없음)' {absent}/{len(rows)}건")
        return
    summarize(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="L1-6 자료 골든셋 실행 (실 LLM 호출)")
    parser.add_argument("--limit", type=int, default=None, help="앞 N건만 (스모크)")
    parser.add_argument("--repeats", type=int, default=1, help="케이스당 반복 횟수")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 변수 구성만")
    parser.add_argument(
        "--only",
        type=lambda v: [x.strip() for x in v.split(",") if x.strip()],
        default=None,
        help="case_id 만 골라 실행 (쉼표 구분) — 특정 케이스 반복 측정용",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
