"""M20·M21·M22 의 **입력·출력·N/A 조건**을 구현보다 먼저 고정한다.

이 셋은 배치(스케줄러) 결과가 있어야 계산되고, 그래서 L1-7A 하네스가 지금까지 못 냈다.
`scripts/l1_7_schedule_eval.py` 가 그 경로를 eval 전용으로 만든다 — **프로덕션 동작은
바꾸지 않는다.**

## 왜 테스트를 먼저 쓰나

지표를 구현한 뒤에 계약을 적으면, **구현이 우연히 하는 일**이 계약이 된다. 이 레포는 그
사고를 이미 두 번 겪었다 — `review_feedback` 을 빈 문자열로 넘겨 34호출을 버렸고
(`l1-7-results.md` §5), M18 을 a/b 로 쪼개면서 회차별 표만 옛 키를 읽어 새 실행에서 크래시했다.
둘 다 **"코드가 무엇을 넘기는가" 를 고정한 테스트가 없어서** 생겼다.

## 이 파일이 고정하는 것

1. **N/A 와 실패의 구분** — 실험계획서 §5 「계산 불가 처리」. N/A 는 중립이고 케이스는
   분모에 남는다. 실패로 세면 부당하고, 케이스를 빼면 지표가 소수 케이스만의 것이 된다.
2. **입력 계약** — 하네스가 스케줄러에 넘기는 인자가 프로덕션과 같은 출처인가.
3. **프로덕션 격리** — eval 경로가 `src/` 를 건드리지 않는가.
"""

from __future__ import annotations

import inspect
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from scripts import l1_7_schedule_eval as sched

from reaction_backend.orchestrator import first_plan, first_plan_adapter, plan_scheduler

_ROOT = Path(__file__).resolve().parent.parent


def _case(case_id: str) -> dict[str, Any]:
    for row in sched.load_decompose_cases():
        if row["case_id"] == case_id:
            return row
    raise AssertionError(f"골든셋에 {case_id} 가 없다")


# ── 1. N/A 조건 — 실패가 아니다 ─────────────────────────────────────────────


def test_na_is_a_distinct_state_from_pass_and_fail() -> None:
    """세 상태가 서로 구별되는 값이어야 한다.

    N/A 를 `False` 로 표현하면 AND 에서 실패로 접히고, `True` 로 표현하면 통과로 세진다.
    둘 다 틀렸다 — **중립**이어야 한다.
    """
    assert sched.NA is not True
    assert sched.NA is not False
    assert sched.NA != 0 and sched.NA != 1


def test_m20_is_na_when_the_user_never_stated_a_frequency() -> None:
    """빈도를 안 말했으면 "빈도를 지켰나" 는 물을 수 없다."""
    outcome = sched.build_outcome(_case("normal-toeic-near"), today=date(2026, 9, 3))
    assert sched.m20_frequency_of(outcome) is not None  # 이 케이스는 빈도가 있다
    stripped = sched.without_frequency(outcome)
    assert sched.m20_frequency_of(stripped) is None
    assert sched.m20_cadence(stripped, placed=[], start_day=date(2026, 9, 3)) is sched.NA


def test_m22_measures_negative_float_not_early_finish() -> None:
    """M22 는 **마감을 넘겨 잡혔는가**다 — 일찍 끝난 것은 실패가 아니다.

    초판은 방향이 반대였다("마감까지 덮었는가"). 골든셋 실측에서 **11건 중 9건이 실패**로
    나왔는데 전부 같은 모양이었다 — `window_end = 마감 - 1일`, 배치는 창 끝까지 갔다.
    즉 **분량이 거기까지**였고, `horizon_coverage_notice` docstring 이 그 갈래를
    **"정상 상황"** 이라 부른다. §5 가 든 근거 **DCMA #7 Negative Float** 은 일정이 마감을
    **넘긴다**는 뜻이다.
    """
    start = date(2026, 9, 3)
    deadline = start + timedelta(days=10)
    assert sched.m22_coverage(deadline=deadline, last_planned=start, start_day=start) is True
    assert sched.m22_coverage(deadline=deadline, last_planned=deadline, start_day=start) is True
    over = deadline + timedelta(days=1)
    assert sched.m22_coverage(deadline=deadline, last_planned=over, start_day=start) is False


def test_m22_is_na_without_a_deadline_or_placement() -> None:
    start = date(2026, 9, 3)
    assert sched.m22_coverage(deadline=None, last_planned=start, start_day=start) is sched.NA
    assert sched.m22_coverage(deadline=start, last_planned=None, start_day=start) is sched.NA


