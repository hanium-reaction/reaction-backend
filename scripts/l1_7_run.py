"""L1-7A 하네스 — 첫 계획 준수도 (실 LLM 호출).

`docs/experiments/experiment-plan-v1.md` §2 L1-7A 를 실행한다. 가설:

> **H1-7A**: LLM 분해 원안은 사용자가 명시한 제약(세션 길이·주당 분량·빈도)을 상당 비율로
> 벗어나며, 그 이탈을 ③층 결정적 보정이 흡수하고 있다. 즉 **현재 계획 품질의 상당 부분은
> LLM 이 아니라 룰이 만들고 있다.**

그래서 이 하네스의 핵심은 **③층 보정 전/후를 둘 다 기록**하는 것이다. 보정 후만 보면
"계획이 제약을 지킨다" 는 결론이 나오는데, 그건 룰이 지킨 것이지 LLM 이 지킨 게 아니다.

## 정답 라벨이 없어도 되는 이유

정답이 설계자가 아니라 **사용자가 인터뷰에서 답한 값**이다(`session_length_min` /
`weekly_hours` / `frequency_per_week` / `horizon`). `eval/README.md` 가 회복 골든셋에서
경계한 "설계 의도를 정확도로 쓰면 자기충족적" 문제를 원리적으로 피한다.

## 지금 재는 것과 안 재는 것

| 지표 | 상태 |
|---|---|
| M17 `session_length_compliance` | ✅ 상한초과/하한미달 2분해 |
| M18 `volume_budget_ratio` | ✅ 1.0 기준 양방향 |
| M19 `truncation_rate` | ✅ `_take_within_budget` 이 실제로 자른 수 |
| M23 `milestone_fidelity` | ✅ `milestone_fixed` 6건에서만 (나머지는 분모 0 — 미측정) |
| M24 `out_of_cycle_rate` | ✅ 〃, `can_refill` 케이스에서만 |
| M25 `waiting_step_rate` | ✅ `_WAITING_TITLE_RE` 백스톱이 잡은 수 |
| M20 `cadence_compliance` | ✅ `scripts/l1_7_schedule_eval.py` 가 배치를 돌린다 |
| M21 `placement_rate` | ✅ 〃 |
| M22 `horizon_coverage` | ✅ 〃 (negative float — 안전 불변식) |
| **M26-core** | ✅ **M18 을 뺀 8개의 AND** — 실험계획서 §5 |

⚠️ **M18 은 M26-core 에 없다.** 연속량을 이진화해 AND 에 넣으면 임계값 하나가 전체를
흔든다. 두 주지표를 나란히 낸다(실험계획서 §5). **N/A 는 실패가 아니라 중립**이고,
케이스는 분모에 남는다 — 마일스톤 없는 계획을 실패로 세면 안 된다.

## 실행

    uv run python scripts/l1_7_run.py --dry-run          # LLM 없이 구조 확인
    uv run python scripts/l1_7_run.py --limit 3          # 스모크
    uv run python scripts/l1_7_run.py --repeats 3        # 본 실행

원자료는 `eval/l1_7_results.jsonl` (비결정적 실 LLM 결과라 `.gitignore`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reaction_backend.orchestrator import first_plan, first_plan_adapter
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import GoalDecomposition, GoalNodeDraft, MilestoneDraft

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # `python scripts/...` 직접 실행 — pytest 는 이미 잡혀 있다
    sys.path.insert(0, str(_ROOT))  # M26-core 가 `scripts.l1_7_schedule_eval` 을 부른다

CASES_PATH = _ROOT / "eval" / "golden_first_plan_cases.jsonl"
RESULTS_PATH = _ROOT / "eval" / "l1_7_results.jsonl"
KST = timezone(timedelta(hours=9))

# 폴백 계획은 "LLM 이 제약을 지켰는가" 에 대해 아무것도 말하지 않는다 — 집계에서 뺀다.
# `goal_nodes` 는 min_length=1 이라 빈 리스트를 주면 ValidationError 로 죽는다(l1_6 전례).
_FALLBACK_NODE = GoalNodeDraft(
    node_id="fallback-root",
    parent_id=None,
    title="(폴백)",
    node_type="root",
    order_index=0,
    is_leaf=False,
)


def load_cases(limit: int | None = None, blocks: list[str] | None = None) -> list[dict[str, Any]]:
    """`decompose` 케이스만 읽는다 — `verify` 는 L1-7B(검토기) 몫이다."""
    rows = [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = [c for c in rows if c["kind"] == "decompose"]
    if blocks:
        cases = [c for c in cases if c["block"] in blocks]
    return cases[:limit] if limit else cases


def build_outcome(case: dict[str, Any], *, today: date) -> InterviewOutcome:
    """저장된 슬롯으로 `InterviewOutcome` 을 되짚는다.

    ⚠️ 마감은 **상대 오프셋**으로 저장돼 있다(`deadline_offset_days`). 실행일 + 같은
    오프셋으로 만들어야 마감까지 남은 일수가 골든셋을 구울 때와 같아진다 — 절대 날짜를
    저장했다면 하루만 지나도 '마감 임박' 이 '마감 지남' 이 된다.
    """
    interview, goal = case["interview"], case["interview"]["goal"]
    deadline = (today + timedelta(days=goal["deadline_offset_days"])).isoformat()
    return InterviewOutcome(
        session_id=f"l1-7-{case['case_id']}",
        generated_at=datetime(today.year, today.month, today.day, 9, 0, tzinfo=KST),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role=interview["role"], season=interview["season"]),
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
                session_length_min=goal["session_length_min"],
                weekly_hours=goal["weekly_hours"],
                frequency_per_week=goal["frequency_per_week"],
                preferred_time=interview["preferred_time"],
                approach_note=goal.get("approach_note"),
            )
        ],
        availability=AvailabilityProfile(
            # ⚠️ **입력 계약에서 읽는다.** 예전에는 `09:00-23:00` 을 하드코딩해서, 케이스에
            # 좁은 활동창을 넣어도 스케줄러에 **전혀 반영되지 않았다** — 그 축이 변별력을
            # 못 가졌다(M33 설계 검토 지적). 없으면 기존 84건과 같은 기본값을 쓴다.
            activity_window=TimeRange(
                start=interview.get("activity_start", "09:00"),
                end=interview.get("activity_end", "23:00"),
            ),
            peak_window=[interview["preferred_time"]],
        ),
        preferences=PreferenceProfile(
            recovery_tone="담백",
            rest_ok=True,
            downscope_unit_min=15,
            focus_duration_min=interview["focus_duration_min"],
        ),
        horizon=deadline,
    )


def cycle_window(
    case: dict[str, Any], outcome: InterviewOutcome, today: date
) -> list[MilestoneDraft]:
    """이번 주기가 다룰 마일스톤 — 프로덕션 `_cycle_milestones` 와 같은 계산.

    ⚠️ `horizon_weeks` 를 **상수로 흉내내지 않는다.** 2026-09-02 에 테스트에서 `2` 를
    하드코딩했다가 전 케이스의 수치가 틀렸다 — 2 는 만다라 유래 목표 전용 값이다.
    """
    raw = case["interview"].get("milestones") or []
    if not raw:
        return []
    milestones = [MilestoneDraft(title=m["title"], summary=m["summary"]) for m in raw]
    return first_plan_adapter.cycle_milestone_window(
        milestones,
        cursor=case["interview"].get("milestone_cursor", 0),
        horizon_weeks=first_plan_adapter._horizon_weeks(today, outcome.horizon),
        full_horizon_weeks=first_plan_adapter.full_horizon_weeks(today, outcome.horizon),
    )


def _out_of_cycle_note(case: dict[str, Any], window: list[MilestoneDraft]) -> str:
    """`first_plan._out_of_cycle_note` 와 **같은 문자열**을 만든다.

    프로덕션 쪽은 `FirstPlanState` 를 받아서 그대로 못 부른다. 로직을 옮겨 적었으므로
    **그쪽을 고치면 여기도 고쳐야 한다** — 갈리면 LLM 이 프로덕션과 다른 경계를 읽는다.
    """
    all_ms = case["interview"].get("milestones") or []
    if not all_ms:
        return "(없음)"
    titles = [m["title"] for m in all_ms]
    window_titles = {m.title for m in window}
    cursor = int(case["interview"].get("milestone_cursor") or 0)
    done = titles[:cursor]
    later = [t for t in titles[cursor:] if t not in window_titles]
    lines = []
    if done:
        lines.append("- 이미 끝낸 단계(다시 시키지 말 것): " + " / ".join(done))
    if later:
        lines.append("- 다음 주기가 받을 단계(여기서 시작하지 말 것): " + " / ".join(later))
    return "\n".join(lines) if lines else "(없음)"


def _corrected(
    outcome: InterviewOutcome,
    raw: GoalDecomposition,
    window: list[MilestoneDraft],
    today: date,
) -> GoalDecomposition:
    """③층 전체를 돌린 **최종 계획** — `decompose_goal` 과 같은 순서·게이트.

    `waterfall()` 이 단계별 개수를 세는 것과 같은 체인이다. 여기서 갈리면 M23 이 폭포와
    다른 계획을 보게 된다.
    """
    plan, _ = first_plan_adapter.drop_waiting_steps(raw)
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    if window and (heaviest.frequency_per_week or 0) > 0:
        plan, _ = first_plan_adapter.drop_out_of_cycle_branches(plan, window)
    plan = first_plan_adapter.shape_action_plan(outcome, "standard", plan, target_date=today)
    return first_plan_adapter.extend_action_plan_to_horizon(
        outcome, "standard", plan, target_date=today
    )


def score_raw(
    outcome: InterviewOutcome,
    raw: GoalDecomposition,
    window: list[MilestoneDraft],
    today: date,
    *,
    case_milestones: int = 0,
) -> dict[str, Any]:
    """③층 **보정 전 원안**에 대한 M17·M18·M19·M23·M24·M25."""
    items = raw.action_items
    n = len(items)
    ceiling = first_plan_adapter.session_min_for(outcome)
    floor = min(15, first_plan_adapter.planned_session_min_for(outcome), ceiling)
    minutes = [a.estimated_minutes or 0 for a in items]

    over = sum(1 for m in minutes if m > ceiling)
    under = sum(1 for m in minutes if m < floor)
    budget = first_plan_adapter.horizon_minute_budget(outcome, "standard", target_date=today)

    # M19 — ③층이 실제로 몇 개를 잘라내는가. 상한 두 개를 프로덕션과 같은 인자로 건다.
    kept = first_plan_adapter._take_within_budget(
        first_plan_adapter.normalize_action_minutes(outcome, list(items)),
        budget_min=budget,
        max_count=first_plan_adapter.cadence_session_cap(outcome, "standard", target_date=today),
    )
    # M25 — '외부 대기' 백스톱이 잡은 수 = 분해 프롬프트 규칙이 놓친 양
    _dropped_plan, waiting = first_plan_adapter.drop_waiting_steps(raw)

    # ⚠️ **M18 은 두 개다.** 프롬프트가 LLM 에게 요구한 분량(`llm_minute_target`)과 최종
    # 계획의 예산(`horizon_minute_budget`)이 **같지 않다** — 앞의 것은 세션 수 상한
    # (`_MAX_LLM_SESSIONS`)에 묶여 있어서, 상한이 걸린 케이스에서는 요구량이 예산보다 작다.
    #
    # 하나로 뭉치면 서로 다른 두 주장이 섞인다:
    #   M18a  LLM 이 **자기가 받은 지시**를 지켰는가        → 프롬프트 준수
    #   M18b  원안이 **최종 예산**에 얼마나 못 미치는가      → 최종 예산 대비 부족분
    #
    # 1차 문서는 M18b 만 내고 "LLM 이 분량 지시를 85/102 못 지켰다" 로 읽었는데, 그건
    # M18a(83/102)의 주장이지 M18b 의 주장이 아니다. 독립 검토가 지적한 자리다.
    ask = first_plan_adapter.llm_minute_target(outcome, "standard", target_date=today)
    row: dict[str, Any] = {
        "raw_leaf_count": n,
        "session_ceiling": ceiling,
        "session_floor": floor,
        "m17_over_ceiling": over,
        "m17_under_floor": under,
        "m17_in_band": n - over - under,
        "m18_raw_minutes": sum(minutes),
        "m18_prompt_target": ask,
        "m18_budget": budget,
        "m18a_ratio": (sum(minutes) / ask) if ask else None,
        "m18b_ratio": (sum(minutes) / budget) if budget else None,
        # 프롬프트 요구량 자체가 최종 예산에 못 미치는 케이스 — 그 계획은 M18b 로는
        # **구조적으로** 1.0 에 닿을 수 없다. 미달을 LLM 탓으로 읽으면 안 된다.
        "m18_target_below_budget": bool(budget and ask < budget),
        "m19_truncated": max(0, n - len(kept)),
        "m25_waiting": len(waiting),
    }
    if window:
        # ⚠️ M23 은 **최종 계획**에서 잰다 — 프로덕션이 그렇게 부른다(`first_plan.py` 의
        # `gp` 는 shape·extend 를 지난 것). `missing_milestone_titles` 의 docstring 이
        # 드는 실패 경로 1번이 "shape 이 세션 수를 자르고 _prune_to_leaves 가 leaf 없는
        # branch 를 버린다" 인데, 원안에서 재면 그 경로에 **구조적으로 눈이 먼다**.
        final = _corrected(outcome, raw, window, today)
        row["m23_window"] = len(window)
        row["m23_missing"] = len(first_plan_adapter.missing_milestone_titles(window, final))
        branches = [x for x in raw.goal_nodes if x.node_type == "branch"]
        _kept_plan, out_of_cycle = first_plan_adapter.drop_out_of_cycle_branches(raw, window)
        row["m24_branches"] = len(branches)
        row["m24_out_of_cycle"] = len(out_of_cycle)
        # ⚠️ 창이 남은 마일스톤 **전부**를 덮으면 이탈이 원리적으로 불가능하다 — 그 케이스의
        # 0 은 "재서 0" 이 아니라 **미측정**이다. 분모에 넣으면 M24 가 부풀려진다.
        row["m24_measurable"] = len(window) < case_milestones
    return row


def waterfall(
    outcome: InterviewOutcome, raw: GoalDecomposition, window: list[MilestoneDraft], today: date
) -> dict[str, int]:
    """F21 — 룰 개입량 폭포. ③층 각 단계 **뒤**의 leaf 수를 프로덕션 순서대로 기록한다.

    `decompose_goal` 과 같은 순서: drop_waiting → drop_out_of_cycle(되채울 수 있을 때만)
    → shape → extend. `_corrected` 와 **같은 체인**이다 — 그쪽은 최종 계획만 돌려주고
    여기는 단계별 개수를 센다. 한쪽을 고치면 두 곳 다 고쳐야 한다.

    ⚠️ **개수만 세므로 길이 클램프가 안 보인다.** `normalize_action_minutes` 는 카드 수를
    바꾸지 않아, 상한을 넘긴 카드가 조용히 다시 쓰여도 이 폭포는 0 으로 기록한다. 1차
    실행에서 13개 leaf 가 그렇게 재작성됐는데 폭포는 전 단계 동일로 보였다 — F21 을 이
    수치만으로 그리면 룰의 최대 개입을 빠뜨린다.
    """
    stages: dict[str, int] = {"0_llm_raw": len(raw.action_items)}
    plan, _ = first_plan_adapter.drop_waiting_steps(raw)
    stages["1_waiting_dropped"] = len(plan.action_items)

    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    if window and (heaviest.frequency_per_week or 0) > 0:
        plan, _ = first_plan_adapter.drop_out_of_cycle_branches(plan, window)
    stages["2_out_of_cycle_dropped"] = len(plan.action_items)

    plan = first_plan_adapter.shape_action_plan(outcome, "standard", plan, target_date=today)
    stages["3_shaped"] = len(plan.action_items)
    plan = first_plan_adapter.extend_action_plan_to_horizon(
        outcome, "standard", plan, target_date=today
    )
    stages["4_extended"] = len(plan.action_items)
    return stages


async def run_case(
    case: dict[str, Any], repeat: int, *, today: date, dry_run: bool
) -> dict[str, Any]:
    from reaction_backend.config import get_settings
    from reaction_backend.llm import aiClient

    outcome = build_outcome(case, today=today)
    window = cycle_window(case, outcome, today)
    ctx = first_plan_adapter.context_from_outcome(outcome, target_date=today)
    prompt_vars: dict[str, str] = ctx["prompt_vars"]

    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "block": case["block"],
        "repeat": repeat,
        "cycle_milestones": len(window),
    }
    if dry_run:
        row["horizon_weeks"] = prompt_vars.get("horizon_weeks")
        row["session_length"] = prompt_vars.get("session_length")
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
        # ⚠️ **프롬프트 변수를 손으로 조립하지 않는다.** 처음엔 `milestones` 를 직접
        # 포맷하고 `out_of_cycle` 을 통째로 빠뜨려, 전 호출이 `no_prompt`(렌더 실패)로
        # 폴백했다 — 스모크에서 2/2 폴백으로 드러났다. 프로덕션(`decompose_goal`)이 쓰는
        # 함수를 그대로 부른다. 형식이 갈리면 LLM 이 다른 것을 읽고, 그러면 이 하네스는
        # 프로덕션이 아닌 무언가를 재게 된다.
        variables={
            **prompt_vars,
            # ⚠️ 빈 문자열이 아니다. 프로덕션은 첫 분해에도 **자리표시 문장**을 보낸다
            # (`_replan_feedback` → "(첫 분해 — 이전 피드백 없음)"). 1차 실행에서 여기 ""
            # 를 넣어 34호출 전부가 프로덕션이 내지 않는 프롬프트였다 — 같은 커밋에서
            # `_format_milestones` 는 프로덕션 함수를 부르면서 이것만 빠뜨렸다.
            "review_feedback": first_plan._replan_feedback({"review": None}),
            "milestones": first_plan._format_milestones(window),
            "out_of_cycle": _out_of_cycle_note(case, window),
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
        # ⚠️ **모델 출력을 그대로 남긴다.** 1차 실행은 집계 수치만 저장해서 M23·M24·M25 를
        # 아무도 재감사할 수 없었다(l1_6 은 `sessions`·`hit_items` 를 남긴다). 34번의 실
        # LLM 호출이 복구 불가능한 상태였다.
        row["plan"] = {
            "goal_nodes": [n.model_dump(mode="json") for n in result.value.goal_nodes],
            "action_items": [a.model_dump(mode="json") for a in result.value.action_items],
        }
        row.update(
            score_raw(
                outcome,
                result.value,
                window,
                today,
                case_milestones=len(case["interview"].get("milestones") or []),
            )
        )
        row["waterfall"] = waterfall(outcome, result.value, window, today)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# M26-core — 실험계획서 §5 「M26 통과 조건」
#
# ⚠️ **M18 은 여기 없다.** M17·M19~M25 는 "위반이 있나 없나" 라 이진 판정이 자연스럽지만
# M18 은 "얼마나 벗어났나" 라 연속량이다. 억지로 이진화해 AND 에 넣으면 **임계값 하나가
# M26 전체를 흔든다.** 두 주지표를 나란히 낸다.
#
# ⚠️ **N/A 는 실패가 아니다.** 사용자에게 요구된 것 자체가 없는 경우(마일스톤 없는 계획의
# M23 등)는 AND 에서 빠지고 **케이스는 분모에 남는다.** 실패로 세면 부당하고, 케이스를
# 통째로 빼면 M26 이 마일스톤 6건만의 지표가 된다.
# ─────────────────────────────────────────────────────────────────────────────

PRIMARY_REPEAT = 0
"""주지표의 1차 추정에 쓰는 반복 회차. **사전 지정**이다 — 결과를 보고 고르면 안 된다."""

# M26-core 를 이루는 8개. M18 은 의도적으로 빠져 있다(위).
CORE_METRICS = ("m17", "m19", "m20", "m21", "m22", "m23", "m24", "m25")


def core_verdicts(row: dict[str, Any], sched_row: dict[str, Any] | None) -> dict[str, Any]:
    """한 계획의 지표별 판정. 값은 `True` / `False` / `schedule_eval.NA`.

    `sched_row` 는 같은 계획을 배치한 결과다(`scripts/l1_7_schedule_eval.py`).
    **같은 계획이어야 한다** — 다른 실행을 섞으면 AND 가 서로 다른 계획을 판정한다.
    """
    from scripts.l1_7_schedule_eval import NA

    v: dict[str, Any] = {
        "m17": row["m17_over_ceiling"] == 0 and row["m17_under_floor"] == 0,
        "m19": row["m19_truncated"] == 0,
        "m25": row["m25_waiting"] == 0,
    }
    # M23 — 마일스톤이 없으면 지킬 것이 없다. **N/A 이지 실패가 아니다.**
    if "m23_window" in row and row["m23_window"]:
        v["m23"] = row["m23_missing"] == 0
    else:
        v["m23"] = NA
    # M24 — 창이 남은 마일스톤 전부를 덮으면 이탈이 **원리적으로 불가능**하다(미측정).
    v["m24"] = row["m24_out_of_cycle"] == 0 if row.get("m24_measurable") else NA
    # M20·M21·M22 — 배치 결과가 있어야 한다.
    if sched_row is None:
        v["m20"] = v["m21"] = v["m22"] = NA
    else:
        v["m20"], v["m21"], v["m22"] = sched_row["m20"], sched_row["m21"], sched_row["m22"]
    return v


def m26_core(verdicts: dict[str, Any]) -> tuple[Any, int]:
    """`(판정, 적용된 지표 수)`.

    적용된 것이 **하나도 없으면** 통과라고 하지 않는다 — `NA` 다. 빈 AND 를 참으로
    두는 것이 "아무것도 안 재고 통과" 를 만드는 경로다.
    """
    from scripts.l1_7_schedule_eval import NA, _NotApplicable

    applied = [v for k in CORE_METRICS for v in [verdicts[k]] if not isinstance(v, _NotApplicable)]
    if not applied:
        return NA, 0
    return all(applied), len(applied)


def summarize_core(
    rows: list[dict[str, Any]], sched_by_case: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """M26-core 집계. **순수 함수라 테스트가 닿는다.**

    ⚠️ 집계 함수가 검증 밖에 있으면 분모·N/A·제외 규칙을 동시에 뒤집어도 전 테스트가
    초록이 된다 — L1-7B v4 하네스에서 실제로 그랬다(뮤테이션으로 확인됨).
    """
    from scripts.l1_7_schedule_eval import _NotApplicable

    primary = [r for r in rows if r["repeat"] == PRIMARY_REPEAT]
    out: dict[str, Any] = {"n_cases": len(primary), "per_metric": {}, "by_applied": {}}
    per: dict[str, dict[str, int]] = {m: {"pass": 0, "fail": 0, "na": 0} for m in CORE_METRICS}
    passed = failed = na_cases = 0
    for r in primary:
        v = core_verdicts(r, sched_by_case.get(r["case_id"]))
        for m in CORE_METRICS:
            key = "na" if isinstance(v[m], _NotApplicable) else ("pass" if v[m] else "fail")
            per[m][key] += 1
        verdict, applied = m26_core(v)
        if isinstance(verdict, _NotApplicable):
            na_cases += 1
        elif verdict:
            passed += 1
        else:
            failed += 1
        out["by_applied"].setdefault(applied, {"pass": 0, "fail": 0})
        if not isinstance(verdict, _NotApplicable):
            out["by_applied"][applied]["pass" if verdict else "fail"] += 1
    out["per_metric"] = per
    out["pass"] = passed
    out["fail"] = failed
    out["na"] = na_cases
    return out


def _pct(num: int, den: int) -> str:
    return "—" if den == 0 else f"{num / den:.3f} ({num}/{den})"


def _print_m26_core(ok: list[dict[str, Any]]) -> None:
    """M26-core 를 낸다 — **같은 계획 위에서** 배치까지 돌려 M17~M25 를 합친다.

    ⚠️ 배치는 `scripts/l1_7_schedule_eval.py` 가 한다. **LLM 을 다시 부르지 않는다** —
    저장된 `plan` 을 스케줄러에 태울 뿐이라 `--summarize-only` 로도 나온다.
    """
    from datetime import date as _date

    from scripts import l1_7_schedule_eval as sched

    cases = {c["case_id"]: c for c in load_cases()}
    today = _date.today()
    sched_by_case: dict[str, dict[str, Any]] = {}
    for r in ok:
        if r["repeat"] != PRIMARY_REPEAT or "plan" not in r:
            continue
        case = cases.get(r["case_id"])
        if case is None:
            continue
        sched_by_case[r["case_id"]] = sched.evaluate_case(
            case, today=today, action_items=r["plan"]["action_items"]
        )

    m = summarize_core(ok, sched_by_case)
    print(f"\n{'=' * 72}")
    print(f"── **M26-core** (repeat {PRIMARY_REPEAT} 의 고유 {m['n_cases']}건)")
    print("   제약·배치·마일스톤 조건의 AND. **M18 은 여기 없다** — 나란히 보는 주지표다.")
    den = m["pass"] + m["fail"]
    print(f"   통과 {_pct(m['pass'], den)}   · 전 지표 N/A 인 케이스 {m['na']}건")

    print("\n   지표별 (통과율 · 적용 · N/A):")
    for k in CORE_METRICS:
        v = m["per_metric"][k]
        applied = v["pass"] + v["fail"]
        rate = f"{v['pass'] / applied:.3f}" if applied else "—"
        print(
            f"     {k.upper():<5} {rate:>6} ({v['pass']}/{applied})   "
            f"**적용 {applied}건 · N/A {v['na']}건**"
        )
    print("   ⚠️ 적용 사례 수를 빼고 통과율만 읽으면 안 된다 (실험계획서 §5).")

    if m["by_applied"]:
        print("\n   적용된 지표 수별 — 수가 다른 케이스를 한 비율로 뭉개지 않는다:")
        for n in sorted(m["by_applied"]):
            v = m["by_applied"][n]
            print(f"     {n}개 적용: 통과 {_pct(v['pass'], v['pass'] + v['fail'])}")

    if not sched_by_case:
        print("\n   ⚠️ 배치 결과가 없어 M20·M21·M22 가 전부 N/A 다.")
    else:
        print(
            "\n   ⚠️ **배치는 '달력이 빈 사용자' 조건이다** — DB 유래 입력(승인된 다른 계획)을\n"
            "      비웠다. 실사용자는 더 어렵고 M20·M21 은 여기서 낙관적이다.\n"
            "   ⚠️ **M22 는 이 조건에서 거의 항상 참인 안전 불변식**이다 — 성과 주장에 쓰지 않는다."
        )
    print("=" * 72)


def summarize(rows: list[dict[str, Any]]) -> None:
    ok = [r for r in rows if not r.get("fell_back") and "raw_leaf_count" in r]
    fb = [r for r in rows if r.get("fell_back")]
    print(
        f"\n{'=' * 72}\nL1-7A 결과 — 실행 {len(rows)}건 / 집계 대상 {len(ok)}건 "
        f"/ 룰 폴백 {len(fb)}건 (집계 제외)"
    )
    if fb:
        print("  폴백 사유:", dict(Counter(r.get("reason") or "?" for r in fb)))
    if not ok:
        print("  집계할 것이 없다.")
        return

    leaves = sum(r["raw_leaf_count"] for r in ok)
    over = sum(r["m17_over_ceiling"] for r in ok)
    under = sum(r["m17_under_floor"] for r in ok)
    # §5 는 M17~M26 을 **micro / macro 둘 다** 보고하라고 못박는다 — 사유도 적혀 있다:
    # "micro 만 보고하면 실제 체감보다 좋아 보인다". 1차 실행은 micro 만 냈다.
    clean17 = sum(1 for r in ok if r["m17_over_ceiling"] == 0 and r["m17_under_floor"] == 0)
    print(f"\n── M17 세션 길이 준수 (③층 보정 **전** 원안, leaf {leaves}개 / 계획 {len(ok)}개)")
    print(f"   micro 밴드 안 : {_pct(leaves - over - under, leaves)}   ← leaf 단위")
    print(
        f"   macro 밴드 안 : {_pct(clean17, len(ok))}   ← **계획 단위** (한 장이라도 벗어나면 실패)"
    )
    print(f"   상한 초과     : {_pct(over, leaves)}   ← 사용자 집중용량을 넘긴 카드")
    print(f"   하한 미달     : {_pct(under, leaves)}   ← 9분 garbage 계열")

    def _m18(key: str, label: str, note: str) -> None:
        vals = [r[key] for r in ok if r.get(key) is not None]
        if not vals:
            return
        print(f"\n── {label} (n={len(vals)})")
        print(f"   {note}")
        print(
            f"   중앙값 {statistics.median(vals):.3f} · 최소 {min(vals):.3f} · 최대 {max(vals):.3f}"
        )
        # ⚠️ **임계값을 새로 만들지 않는다.** 1차 실행은 "미달(<0.8) 3건" 이라고 냈는데
        # 0.8 은 사전등록 어디에도 없는 **분석 시점 임계값**이었다 — 같은 커밋이 M26 을
        # 내지 않은 이유가 정확히 그것("분석 시점에 임계값을 정하면 §0.1 위반")인데
        # M18 에는 적용한 셈이다. §5 의 읽는 규칙은 명시적이다: **1.0 미만이면 과소 생성.**
        print(
            f"   1.0 미만(=과소 생성) {sum(1 for x in vals if x < 1.0)}건 · "
            f"정확히 1.0 {sum(1 for x in vals if x == 1.0)}건 · "
            f"초과(>1.0) {sum(1 for x in vals if x > 1.0)}건"
        )

    _m18(
        "m18a_ratio",
        "M18a 프롬프트 분량 준수",
        "LLM 이 **자기가 받은 지시**(`total_minutes`)를 지켰는가",
    )
    _m18(
        "m18b_ratio",
        "M18b 최종 커버리지 부족",
        "원안이 **최종 예산**(`horizon_minute_budget`)에 얼마나 못 미치는가 (= 부족분)",
    )
    capped = [r["case_id"] for r in ok if r.get("m18_target_below_budget")]
    if capped:
        uniq = sorted(set(capped))
        print(
            f"\n   ⚠️ 세션 수 상한이 걸려 **프롬프트 요구량 < 최종 예산** 인 케이스 "
            f"{len(uniq)}종 ({len(capped)}행): {', '.join(uniq)}"
        )
        print("      이 케이스들은 M18b 로는 구조적으로 1.0 에 못 닿는다 — LLM 탓이 아니다.")
    ratios = [r["m18b_ratio"] for r in ok if r.get("m18b_ratio") is not None]
    if ratios:
        print(f"   M18b 분포: {[round(x, 3) for x in sorted(ratios)]}")

    trunc = sum(r["m19_truncated"] for r in ok)
    wait = sum(r["m25_waiting"] for r in ok)
    print(
        f"\n── M19 절단율   micro {_pct(trunc, leaves)} · "
        f"macro {_pct(sum(1 for r in ok if r['m19_truncated'] == 0), len(ok))} 무절단 계획"
    )
    print(
        f"── M25 대기단계 micro {_pct(wait, leaves)} · "
        f"macro {_pct(sum(1 for r in ok if r['m25_waiting'] == 0), len(ok))} 무대기 계획"
    )
    joint = sum(
        1
        for r in ok
        if r["m17_over_ceiling"] == 0
        and r["m17_under_floor"] == 0
        and r["m19_truncated"] == 0
        and r["m25_waiting"] == 0
    )
    print(f"\n── M17·M19·M25 를 **한 계획 안에서 전부** 통과: {_pct(joint, len(ok))}")
    print("   ⚠️ 이것은 M26-core 가 아니다 — 아래 M26-core 절을 볼 것.")

    ms = [r for r in ok if "m23_window" in r]
    if ms:
        win = sum(r["m23_window"] for r in ms)
        miss = sum(r["m23_missing"] for r in ms)
        # ⚠️ M24 의 분모는 **이탈이 가능한 케이스만**이다. 창이 남은 마일스톤 전부를 덮으면
        # (마감이 계획 지평 안) 이탈이 원리적으로 불가능해, 그 0 은 "재서 0" 이 아니라
        # **미측정**이다 — 1차 실행은 그걸 분모에 넣어 14 로 보고했다.
        m24 = [r for r in ms if r.get("m24_measurable")]
        br = sum(r["m24_branches"] for r in m24)
        ooc = sum(r["m24_out_of_cycle"] for r in m24)
        print(f"\n── M23 마일스톤 충실도 ({len(ms)}건 / 창 {win}개 — **최종 계획**에서 잰다)")
        print(f"   누락 {_pct(miss, win)}   ← 이번 주기 마일스톤 중 계획에 안 남은 것")
        print(
            f"── M24 범위 이탈 ({len(m24)}건에서만 측정 가능 — "
            f"{len(ms) - len(m24)}건은 창이 전부를 덮어 이탈 불가)"
        )
        print(f"   {_pct(ooc, br)}   ← 원안 branch 중 구간 밖")
    else:
        print("\n── M23·M24 : 마일스톤을 가진 케이스가 실행에 없었다 — 미측정")

    wfs = [r["waterfall"] for r in ok if "waterfall" in r]
    if wfs:
        print(f"\n── F21 룰 개입량 폭포 (평균 leaf 수, n={len(wfs)})")
        for stage in (
            "0_llm_raw",
            "1_waiting_dropped",
            "2_out_of_cycle_dropped",
            "3_shaped",
            "4_extended",
        ):
            vals = [w[stage] for w in wfs]
            print(f"   {stage:26} {statistics.mean(vals):6.2f}")
        raw0 = statistics.mean([w["0_llm_raw"] for w in wfs])
        fin = statistics.mean([w["4_extended"] for w in wfs])
        print(f"   → LLM 원안 {raw0:.2f} → 최종 {fin:.2f}  (룰이 만든 몫 {fin - raw0:+.2f})")

    # ── 반복 간 안정성 ────────────────────────────────────────────────────
    # LLM 은 비결정적이라 **한 번 돌린 수치는 그 수치의 신뢰구간을 말해주지 않는다.**
    # 반복별로 같은 지표를 따로 내서, 차이가 결론을 뒤집을 만한지 눈으로 볼 수 있게 한다.
    repeats = sorted({r["repeat"] for r in ok})
    if len(repeats) > 1:
        print(f"\n── 반복 간 안정성 (n={len(repeats)}회)")
        # ⚠️ M18 은 **a/b 를 따로** 낸다. 커밋 `032d5ba` 가 둘을 쪼갠 이유가
        # "하나로 뭉치면 서로 다른 두 주장이 섞인다" 였는데, 이 표만 옛 단일 키를 읽고
        # 있었다. 옛 원자료에는 그 키가 남아 있어 `--summarize-only` 로는 안 터졌고
        # **새 실행에서 처음 크래시**했다(빈 리스트 median).
        print(
            f"   {'회차':<6}{'leaf':>6}{'M17 micro':>12}{'M17 macro':>12}"
            f"{'M18a 중앙':>11}{'M18b 중앙':>11}{'M18b<1.0':>10}{'M19':>6}{'M25':>6}"
        )
        for rep in repeats:
            sub = [r for r in ok if r["repeat"] == rep]
            lv = sum(r["raw_leaf_count"] for r in sub)
            ov = sum(r["m17_over_ceiling"] for r in sub)
            un = sum(r["m17_under_floor"] for r in sub)
            cl = sum(1 for r in sub if r["m17_over_ceiling"] == 0 and r["m17_under_floor"] == 0)
            ra = [r["m18a_ratio"] for r in sub if r.get("m18a_ratio") is not None]
            rb = [r["m18b_ratio"] for r in sub if r.get("m18b_ratio") is not None]
            # 빈 리스트에 median 을 걸면 크래시한다 — 계산 불가는 '—' 로 찍고 넘어간다.
            ma = f"{statistics.median(ra):.3f}" if ra else "—"
            mb = f"{statistics.median(rb):.3f}" if rb else "—"
            print(
                f"   {rep:<6}{lv:>6}{(lv - ov - un) / lv:>12.3f}{cl / len(sub):>12.3f}"
                f"{ma:>11}{mb:>11}{sum(1 for x in rb if x < 1.0):>10}"
                f"{sum(r['m19_truncated'] for r in sub):>6}"
                f"{sum(r['m25_waiting'] for r in sub):>6}"
            )

        # 케이스 단위 흔들림 — 같은 케이스가 회차마다 다른 판정을 받는가
        flip = 0
        for cid in {r["case_id"] for r in ok}:
            verdicts = {
                (r["m17_over_ceiling"] == 0 and r["m17_under_floor"] == 0)
                for r in ok
                if r["case_id"] == cid
            }
            if len(verdicts) > 1:
                flip += 1
        n_cases = len({r["case_id"] for r in ok})
        print(f"   M17 판정이 회차마다 뒤집힌 케이스: {_pct(flip, n_cases)}")
        # ⚠️ **"그 케이스들이 경계에 있다" 로 읽으면 안 된다.** 전 케이스가 동일 확률
        # p 인 동전이어도 3회 중 뒤집힐 기대값은 34 × (1 − p³ − (1−p)³) 이라 두 자릿수가
        # 정상이다. 관측 뒤집힘이 그 기대값을 **넘을 때만** 특정 케이스의 불안정성을
        # 말할 수 있다 — 그래서 기대값을 함께 찍는다 (l1-7-results.md §4 에서 철회한 주장).
        p_hat = sum(
            1 for r in ok if r["m17_over_ceiling"] == 0 and r["m17_under_floor"] == 0
        ) / len(ok)
        expected = n_cases * (1 - p_hat**3 - (1 - p_hat) ** 3)
        print(
            f"   ⚠️ 우연 기대값 {expected:.1f}건 (동일 확률 p={p_hat:.3f} 가정) — "
            "관측이 이보다 크지 않으면 **불안정 증거가 없다.** 넘더라도 반대는 성립하지 않는다 — "
            "케이스 간 이질성은 오목성 때문에 뒤집힘을 **줄인다**(Jensen)."
        )

    _print_m26_core(ok)

    lat = [r["latency_ms"] for r in ok if r.get("latency_ms")]
    if lat:
        s = sorted(lat)
        print(
            f"\n── 시스템 : 지연 중앙 {statistics.median(s):.0f}ms · "
            f"p95 {s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)]:.0f}ms · "
            f"토큰 in {sum(r.get('tokens_in') or 0 for r in ok)} / "
            f"out {sum(r.get('tokens_out') or 0 for r in ok)}"
        )

    print(
        f"\n⚠️ M26-core 는 위 절에 있다. **M18 은 거기 없고** 나란히 보는 주지표다. "
        f"배치는 '달력이 빈 사용자' 조건이라 M20·M21 이 낙관적이다.\n{'=' * 72}"
    )


async def main_async(args: argparse.Namespace) -> None:
    # 집계 로직만 고쳤을 때 LLM 을 다시 부르지 않는다 — 34호출은 공짜가 아니고, 같은
    # 원자료를 다시 집계하는 것이 재현성 면에서도 옳다.
    if args.summarize_only:
        rows = [
            json.loads(line)
            for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        print(f"저장된 원자료 재집계: {RESULTS_PATH.relative_to(_ROOT)} ({len(rows)}행)")
        # 집계 정의가 바뀌면 옛 행에 새 필드가 없다. **저장해 둔 모델 출력에서 되살린다** —
        # LLM 을 다시 부르지 않고도 새 지표를 옛 실행에 적용할 수 있다는 것이, 출력을
        # 통째로 저장하기로 한 이유다(1차 실행은 집계 수치만 남겨 이게 불가능했다).
        cases = {c["case_id"]: c for c in load_cases()}
        today = date.today()
        backfilled = 0
        for row in rows:
            if "m18a_ratio" in row or "plan" not in row or row["case_id"] not in cases:
                continue
            outcome = build_outcome(cases[row["case_id"]], today=today)
            minutes = sum(i["estimated_minutes"] or 0 for i in row["plan"]["action_items"])
            ask = first_plan_adapter.llm_minute_target(outcome, "standard", target_date=today)
            budget = first_plan_adapter.horizon_minute_budget(
                outcome, "standard", target_date=today
            )
            row["m18_prompt_target"] = ask
            row["m18a_ratio"] = (minutes / ask) if ask else None
            row["m18b_ratio"] = (minutes / budget) if budget else None
            row["m18_target_below_budget"] = bool(budget and ask < budget)
            backfilled += 1
        if backfilled:
            print(f"  (모델 출력에서 M18a/M18b 를 되살린 행: {backfilled})")
        summarize(rows)
        return

    today = date.today()
    cases = load_cases(limit=args.limit, blocks=args.blocks)
    print(
        f"케이스 {len(cases)}건 × 반복 {args.repeats}회 = 호출 {len(cases) * args.repeats}건"
        f"{' (dry-run)' if args.dry_run else ''}"
    )

    rows: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for case in cases:
            row = await run_case(case, repeat, today=today, dry_run=args.dry_run)
            rows.append(row)
            flag = "!" if row.get("fell_back") else "."
            print(flag, end="", flush=True)
    print()

    if not args.dry_run:
        RESULTS_PATH.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        print(f"원자료: {RESULTS_PATH.relative_to(_ROOT)} ({len(rows)}행)")
    summarize(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="L1-7A 첫 계획 준수도 실행 (실 LLM 호출)")
    parser.add_argument("--limit", type=int, default=None, help="앞 N건만 (스모크)")
    parser.add_argument("--repeats", type=int, default=1, help="케이스당 반복 횟수")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 구성만 확인")
    parser.add_argument(
        "--summarize-only", action="store_true", help="저장된 원자료만 다시 집계 (LLM 호출 없음)"
    )
    parser.add_argument(
        "--blocks",
        nargs="*",
        default=None,
        help="블록 필터 (normal / constraint_edge / milestone_fixed / busy_saturated)",
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
