"""L1-7A 배치 경로 — **M20·M21·M22 를 eval 전용으로 계산한다.**

L1-7A 하네스가 지금까지 이 셋을 못 낸 이유는 **배치(스케줄러) 결과가 없어서**다.
프로덕션 `first_plan.schedule_blocks` 는 DB(시간정책·고정일정·승인 블록·캘린더)를 읽는
async 노드라 골든셋에 바로 태울 수 없다.

**그런데 스케줄러 본체 `plan_scheduler.schedule_actions_multiday` 는 순수 함수다** —
데이터와 콜백만 받는다. 그래서 DB 유래 입력만 비우면 같은 배치 알고리즘을 그대로 돌릴 수
있다. 이 모듈이 그 배선이다.

## ⚠️ 프로덕션은 한 줄도 바꾸지 않는다

`src/` 는 **읽기만** 한다. 이 모듈을 `src/` 어디에서도 import 하지 않는다
(`tests/test_schedule_eval_contract.py::test_eval_path_does_not_touch_production`).

## 무엇을 일부러 뺐나 — 그리고 그게 결과에 무엇을 의미하나

`SCHEDULER_ARGS_OMITTED` 의 둘은 **DB 유래**라 비운다:

- `committed_min_by_day` — 다른 목표의 **이미 승인된** 계획이 그날 쓴 집중 시간
- `roomy_busy_for_day` — 승인 블록에 휴식 여백을 덧댄 1차 배치용 busy

**따라서 이 배치는 "달력이 비어 있는 사용자" 조건이다.** 실사용자는 이미 다른 계획이
잡혀 있어 배치가 더 어렵다 — **M20·M21 은 여기서 낙관적으로 나온다.** 결과 문서에 반드시
함께 싣는다.

## N/A 는 실패가 아니다 (실험계획서 §5 「계산 불가 처리」)

| 상태 | 뜻 | M26-core 처리 |
|---|---|---|
| `True` / `False` | 조건이 적용되고 판정됐다 | AND 에 들어간다 |
| `NA` | 사용자에게 **요구된 것 자체가 없다** | 중립 — AND 에서 빠지고 케이스는 분모에 남는다 |

`NA` 를 `False` 로 두면 AND 에서 실패로 접히고 `True` 로 두면 통과로 세진다. 둘 다 틀렸다.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # `python scripts/...` 직접 실행 — pytest 는 이미 잡혀 있다
    sys.path.insert(0, str(_ROOT))

from reaction_backend.orchestrator import (  # noqa: E402
    first_plan,
    first_plan_adapter,
    goal_structuring,
    plan_scheduler,
)
from reaction_backend.schemas.interview import (  # noqa: E402
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CASES_PATH = _ROOT / "eval" / "golden_first_plan_cases.jsonl"
KST = timezone(timedelta(hours=9))


class _NotApplicable:
    """`True`/`False` 와 **구별되는** 제3의 상태. `bool()` 이 안 되게 막는다."""

    __slots__ = ()

    def __bool__(self) -> bool:  # pragma: no cover - 실수로 진리값을 쓰면 즉시 터뜨린다
        raise TypeError("N/A 를 참/거짓으로 쓰지 마라 — AND 에서 중립이어야 한다")

    def __repr__(self) -> str:
        return "N/A"


NA: Final = _NotApplicable()
"""해당 없음. **실패가 아니다.**"""

Verdict = bool | _NotApplicable

# `schedule_actions_multiday` 의 인자를 남김없이 분류한다 — 조용히 빠지는 것이 없게.
SCHEDULER_ARGS_SUPPLIED: Final = (
    "start_day",
    "horizon_day",
    "actions",
    "busy_for_day",
    "peak_windows",
    "focus_chunk_min",
    "break_min",
    "daily_focus_cap_min",
)
SCHEDULER_ARGS_STRESS_ONLY: Final = (
    # 둘 다 DB 유래(승인된 다른 계획). **기본은 비운다** — "달력이 빈 사용자" 조건이다.
    # `place(calendar=...)` 로 민감도 조건을 줄 때만 합성해 넣는다.
    "committed_min_by_day",
    "roomy_busy_for_day",
)
SCHEDULER_ARGS_OMITTED: Final = SCHEDULER_ARGS_STRESS_ONLY
"""하위 호환 별칭 — 기본 실행에서는 여전히 안 넘긴다."""


@dataclass(frozen=True)
class BusyCalendar:
    """이미 승인된 다른 계획 — **민감도 검증용 합성 달력.**

    프로덕션은 이걸 DB 에서 읽는다. 빈 달력에서만 재면 **M20·M21 이 실패를 잡는지 알 수
    없어서** 합성 조건을 만든다. 기본값(모든 필드 0)은 지금까지와 같은 "빈 달력" 이다.
    """

    busy_minutes_per_day: int = 0
    """하루에 다른 계획이 이미 쓰고 있는 분. `daily_focus_cap` 을 그만큼 잠식한다."""

    busy_start_hour: int = 9
    """그 점유가 시작되는 시각 — 활동창 앞을 막을수록 배치가 어려워진다."""

    skip_weekday: int | None = None
    """이 요일(0=월)은 통째로 비운다. 없으면 매일 점유."""

    def _applies(self, day: date) -> bool:
        return self.busy_minutes_per_day > 0 and day.weekday() != self.skip_weekday

    def busy_on(self, day: date) -> list[Any]:
        if not self._applies(day):
            return []
        start = datetime(day.year, day.month, day.day, self.busy_start_hour, 0, tzinfo=KST)
        return [
            goal_structuring.BusyBlock(
                goal_structuring.TimeInterval(
                    start, start + timedelta(minutes=self.busy_minutes_per_day)
                ),
                "fixed_schedule",
                "이미 승인된 다른 계획",
            )
        ]

    def committed_min_by_day(self, start_day: date, end_day: date) -> dict[date, int]:
        out: dict[date, int] = {}
        d = start_day
        while d <= end_day:
            if self._applies(d):
                out[d] = self.busy_minutes_per_day
            d += timedelta(days=1)
        return out


def load_decompose_cases() -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [c for c in rows if c["kind"] == "decompose"]


def build_outcome(case: dict[str, Any], *, today: date) -> InterviewOutcome:
    """`l1_7_run.build_outcome` 과 같은 복원 — 마감은 상대 오프셋이다."""
    interview, goal = case["interview"], case["interview"]["goal"]
    deadline = (today + timedelta(days=goal["deadline_offset_days"])).isoformat()
    return InterviewOutcome(
        session_id=f"l1-7-sched-{case['case_id']}",
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


def without_frequency(outcome: InterviewOutcome) -> InterviewOutcome:
    """빈도를 지운 사본 — M20 의 N/A 경로를 테스트가 밟게 한다."""
    data = outcome.model_dump()
    data["core_goals"][0]["frequency_per_week"] = None
    return InterviewOutcome.model_validate(data)


# ── 배치 창 ─────────────────────────────────────────────────────────────────


def schedule_window(
    outcome: InterviewOutcome,
    *,
    start_day: date,
    scope: Literal["horizon", "week"],
    density: str,
) -> tuple[date, date]:
    """프로덕션 `schedule_blocks` 의 배치 창 계산.

    ⚠️ 이 계산은 프로덕션에서 **노드 본문에 인라인**돼 있어 재사용할 함수가 없다.
    `_schedule_end` 는 **직접 import 해** 재구현을 줄였고, 남은 좁히기 분기만 옮겨 적었다.
    `tests/test_schedule_eval_contract.py::test_window_matches_production_for_every_golden_case`
    가 그 분기를 지킨다 — 옮겨 적은 것은 갈리기 때문이다(`_review_variables` 전례).
    """
    overdue = first_plan_adapter.is_overdue_deadline(outcome.horizon, start_day)
    end = first_plan._schedule_end(start_day, outcome.horizon, scope)
    minutes = first_plan_adapter.horizon_minute_budget(outcome, density, target_date=start_day)
    if minutes:
        days_needed = first_plan_adapter.placement_days_needed(
            minutes, first_plan_adapter.weekly_minutes(outcome, density)
        )
        density_end = start_day + timedelta(days=days_needed - 1)
        if scope == "horizon" and (not outcome.horizon or overdue):
            end = density_end
        else:
            end = max(min(end, density_end), start_day)
    return start_day, end


# ── 배치 실행 ───────────────────────────────────────────────────────────────


def place(
    action_items: list[dict[str, Any]],
    outcome: InterviewOutcome,
    *,
    today: date,
    density: str = "standard",
    calendar: BusyCalendar | None = None,
) -> tuple[list[Any], list[str], int, date]:
    """계획을 실제 스케줄러에 태운다. `(blocks, warnings, n_actions, end)`.

    ⚠️ **계획을 인자로 받는다.** 골든셋의 `decompose` 케이스에는 `plan` 이 없다 — 계획은
    LLM 이 만드는 것이고, 골든셋은 그 **입력(슬롯)** 만 갖는다. 배치 지표는 저장된 L1-7A
    실행 결과(`eval/l1_7_results.jsonl` 의 `plan`)를 태워야 **M17~M25 와 같은 계획 위에서**
    계산된다 — 그래야 M26-core 의 AND 가 성립한다.

    busy 는 기본적으로 **outcome 유래 시간정책만** — 수면·노터치다(모듈 docstring).

    `calendar` 를 주면 **이미 승인된 다른 계획이 있는 조건**을 재현한다. 프로덕션은 그것을
    DB 에서 읽지만(`committed_min_by_day`·`roomy_busy_for_day`), 여기서는 **민감도 검증**을
    위해 합성해 넣는다 — 빈 달력에서만 재면 M20·M21 이 실패를 잡는지 알 수 없다.
    """
    items = action_items
    actions = first_plan_adapter.plan_actions_from_decomposition([_ActionLike(a) for a in items])
    start_day, end = schedule_window(outcome, start_day=today, scope="horizon", density=density)
    policies = first_plan_adapter.time_policies_from_outcome(outcome)

    cal = calendar or BusyCalendar()

    def busy_for_day(day: date) -> list[Any]:
        return [*goal_structuring.time_policies_to_busy(day, policies), *cal.busy_on(day)]

    extra: dict[str, Any] = {}
    if calendar is not None:
        # 프로덕션이 DB 에서 채우는 둘. 민감도 조건에서만 합성해 넣는다.
        extra["committed_min_by_day"] = cal.committed_min_by_day(start_day, end)
        extra["roomy_busy_for_day"] = busy_for_day

    placed, warnings = plan_scheduler.schedule_actions_multiday(
        start_day=start_day,
        horizon_day=end,
        actions=actions,
        busy_for_day=busy_for_day,
        peak_windows=first_plan_adapter.peak_windows_for_plan(outcome),
        focus_chunk_min=first_plan_adapter.focus_chunk_min_from_outcome(outcome),
        break_min=first_plan_adapter.break_min_from_outcome(outcome),
        daily_focus_cap_min=first_plan_adapter.daily_cap_for_plan(
            outcome,
            density,
            longest_action_min=max((a["estimated_minutes"] or 0 for a in items), default=0),
        ),
        **extra,
    )
    return placed, warnings, len(actions), end


class _ActionLike:
    """`plan_actions_from_decomposition` 이 기대하는 최소 인터페이스.

    골든셋은 `ActionItemDraft` 를 dict 로 저장한다. 스키마를 다시 만들지 않고 필요한
    속성만 노출한다 — 필드가 늘면 여기서 `AttributeError` 로 즉시 드러난다.
    """

    __slots__ = ("category", "estimated_minutes", "first_step", "node_id", "title")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.node_id = raw["node_id"]
        self.title = raw["title"]
        self.category = raw["category"]
        self.estimated_minutes = raw["estimated_minutes"]
        self.first_step = raw.get("first_step")


# ── M20 · M21 · M22 ─────────────────────────────────────────────────────────


def m20_frequency_of(outcome: InterviewOutcome) -> int | None:
    goal = outcome.core_goals[0] if outcome.core_goals else None
    return getattr(goal, "frequency_per_week", None) if goal else None


def m20_pass(*, actual_per_week: float, requested: int) -> bool:
    """**슬랙 없음.** 프로덕션의 `0.8` UX 슬랙을 쓰지 않는다 — 실험계획서 §5.

    그 값은 사용자를 덜 성가시게 하려는 것이고, 지표는 "사용자가 말한 빈도를 맞췄나" 를
    물어야 한다. 그리고 그 `0.8` 은 1차 실행의 결함으로 이미 지목된 숫자다.
    """
    return actual_per_week >= requested


def actual_per_week(placed: list[Any], *, start_day: date) -> float:
    """주당 케이던스 — **프로덕션 `cadence_shortfall_notice` 와 같은 산식.**

    ```
    days = {블록의 날짜}
    span = max((마지막날 − start_day).days + 1, 1)
    actual_per_week = len(days) / span * 7
    ```

    ⚠️ **초판은 세 곳이 달랐고 케이던스를 실제보다 좋게 계산했다** (독립 검토 지적):

    | | 초판(틀림) | 프로덕션 = 지금 |
    |---|---|---|
    | 기간 시작 | `min(days)` — **첫날을 건너뛰면 기간이 짧아져 비율이 커진다** | `start_day` |
    | 분자 | `len(placed)` 블록 수 — **같은 날 두 블록이면 2회로 센다** | `len(days)` 날짜 수 |
    | 기간 환산 | `max(span/7, 1.0)` 주 바닥 — 3일 배치를 1주로 눌러 비율이 **작아진다** | `span/7` 그대로 |

    **날짜 수가 맞는 단위다** — `frequency_per_week`("주 3회")는 *며칠 하느냐*(케이던스)이지
    세션 개수가 아니다. 같은 날 두 번 하는 것은 "2회" 가 아니다.
    """
    days = {b.interval.start.date() for b in placed}
    span = max((max(days) - start_day).days + 1, 1)
    return len(days) / span * 7


def m20_cadence(outcome: InterviewOutcome, *, placed: list[Any], start_day: date) -> Verdict:
    """배치된 **날짜**가 사용자가 말한 주당 빈도를 채웠는가."""
    requested = m20_frequency_of(outcome)
    if not requested:
        return NA  # 빈도를 안 말했으면 물을 수 없다
    if not placed:
        return False
    return m20_pass(
        actual_per_week=actual_per_week(placed, start_day=start_day), requested=requested
    )


def m21_placement(*, n_actions: int, unplaced: int) -> Verdict:
    """놓지 못한 세션이 하나라도 있으면 실패. 놓을 것이 없으면 N/A."""
    if n_actions == 0:
        return NA
    return unplaced == 0


def m22_coverage(*, deadline: date | None, last_planned: date | None, start_day: date) -> Verdict:
    """**마감을 넘겨 일정이 잡혔는가** (DCMA #7 Negative Float).

    ⚠️ **2026-09-03 정정 — 초판은 방향이 반대였다.** 처음엔 "마감까지 덮었는가" 로 구현해
    `last_planned >= deadline` 을 통과로 봤다. 골든셋 실측에서 **11건 중 9건이 실패**로
    나왔는데, 전부 같은 모양이었다:

        window_end = 마감 − 1일,  last_planned = window_end   (9건 모두)

    즉 배치는 **창 끝까지 갔고**, 창이 하루 일찍 닫힌 것은 주당 rate 로 담을 분량이
    거기까지였기 때문이다. 이것은 `horizon_coverage_notice` 의 **갈래2**이고 그 docstring 이
    **"정상 상황"** 이라 부른다 — 유한한 목표는 마감 전에 할 일이 끝난다.
    **또 의도된 동작을 결함으로 세고 있었다**(갈래1에서 같은 실수를 한 뒤 두 번째).

    §5 가 든 근거 **DCMA #7 Negative Float** 은 "일정이 마감을 **넘긴다**" 를 뜻한다.
    일찍 끝나는 것이 아니다. 그 근거대로 다시 쓴다 — **마감 뒤에 놓인 블록이 있으면 실패.**

    N/A: 마감이 없으면 물을 수 없다.

    ⚠️ "얼마나 일찍 끝났는가" 는 여전히 정보라서 `days_short` 로 **판정 없이** 보고한다.
    """
    if deadline is None:
        return NA
    if last_planned is None:
        return NA  # 놓인 것이 없으면 넘길 수도 없다 — M21 이 그걸 잡는다
    return last_planned <= deadline


def days_short_of_deadline(*, deadline: date | None, last_planned: date | None) -> int | None:
    """마감보다 며칠 일찍 끝났는가 — **판정이 아니라 관찰치**다.

    양수면 일찍 끝난 것이고, 대부분 "분량이 거기까지" 라는 정상 상황이다(위 참조).
    """
    if deadline is None or last_planned is None:
        return None
    return (deadline - last_planned).days


def _unplaced_count(warnings: list[str]) -> int:
    return sum(1 for w in warnings if first_plan._UNPLACED_MARKER in w)


RUNS_PATH = _ROOT / "eval" / "l1_7_results.jsonl"


def load_runs() -> list[dict[str, Any]]:
    """저장된 L1-7A 실행 결과 — 각 행이 `plan` 전문을 갖는다 (비결정적이라 gitignore)."""
    if not RUNS_PATH.exists():
        return []
    return [
        json.loads(line)
        for line in RUNS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def evaluate_case(
    case: dict[str, Any], *, today: date, action_items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """한 케이스의 배치 지표. `action_items` 를 안 주면 **룰만으로** 최소 계획을 만든다.

    실측에는 저장된 LLM 계획을 넘긴다. 인자를 비우는 것은 **테스트·스모크용**이다 —
    LLM 없이도 배치 경로가 끝까지 도는지 확인할 수 있어야 한다.
    """
    outcome = build_outcome(case, today=today)
    if action_items is None:
        action_items = rule_only_plan(outcome, today=today)
    placed, warnings, n_actions, end = place(action_items, outcome, today=today)
    days = sorted({b.interval.start.date() for b in placed})
    deadline = date.fromisoformat(outcome.horizon) if outcome.horizon else None
    return {
        "case_id": case["case_id"],
        "block": case["block"],
        "n_actions": n_actions,
        "placed_blocks": len(placed),
        "unplaced": _unplaced_count(warnings),
        "last_planned": days[-1].isoformat() if days else None,
        "window_end": end.isoformat(),
        "m20": m20_cadence(outcome, placed=placed, start_day=today),
        "m21": m21_placement(n_actions=n_actions, unplaced=_unplaced_count(warnings)),
        "m22": m22_coverage(
            deadline=deadline, last_planned=days[-1] if days else None, start_day=today
        ),
        # 판정이 아니라 **관찰치** — 유한한 목표가 마감 전에 끝나는 것은 정상 상황이다.
        "days_short": days_short_of_deadline(
            deadline=deadline, last_planned=days[-1] if days else None
        ),
    }


def rule_only_plan(outcome: InterviewOutcome, *, today: date) -> list[dict[str, Any]]:
    """LLM 없이 만드는 최소 계획 — 룰이 정한 세션 수 × 세션 길이.

    ⚠️ **실측용이 아니다.** 배치 경로가 도는지 확인하는 스모크·테스트 재료다. 실제 M20~M22
    는 저장된 LLM 계획(`load_runs`)을 태워서 낸다.
    """
    n = first_plan_adapter.llm_session_target(outcome, "standard", target_date=today) or 1
    minutes = first_plan_adapter.planned_session_min_for(outcome)
    goal = outcome.core_goals[0]
    return [
        {
            "node_id": f"l{i + 1}",
            "title": f"{goal.title} {i + 1}회차",
            "category": goal.category,
            "estimated_minutes": minutes,
            "first_step": "시작하기",
        }
        for i in range(n)
    ]


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """지표별 통과/실패/N-A 를 **적용 사례 수와 함께** 낸다 (실험계획서 §5 규칙)."""
    out: dict[str, Any] = {}
    for metric in ("m20", "m21", "m22"):
        vals = [r[metric] for r in rows]
        na = sum(1 for v in vals if isinstance(v, _NotApplicable))
        applicable = [v for v in vals if not isinstance(v, _NotApplicable)]
        out[metric] = {
            "pass": sum(1 for v in applicable if v),
            "fail": sum(1 for v in applicable if not v),
            "applicable": len(applicable),
            "na": na,
        }
    return out


def main() -> None:
    today = date.today()
    runs = {(r["case_id"], r.get("repeat", 0)): r for r in load_runs()}
    cases = load_decompose_cases()
    if runs:
        rows = [
            evaluate_case(
                c, today=today, action_items=runs[(c["case_id"], 0)]["plan"]["action_items"]
            )
            for c in cases
            if (c["case_id"], 0) in runs
        ]
        src = f"저장된 L1-7A 실행 (repeat 0) {len(rows)}건"
    else:
        rows = [evaluate_case(c, today=today) for c in cases]
        src = f"⚠️ 룰 전용 최소 계획 {len(rows)}건 — 실측 아님 (원자료가 없다)"
    s = summarize_rows(rows)
    print(f"\n{'=' * 74}\nL1-7A 배치 경로 — M20·M21·M22 (eval 전용 · 프로덕션 무변경)")
    print(f"입력: {src} · 기준일 {today}")
    print("\n⚠️ DB 유래 입력을 비웠다 — **달력이 비어 있는 사용자 조건**이다.")
    print("   실사용자는 이미 잡힌 일정이 있어 더 어렵다. M20·M21 은 여기서 낙관적이다.\n")
    names = {
        "m20": "M20 cadence_compliance (슬랙 없음)",
        "m21": "M21 placement_rate",
        "m22": "M22 negative float — 마감을 넘겨 잡혔는가",
    }
    for metric, label in names.items():
        v = s[metric]
        rate = f"{v['pass'] / v['applicable']:.3f}" if v["applicable"] else "—"
        print(f"── {label}")
        print(
            f"   통과 {rate} ({v['pass']}/{v['applicable']})   **적용 {v['applicable']}건 · N/A {v['na']}건**"
        )
    print("\n⚠️ N/A 는 실패가 아니다 — M26-core 에서 중립이고 케이스는 분모에 남는다.")
    print(
        "⚠️ **M22 는 이 조건에서 거의 항상 참인 안전 불변식이다.** 배치 창 자체가 마감 이하로\n"
        "   잘리고 스케줄러는 창 밖에 배치하지 않는다. '위반 0' 으로 남기되\n"
        "   **'마감 제약을 잘 지켰다' 는 성과 주장에는 쓰지 않는다.**\n"
        "   변별력 검증은 M22 가 아니라 **마감 과부하·바쁜 달력 조건의 M20/M21** 로 한다."
    )
    shorts = [r["days_short"] for r in rows if r.get("days_short") is not None]
    if shorts:
        import statistics as _st

        print(
            f"\n[관찰] 마감보다 일찍 끝난 일수 — 중앙 {_st.median(shorts):.0f}일 "
            f"(최소 {min(shorts)} · 최대 {max(shorts)})"
        )
        print("   ⚠️ 판정이 아니다. 유한한 목표가 마감 전에 끝나는 것은 정상 상황이다.")
    fails = [r["case_id"] for r in rows if r["m21"] is False]
    if fails:
        print(f"\n배치 실패 케이스: {', '.join(fails)}")
    print("=" * 74)


if __name__ == "__main__":
    main()