def test_early_finish_is_an_observation_not_a_verdict() -> None:
    """ "얼마나 일찍 끝났는가" 는 숫자로 남기되 **판정에 넣지 않는다.**"""
    start = date(2026, 9, 3)
    deadline = start + timedelta(days=10)
    assert sched.days_short_of_deadline(deadline=deadline, last_planned=start) == 10
    assert sched.days_short_of_deadline(deadline=deadline, last_planned=deadline) == 0
    assert sched.days_short_of_deadline(deadline=None, last_planned=start) is None
    row = sched.evaluate_case(_case("normal-toeic-near"), today=date(2026, 9, 3))
    assert "days_short" in row


def test_m21_is_never_na_when_there_was_something_to_place() -> None:
    """배치할 카드가 있었다면 "놓았나" 는 항상 물을 수 있다 — N/A 가 없다."""
    assert sched.m21_placement(n_actions=3, unplaced=0) is True
    assert sched.m21_placement(n_actions=3, unplaced=1) is False
    # 놓을 것이 없으면 그때만 N/A.
    assert sched.m21_placement(n_actions=0, unplaced=0) is sched.NA


# ── 2. 통과 조건 — 슬랙 없음 (실험계획서 §5) ────────────────────────────────


def test_m20_uses_no_ux_slack() -> None:
    """프로덕션의 `_CADENCE_OK_RATIO = 0.8` 을 **일부러 안 쓴다.**

    그 값은 사용자를 덜 성가시게 하려는 UX 값이고, 지표는 "사용자가 말한 빈도를 맞췄나" 를
    물어야 한다. 그리고 그 `0.8` 은 `l1-7-results.md` §5 가 1차 실행의 결함 #2 로 직접
    지목한 숫자다 — 프로덕션 함수를 경유해 뒷문으로 들여오지 않는다.
    """
    src = (_ROOT / "scripts" / "l1_7_schedule_eval.py").read_text(encoding="utf-8")
    assert "cadence_shortfall_notice(" not in src, "UX 슬랙이 붙은 판정을 호출하고 있다"
    assert "_CADENCE_OK_RATIO" not in src
    # 정확히 같아도 통과, 하나라도 모자라면 실패.
    assert sched.m20_pass(actual_per_week=3.0, requested=3) is True
    assert sched.m20_pass(actual_per_week=2.9, requested=3) is False
    assert sched.m20_pass(actual_per_week=4.0, requested=3) is True


def test_m22_does_not_reuse_the_production_notice() -> None:
    """`horizon_coverage_notice` 를 **호출하지 않는다.**

    그 함수는 발화 자체가 결함이 아니다 — docstring 이 갈래1을 *"의도된 설계"*,
    갈래2를 *"정상 상황"* 이라 부르고 사용자 문구가 *"빠뜨린 게 아니에요"* 다.
    그 발화를 판정으로 쓰면 의도된 동작을 결함으로 세게 된다(초판이 그랬다).
    그리고 그 함수의 3일 슬랙(`_HORIZON_COVERED_SLACK_DAYS`)도 상속하지 않는다.
    """
    src = (_ROOT / "scripts" / "l1_7_schedule_eval.py").read_text(encoding="utf-8")
    # 이름 **언급**은 정당하다 — 왜 안 쓰는지 적으려면 불러야 한다. 막을 것은 **호출**이다.
    assert "horizon_coverage_notice(" not in src, "슬랙이 붙은 프로덕션 판정을 호출하고 있다"
    assert "_HORIZON_COVERED_SLACK_DAYS" not in src


# ── 3. 입력 계약 — 프로덕션과 같은 출처에서 오는가 ──────────────────────────


def test_scheduler_inputs_all_come_from_production_helpers() -> None:
    """하네스가 스케줄러에 넘기는 인자를 **자기가 계산하지 않는다.**

    프로덕션 `schedule_blocks` 가 쓰는 어댑터 함수를 그대로 부른다. 하나라도 하네스가
    다시 계산하면 그 순간 두 경로가 갈린다.
    """
    src = (_ROOT / "scripts" / "l1_7_schedule_eval.py").read_text(encoding="utf-8")
    for helper in (
        "time_policies_from_outcome",
        "peak_windows_for_plan",
        "focus_chunk_min_from_outcome",
        "break_min_from_outcome",
        "daily_cap_for_plan",
        "plan_actions_from_decomposition",
        "placement_days_needed",
        "weekly_minutes",
    ):
        assert helper in src, f"프로덕션 헬퍼 `{helper}` 를 안 쓰고 있다"


