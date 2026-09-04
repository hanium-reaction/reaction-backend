"""M20·M21 민감도 검증 — **지표가 실제로 실패를 잡는가.**

`scripts/l1_7_schedule_eval.py` 는 DB 유래 입력을 비워 **"달력이 비어 있는 사용자"** 조건에서
배치한다. 거기서 M20 32/34 · M21 34/34 가 나왔는데, **그 수치만으로는 지표가 실패를 잡는지
알 수 없다.** 항상 통과하는 지표와 구별되지 않기 때문이다.

## 이 스크립트가 하는 것

**같은 계획을 두 조건에서 배치해 판정이 갈리는지 본다.**

```
기준선   빈 달력                     (지금까지의 조건)
스트레스 이미 승인된 계획이 하루를 잠식  (BusyCalendar)
```

판정이 **갈리면** 그 지표는 그 조건에 민감하다. **안 갈리면** 둘 중 하나다 —
지표가 둔감하거나, 그 조건이 실제로 문제를 안 만들거나. 어느 쪽인지는 배치 결과를 보고
따로 판단해야 한다. **"안 갈렸다 = 계획 품질이 좋다" 로 읽으면 안 된다.**

## ⚠️ 이것은 성능 측정이 아니다

시나리오의 달력은 **내가 합성한 것**이고 실사용 분포가 아니다. 여기서 나오는 실패율은
"실사용자가 이만큼 실패한다" 가 아니라 **"이 조건을 주면 지표가 반응한다"** 는 뜻이다.

## 실행

    uv run python scripts/l1_7_schedule_stress.py
    uv run python scripts/l1_7_schedule_stress.py --today 2026-09-03   # 문서 수치 재현

⚠️ `skip_weekday` 시나리오는 **요일 정렬에 의존**한다 — 문서에 실은 수치를 재현하려면
`--today` 로 같은 날짜를 줘야 한다.

LLM 을 부르지 않는다 — 저장된 L1-7A 계획(`eval/l1_7_results.jsonl`)을 태울 뿐이다.
원자료가 없으면 룰 전용 최소 계획으로 돈다(구성 확인용).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # `python scripts/...` 직접 실행
    sys.path.insert(0, str(_ROOT))

from scripts.l1_7_schedule_eval import (  # noqa: E402
    BusyCalendar,
    _NotApplicable,
    _unplaced_count,
    build_outcome,
    days_short_of_deadline,
    load_decompose_cases,
    load_runs,
    m20_cadence,
    m21_placement,
    m22_coverage,
    place,
    rule_only_plan,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class Scenario:
    name: str
    calendar: BusyCalendar
    why: str


# ⚠️ 강도를 **여러 단계**로 둔다. 한 조건에서만 재면 "안 갈렸다" 가 지표 둔감인지
# 조건이 약해서인지 구별되지 않는다.
SCENARIOS: Final = (
    Scenario(
        "baseline",
        BusyCalendar(),
        "빈 달력 — 지금까지의 조건. 다른 시나리오의 대조군이다",
    ),
    Scenario(
        "busy-light",
        BusyCalendar(busy_minutes_per_day=120, busy_start_hour=9),
        "하루 2시간이 이미 잡혀 있다 — 활동창 앞을 막는다",
    ),
    Scenario(
        "busy-heavy",
        BusyCalendar(busy_minutes_per_day=360, busy_start_hour=9),
        "하루 6시간 — 활동창 대부분이 막힌다",
    ),
    Scenario(
        "busy-extreme",
        BusyCalendar(busy_minutes_per_day=780, busy_start_hour=9),
        "하루 13시간 — 활동창이 거의 안 남는다. **M21 이 발화할 수 있는지** 보는 조건이다",
    ),
    Scenario(
        "busy-except-sunday",
        BusyCalendar(busy_minutes_per_day=480, busy_start_hour=9, skip_weekday=6),
        "일요일만 비어 있다 — 케이던스가 무너지는지 본다(M20 의 표적)",
    ),
)


def _verdicts(
    case: dict[str, Any], action_items: list[dict[str, Any]], *, today: date, cal: BusyCalendar
) -> dict[str, Any]:
    outcome = build_outcome(case, today=today)
    placed, warnings, n_actions, _ = place(action_items, outcome, today=today, calendar=cal)
    days = sorted({b.interval.start.date() for b in placed})
    deadline = date.fromisoformat(outcome.horizon) if outcome.horizon else None
    unplaced = _unplaced_count(warnings)
    return {
        "m20": m20_cadence(outcome, placed=placed, start_day=today),
        "m21": m21_placement(n_actions=n_actions, unplaced=unplaced),
        "m22": m22_coverage(
            deadline=deadline, last_planned=days[-1] if days else None, start_day=today
        ),
        "placed": len(placed),
        "unplaced": unplaced,
        "days": len(days),
        "days_short": days_short_of_deadline(
            deadline=deadline, last_planned=days[-1] if days else None
        ),
    }


def _fmt(v: Any) -> str:
    return "N/A" if isinstance(v, _NotApplicable) else ("통과" if v else "**실패**")


def run(today: date) -> dict[str, Any]:
    runs = {(r["case_id"], r.get("repeat", 0)): r for r in load_runs()}
    cases = load_decompose_cases()
    plans: dict[str, list[dict[str, Any]]] = {}
    for c in cases:
        k = (c["case_id"], 0)
        if k in runs and "plan" in runs[k]:
            plans[c["case_id"]] = runs[k]["plan"]["action_items"]
    src = f"저장된 L1-7A 계획 {len(plans)}건"
    if not plans:
        for c in cases:
            plans[c["case_id"]] = rule_only_plan(build_outcome(c, today=today), today=today)
        src = f"⚠️ 룰 전용 최소 계획 {len(plans)}건 — 구성 확인용"

    out: dict[str, Any] = {"source": src, "by_scenario": {}, "cases": len(plans)}
    per_case: dict[str, dict[str, Any]] = {}
    for sc in SCENARIOS:
        # [pass, applicable, na] — **실패 수와 N/A 수를 따로 센다.**
        # 통과 수만 보면 N/A 가 늘어도 줄어드는데, 그건 지표가 반응한 것이 아니라
        # **잴 대상이 사라진 것**이다(busy-extreme 에서 M22 가 정확히 그랬다).
        agg = {"m20": [0, 0, 0], "m21": [0, 0, 0], "m22": [0, 0, 0]}
        for c in cases:
            if c["case_id"] not in plans:
                continue
            v = _verdicts(c, plans[c["case_id"]], today=today, cal=sc.calendar)
            per_case.setdefault(c["case_id"], {})[sc.name] = v
            for m in ("m20", "m21", "m22"):
                if isinstance(v[m], _NotApplicable):
                    agg[m][2] += 1
                else:
                    agg[m][1] += 1
                    agg[m][0] += 1 if v[m] else 0
        out["by_scenario"][sc.name] = agg
    out["per_case"] = per_case
    return out


def main() -> None:
    # ⚠️ 기준일을 인자로 받는다 — `skip_weekday` 시나리오는 **요일 정렬에 의존**하므로
    # 문서에 실은 수치를 재현하려면 같은 날짜를 줘야 한다. (실측: 2026-09-03 목요일과
    # 09-04 금요일이 같은 값을 냈지만 그건 보장이 아니라 운이다.)
    ap = argparse.ArgumentParser(description="M20·M21 민감도 — 지표가 실패를 잡는가")
    ap.add_argument("--today", default=None, help="기준일 YYYY-MM-DD (기본: 오늘)")
    args = ap.parse_args()
    today = date.fromisoformat(args.today) if args.today else date.today()
    r = run(today)
    print(f"\n{'=' * 76}\nM20·M21 민감도 — **지표가 실패를 잡는가**")
    print(f"입력: {r['source']} · 케이스 {r['cases']}건 · 기준일 {today}")
    print("\n⚠️ 이것은 성능 측정이 아니다. 달력은 **합성**이고, 여기 수치는 '실사용자가")
    print("   이만큼 실패한다' 가 아니라 **'이 조건을 주면 지표가 반응한다'** 는 뜻이다.\n")

    print(f"   {'시나리오':<20}{'M20':>17}{'M21':>17}{'M22':>17}")
    for sc in SCENARIOS:
        a = r["by_scenario"][sc.name]
        cells = "".join(
            f"{(f'{a[m][0]}/{a[m][1]}' + (f' N/A{a[m][2]}' if a[m][2] else '')):>17}"
            for m in ("m20", "m21", "m22")
        )
        print(f"   {sc.name:<20}{cells}")
    for sc in SCENARIOS:
        print(f"     {sc.name:<20} {sc.why}")

    base = r["by_scenario"]["baseline"]
    print("\n── 민감도 판정 (기준선 대비)")
    print("   ⚠️ **통과 수가 아니라 실패 수**로 비교한다 — 통과 수는 N/A 가 늘어도 줄어드는데")
    print("      그건 지표가 반응한 것이 아니라 **잴 대상이 사라진 것**이다.")
    for m in ("m20", "m21", "m22"):
        base_fail = base[m][1] - base[m][0]
        moved, na_moved = [], []
        for sc in SCENARIOS:
            if sc.name == "baseline":
                continue
            a = r["by_scenario"][sc.name]
            if a[m][1] - a[m][0] != base_fail:
                moved.append(sc.name)
            if a[m][2] != base[m][2]:
                na_moved.append(f"{sc.name} {base[m][2]}->{a[m][2]}")
        if moved:
            print(f"   {m.upper()}: **반응함** — {', '.join(moved)} 에서 **실패 수**가 달라졌다")
        else:
            print(
                f"   {m.upper()}: 어떤 조건에서도 **실패 수가 그대로** — 둔감하거나 조건이 약하다"
            )
        if na_moved:
            print(f"          (N/A 변화: {', '.join(na_moved)} — **판정 변화가 아니다**)")

    print(
        "\n⚠️ '안 갈렸다' 를 **'계획 품질이 좋다' 로 읽으면 안 된다.** 지표가 둔감한 것인지\n"
        "   조건이 실제로 문제를 안 만드는 것인지는 배치 결과를 보고 따로 판단해야 한다."
    )
    print("=" * 76)


if __name__ == "__main__":
    main()