def test_harness_covers_every_required_scheduler_argument() -> None:
    """`schedule_actions_multiday` 가 요구하는 인자를 하네스가 전부 채우는가.

    빠뜨리면 TypeError 로 바로 터지지만, **DB 의존 인자를 조용히 생략**하면 프로덕션과
    다른 조건에서 배치가 돌아간다. 어느 것을 일부러 뺐는지 문서화를 강제한다.
    """
    params = set(inspect.signature(plan_scheduler.schedule_actions_multiday).parameters)
    supplied = set(sched.SCHEDULER_ARGS_SUPPLIED)
    omitted = set(sched.SCHEDULER_ARGS_OMITTED)
    assert supplied | omitted == params, f"미분류 인자: {params - supplied - omitted}"
    assert not (supplied & omitted)
    # 뺀 것은 전부 DB 유래여야 한다 — 사유가 코드에 적혀 있어야 한다.
    src = (_ROOT / "scripts" / "l1_7_schedule_eval.py").read_text(encoding="utf-8")
    for arg in omitted:
        assert arg in src


def test_window_matches_production_for_every_golden_case() -> None:
    """배치 창이 프로덕션 `schedule_blocks` 의 계산과 같은가.

    ⚠️ 이 계산은 프로덕션에서 노드 본문에 인라인돼 있어(그래서 재사용이 안 된다) 하네스가
    옮겨 적었다. **옮겨 적은 것은 갈린다** — `_review_variables` 가 정확히 그렇게 갈려
    34호출을 버렸다. 여기서는 `_schedule_end` 를 **직접 import 해** 재구현을 줄이고,
    남은 좁히기 분기만 이 테스트가 지킨다.
    """
    today = date(2026, 9, 3)
    for case in sched.load_decompose_cases():
        outcome = sched.build_outcome(case, today=today)
        start, end = sched.schedule_window(
            outcome, start_day=today, scope="horizon", density="standard"
        )
        assert start == today
        assert end >= start, f"{case['case_id']}: 배치 창이 뒤집혔다"
        # 마감이 있으면 창이 마감을 넘지 않는다(지난 마감 제외).
        if outcome.horizon:
            deadline = date.fromisoformat(outcome.horizon)
            if deadline >= today:
                assert end <= deadline, f"{case['case_id']}: 창이 마감을 넘었다"


def test_schedule_end_is_imported_not_reimplemented() -> None:
    src = (_ROOT / "scripts" / "l1_7_schedule_eval.py").read_text(encoding="utf-8")
    assert "_schedule_end" in src
    assert hasattr(first_plan, "_schedule_end")


# ── 4. 프로덕션 격리 ────────────────────────────────────────────────────────


def test_eval_path_does_not_touch_production() -> None:
    """`src/` 어디에서도 이 eval 모듈을 참조하지 않는다."""
    offenders = [
        str(p.relative_to(_ROOT))
        for p in (_ROOT / "src").rglob("*.py")
        if "l1_7_schedule_eval" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"프로덕션이 eval 모듈을 참조한다: {offenders}"


def test_harness_never_opens_a_db_session() -> None:
    """DB 없이 도는가 — 로컬에 PostgreSQL 이 없어도 이 경로는 끝까지 간다."""
    src = (_ROOT / "scripts" / "l1_7_schedule_eval.py").read_text(encoding="utf-8")
    for banned in ("_db_time_policies", "_fixed_schedules", "AsyncSession", "get_session"):
        assert banned not in src, f"DB 의존 `{banned}` 이 eval 경로에 들어왔다"


# ── 5. 실제 배치가 돈다 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("case_id", ["normal-toeic-near", "busy-thin", "milestone-visa"])
def test_placement_actually_runs_without_a_db(case_id: str) -> None:
    """세 블록을 대표로 — 배치가 실제로 결과를 낸다."""
    result = sched.evaluate_case(_case(case_id), today=date(2026, 9, 3))
    assert result["n_actions"] > 0
    assert result["m21"] in (True, False)
    assert result["m20"] in (True, False, sched.NA)
    assert result["m22"] in (True, False, sched.NA)
    assert isinstance(result["placed_blocks"], int)


def test_na_counts_are_reported_per_metric() -> None:
    """보고서에 **적용 사례 수**를 병기하라는 §5 규칙 — 집계가 그걸 낸다."""
    today = date(2026, 9, 3)
    rows = [sched.evaluate_case(c, today=today) for c in sched.load_decompose_cases()[:6]]
    summary = sched.summarize_rows(rows)
    for metric in ("m20", "m21", "m22"):
        assert "applicable" in summary[metric]
        assert "na" in summary[metric]
        assert summary[metric]["applicable"] + summary[metric]["na"] == len(rows)


# ── 6. M20 parity — 프로덕션과 **같은 산식**인가 ────────────────────────────
#
# ⚠️ 독립 검토(2026-09-03)가 초판에서 **세 곳의 불일치**를 찾았다. 전부 케이던스를
# 실제보다 좋게 계산하는 방향이었다 — 기간 시작을 `min(days)` 로, 분자를 블록 수로,
# 기간을 주 단위로 바닥 처리했다. 32/34 라는 수치는 그래서 폐기했다.


def _block(day: date, hour: int = 10) -> Any:
    from datetime import datetime, timedelta, timezone

    from reaction_backend.orchestrator.goal_structuring import DraftScheduledBlock, TimeInterval

    kst = timezone(timedelta(hours=9))
    start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=kst)
    return DraftScheduledBlock(
        interval=TimeInterval(start, start + timedelta(minutes=50)),
        origin="goal",
        origin_id=None,
        title="t",
        category="career",
    )


def test_m20_span_starts_at_start_day_not_first_placement() -> None:
    """**첫날 미배치 회귀** — 시작일을 건너뛰어도 기간은 `start_day` 부터 센다.

    `min(days)` 부터 세면 배치가 늦게 시작할수록 기간이 짧아져 **케이던스가 좋아 보인다.**
    """
    start = date(2026, 9, 3)
    # 3일 뒤부터 3일 연속 배치 — 실제로는 6일 중 3일이다.
    placed = [_block(start + timedelta(days=d)) for d in (3, 4, 5)]
    rate = sched.actual_per_week(placed, start_day=start)
    assert rate == pytest.approx(3 / 6 * 7)  # 3.5
    # min(days) 부터 셌다면 3/3*7 = 7.0 이 됐을 것이다.
    assert rate < 7.0


def test_m20_counts_days_not_blocks() -> None:
    """**같은 날 복수 블록 회귀** — 하루에 두 번 해도 "2회" 가 아니다.

    `frequency_per_week`("주 3회")는 *며칠 하느냐*이지 세션 개수가 아니다.
    """
    start = date(2026, 9, 3)
    two_on_one_day = [_block(start, 10), _block(start, 15)]
    one_day = [_block(start, 10)]
    assert sched.actual_per_week(two_on_one_day, start_day=start) == sched.actual_per_week(
        one_day, start_day=start
    )


def test_m20_matches_production_formula_exactly() -> None:
    """프로덕션 `cadence_shortfall_notice` 와 **같은 span·days** 를 쓰는가.

    그 함수의 문구가 `"{span}일 중 {len(days)}일에 잡혔어요"` 를 그대로 찍으므로,
    거기서 두 수를 뽑아 하네스 계산과 대조한다 — 옮겨 적은 산식이 갈리는지 **실제로**
    확인하는 유일한 방법이다.
    """
    start = date(2026, 9, 3)
    case = _case("normal-toeic-near")
    outcome = sched.build_outcome(case, today=start)
    # 케이던스를 확실히 못 지키는 배치(=문구가 나오는 조건): 10일 중 2일.
    placed = [_block(start + timedelta(days=d)) for d in (1, 9)]
    notice = first_plan_adapter.cadence_shortfall_notice(
        outcome, placed, start_day=start, committed_min_by_day={}
    )
    assert notice is not None, "이 배치는 프로덕션이 케이던스 부족으로 봐야 한다"
    span, days = (int(x) for x in re.findall(r"(\d+)일 중 (\d+)일", notice)[0])
    assert (span, days) == (10, 2)
    assert sched.actual_per_week(placed, start_day=start) == pytest.approx(days / span * 7)


def test_m20_threshold_is_stricter_than_production() -> None:
    """같은 산식이되 **0.8 슬랙은 안 쓴다** — §5 의 등록된 기준.

    프로덕션은 `freq * 0.8` 이상이면 통지하지 않는다. 지표는 그 여유를 쓰지 않는다.
    """
    start = date(2026, 9, 3)
    case = _case("normal-toeic-near")
    outcome = sched.build_outcome(case, today=start)
    freq = sched.m20_frequency_of(outcome)
    assert freq is not None
    # freq 의 80~99% 만 채운 배치 — 프로덕션은 통과시키지만 지표는 실패로 본다.
    slack_rate = freq * 0.9
    assert sched.m20_pass(actual_per_week=slack_rate, requested=freq) is False
    assert slack_rate >= freq * 0.8  # 프로덕션 기준으로는 통과 구간이다
