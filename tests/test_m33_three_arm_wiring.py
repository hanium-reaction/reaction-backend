"""M33 3-arm 배선 — **프로덕션 노드를 부르는가, 옮겨 적었는가.**

⚠️ 이 레포는 프롬프트·변수 계약을 **옮겨 적었다가** 34호출을 통째로 버린 적이 있다
(`l1-7-results.md` §5). 그래서 이 파일이 지키는 첫 번째는 **복사하지 않았다는 것**이다.

두 번째는 **B/C arm 이 실제로 다른 피드백을 보낸다**는 것이다. 스모크에서 세 케이스가
모두 승인이 나면 그 경로가 한 번도 안 밟히므로, 여기서 LLM 없이 직접 확인한다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from scripts import l1_7_run as H
from scripts import l1_7b_m33_run as M

from reaction_backend.orchestrator import first_plan as FP
from reaction_backend.schemas.planning import PlanReview

_ROOT = Path(__file__).resolve().parent.parent
_TODAY = date(2026, 9, 3)


def _state() -> Any:
    case = next(c for c in H.load_cases() if c["case_id"] == "normal-toeic-near")
    return FP.initial_state(
        user_id=uuid.uuid4(),
        outcome=H.build_outcome(case, today=_TODAY),
        target_date=_TODAY.isoformat(),
    )


# ── 1. 옮겨 적지 않았다 ─────────────────────────────────────────────────────


def test_harness_calls_production_nodes_not_copies() -> None:
    """분해·검토·재분해 판정을 **프로덕션 함수로** 부른다."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    for fn in (
        "FP.validate_inputs",
        "FP.decompose_goal",
        "FP.review_plan",
        "FP.should_replan",
        "FP._replan_feedback",
        "FP.initial_state",
    ):
        assert fn in src, f"프로덕션 함수 `{fn}` 을 안 쓰고 있다"


def test_harness_does_not_copy_prompt_or_variables() -> None:
    """프롬프트 id·변수 계약을 하네스가 **다시 쓰지 않는다.**"""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    # 이름 **언급**은 정당하다(왜 안 쓰는지 적으려면 불러야 한다). 막을 것은 **사용**이다.
    for banned in ("prompt_id=", "aiClient.run(", '"prompt_vars"', "variables="):
        assert banned not in src, f"`{banned}` 를 하네스가 다시 들고 있다 — 갈릴 자리다"


def test_harness_never_opens_a_db_session() -> None:
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    for banned in ("AsyncSession", "get_session", "_db_time_policies"):
        assert banned not in src


def test_production_source_is_untouched() -> None:
    """`src/` 를 안 바꾼다 — 이 배선의 전제다."""
    assert hasattr(FP, "initial_state") and hasattr(FP, "_replan_feedback")


# ── 2. B/C arm 이 **실제로 다른 피드백**을 보낸다 ───────────────────────────


def test_c_arm_sends_the_empty_signal_via_production_helper() -> None:
    """C arm 은 `review.feedback` 을 비워서 만든다 — **프로덕션 함수가 빈 신호를 낸다.**

    프롬프트를 따로 쓰지 않는 것이 이 설계의 핵심이다.
    """
    st = _state()
    st["review"] = PlanReview(approved=False, feedback=["세션이 너무 짧아요", "순서를 바꿔보면"])
    b_sent = FP._replan_feedback(st)

    c = dict(st)
    c["review"] = PlanReview(approved=False, feedback=[])
    c_sent = FP._replan_feedback(c)

    assert "세션이 너무 짧아요" in b_sent, "B 가 피드백 전문을 안 보낸다"
    assert c_sent == "(첫 분해 — 이전 피드백 없음)"
    assert b_sent != c_sent, "B·C 가 같은 것을 보내면 M34 가 성립하지 않는다"


def test_c_arm_keeps_the_rejection_verdict() -> None:
    """C 는 **피드백만** 비운다 — 반려 판정 자체는 B 와 같아야 한다."""
    st = _state()
    review = PlanReview(approved=False, feedback=["x"])
    st["review"] = review
    st["replan_count"] = 0
    c = dict(st)
    c["review"] = PlanReview(approved=review.approved, feedback=[])
    assert c["review"].approved is review.approved
    assert FP.should_replan(st) == FP.should_replan(c) == "replan"


def test_approved_case_makes_all_three_arms_identical() -> None:
    """승인이면 **재분해하지 않는다** — 프로덕션 `should_replan` 과 같은 흐름."""
    st = _state()
    st["review"] = PlanReview(approved=True, feedback=[])
    assert FP.should_replan(st) == "approve"


def test_rejected_path_produces_three_distinct_arm_entries() -> None:
    """반려 경로에서 세 arm 이 **각각 기록**된다 (분해는 스텁으로 대체 — LLM 없음)."""
    case = next(c for c in H.load_cases() if c["case_id"] == "normal-toeic-near")
    calls: list[str] = []

    async def fake_validate(state: Any, cfg: Any) -> Any:
        from reaction_backend.orchestrator import first_plan_adapter as A

        return {
            **state,
            "planning_context": A.context_from_outcome(
                state["outcome"], target_date=_TODAY, density=state["density"]
            ),
        }

    async def fake_decompose(state: Any, cfg: Any) -> Any:
        calls.append(FP._replan_feedback(state))
        return {**state, "goal_plan": _stub_plan(len(calls)), "used_fallback": False}

    async def fake_review(state: Any, cfg: Any) -> Any:
        return {
            **state,
            "review": PlanReview(approved=False, feedback=["더 잘게 나눠보면"]),
            "replan_count": 1,
        }

    orig = (FP.validate_inputs, FP.decompose_goal, FP.review_plan)
    FP.validate_inputs, FP.decompose_goal, FP.review_plan = (  # type: ignore[assignment]
        fake_validate,
        fake_decompose,
        fake_review,
    )
    try:
        row = asyncio.run(M.run_case(case, 0, today=_TODAY))
    finally:
        FP.validate_inputs, FP.decompose_goal, FP.review_plan = orig  # type: ignore[assignment]

    assert row["rejected"] is True
    assert set(row["plans"]) == set(M.ARMS)
    assert all(row["plans"][a] is not None for a in M.ARMS)
    # 분해는 3회 — 초기 1 + B 1 + C 1.
    assert len(calls) == 3
    assert calls[0] == "(첫 분해 — 이전 피드백 없음)"  # 초기
    assert "더 잘게 나눠보면" in calls[1]  # B
    assert calls[2] == "(첫 분해 — 이전 피드백 없음)"  # C — 빈 피드백
    assert row["b_feedback_sent"] != row["c_feedback_sent"]


def _stub_plan(n: int) -> Any:
    from reaction_backend.schemas.planning import (
        ActionItemDraft,
        GoalDecomposition,
        GoalNodeDraft,
    )

    return GoalDecomposition(
        goal_nodes=[
            GoalNodeDraft(
                node_id="root",
                parent_id=None,
                title="t",
                node_type="root",
                order_index=0,
                is_leaf=False,
            )
        ],
        action_items=[
            ActionItemDraft(
                node_id=f"l{n}",
                title=f"작업 {n}",
                category="career",
                estimated_minutes=50,
                first_step="교재 펴기",
            )
        ],
    )


# ── 3. 일반/도전을 섞지 않는다 ──────────────────────────────────────────────


def test_stratum_is_required() -> None:
    """`--stratum` 이 필수여야 두 표본이 한 파일에 안 섞인다 (설계 §3.4)."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert "required=True" in src
    assert 'choices=("general", "challenge")' in src


@pytest.mark.parametrize(("stratum", "n"), [("general", 34), ("challenge", 16)])
def test_each_stratum_loads_its_own_cases(stratum: str, n: int) -> None:
    assert len(M.load_stratum(stratum)) == n


def test_output_paths_are_separate_per_stratum() -> None:
    assert M.run_dir("general") != M.run_dir("challenge")
    assert M.run_dir("general").name == "general"
    assert M.run_dir("challenge").name == "challenge"


# ── 4. 사전등록 상수가 코드에 박혀 있다 ─────────────────────────────────────


def test_bootstrap_constants_match_the_design() -> None:
    """설계 §4.2 가 **실행 전에** 고정한 값 — 코드에서 바꾸면 사전등록을 어긴다."""
    assert M.BOOTSTRAP_N == 10_000
    assert M.BOOTSTRAP_SEED == 42
    assert M.PRIMARY_REPEAT == 0
    assert M.ARMS == ("A_none", "B_feedback", "C_retry")


# ── 5. 집계 — 페어링·부트스트랩·M18 분리 ────────────────────────────────────


def test_bootstrap_is_deterministic_with_the_registered_seed() -> None:
    """같은 입력이면 같은 구간 — 시드가 고정돼 있어야 재현된다."""
    d = [1, 0, 0, 1, -1, 0, 1, 0]
    assert M.paired_bootstrap_ci(d) == M.paired_bootstrap_ci(d)


def test_bootstrap_point_estimate_is_the_mean_delta() -> None:
    assert M.paired_bootstrap_ci([1, 1, 0, 0])[0] == pytest.approx(0.5)
    assert M.paired_bootstrap_ci([-1, -1, -1, -1])[0] == pytest.approx(-1.0)


def test_bootstrap_ci_brackets_the_point_estimate() -> None:
    pt, lo, hi = M.paired_bootstrap_ci([1, 0, 1, 0, 1, 0, 1, 0, 1, 0])
    assert lo <= pt <= hi


def test_bootstrap_on_all_zero_deltas_gives_a_degenerate_interval() -> None:
    """세 arm 이 같으면(전부 승인) Δ 가 0 이고 구간도 0 이다 — 설계 §1.1."""
    assert M.paired_bootstrap_ci([0, 0, 0]) == (0.0, 0.0, 0.0)


def test_empty_deltas_do_not_crash() -> None:
    assert M.paired_bootstrap_ci([]) == (0.0, 0.0, 0.0)


def test_pairing_drops_a_case_missing_in_any_arm() -> None:
    """한 arm 에서만 계획이 없으면 **그 케이스를 뺀다** — 설계 §4.1.

    안 빼면 ΔM26-core 가 **서로 다른 케이스 집합의 차**가 된다.
    """
    cases = {c["case_id"]: c for c in M.load_stratum("challenge")}
    cid = next(iter(cases))
    rows = [{"case_id": cid, "plans": {"A_none": None, "B_feedback": None, "C_retry": None}}]
    deltas, dropped = M.paired_deltas(rows, cases, "A_none", "B_feedback", today=_TODAY)
    assert deltas == []
    assert dropped == [cid]


def test_m18_is_computed_separately_from_the_core_and() -> None:
    """M18 은 AND 에 없고 **나란히** 보고된다 — 별도 함수다."""
    assert hasattr(M, "_arm_m18b")
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert "M26-core 의 AND 에는 없고 나란히 보고한다" in src
    assert "M18b (arm 별 분포)" in src


def test_summary_records_the_scoring_basis_difference() -> None:
    """**최종 계획 기준**이라 L1-7A 의 원안 기준 M26-core 와 절대값을 비교하면 안 된다."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert "최종 계획" in src and "절대값을 비교" in src


def test_metric_computation_is_reused_not_copied() -> None:
    """지표 계산을 `l1_7_run` 에서 가져다 쓴다 — 옮겨 적으면 두 실험이 갈린다."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    for fn in ("R.score_raw", "R.core_verdicts", "R.m26_core", "SE.evaluate_case"):
        assert fn in src, f"`{fn}` 를 재사용하지 않고 있다"


def test_undetermined_sign_is_not_forced() -> None:
    """구간이 0 을 걸치면 **부호를 억지로 만들지 않는다** (설계 §5)."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert "억지로 만들지 않는다" in src
    assert "이 층을 룰로 대체하거나 걷어내자" in src


# ── 6. 재현성 — 기준일·run 파일·manifest ────────────────────────────────────


def test_rows_carry_the_base_date() -> None:
    """행이 **기준일을 들고 있어야** 재집계가 `date.today()` 를 안 쓴다."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert '"target_date": today.isoformat()' in src


def test_summarize_uses_the_stored_base_date_not_today() -> None:
    """⚠️ D 에 만든 계획을 D+1 로 재채점하면 **같은 원자료의 M33 이 날짜마다 달라진다.**"""
    # `_TODAY` 를 쓰면 그날 돌릴 때 `date.today()` 여도 통과한다. 과거로 못 박는다.
    rows = [{"target_date": "2019-04-01"}, {"target_date": "2019-04-01"}]
    assert M.base_date_of(rows) == date(2019, 4, 1)
    assert M.base_date_of(rows) != date.today()


def test_mixed_base_dates_are_rejected() -> None:
    """한 실행의 행만 집계한다 — 섞이면 거부."""
    with pytest.raises(SystemExit):
        M.base_date_of([{"target_date": "2026-09-03"}, {"target_date": "2026-09-04"}])


def test_rows_without_a_base_date_are_rejected() -> None:
    """기준일을 저장하기 전 형식의 옛 원자료를 **오늘 날짜로 재집계하지 않는다.**"""
    with pytest.raises(SystemExit):
        M.base_date_of([{"case_id": "x"}])


def test_summarize_does_not_call_today_for_scoring() -> None:
    """재집계 경로가 **오늘 날짜를 호출하지 않는다** — AST 로 실제 호출만 본다.

    ⚠️ 두 번 틀렸던 자리다. 처음엔 슬라이스를 `summarize` 이후로만 잡아 `_arm_verdicts`·
    `paired_deltas`·`_arm_m18b`·`base_date_of` 가 **전부 빠졌고**, 넓히니 이번엔
    docstring 의 **언급**에 걸렸다. 문자열이 아니라 호출을 세야 한다.
    """
    import ast

    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    scoring = {
        "_arm_verdicts",
        "_arm_m18b",
        "paired_deltas",
        "paired_bootstrap_ci",
        "base_date_of",
        "summarize",
        "check_manifest",
    }
    tree = ast.parse(src)
    found = [
        f"{fn.name}: {ast.unparse(node)}"
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and fn.name in scoring
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("today", "now", "utcnow")
    ]
    assert not found, f"재집계가 오늘 날짜로 다시 채점한다: {found}"
    # 이름이 다 있는지도 본다 — 함수를 지우거나 이름을 바꾸면 검사가 조용히 비어 버린다.
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert scoring <= defined, f"채점 함수가 사라졌다: {scoring - defined}"
    # 문자열만으론 부족하다 — 재집계가 실제로 저장된 값을 쓰는지 확인한다.
    assert M.base_date_of([{"target_date": "2001-02-03"}]) == date(2001, 2, 3)


def test_each_run_writes_a_new_file() -> None:
    """덮어쓰면 이전 실행이 사라져 **문서가 인용한 수치를 다시 못 만든다.**"""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert "run_dir(args.stratum)" in src
    assert "%Y%m%dT%H%M%S" in src


@pytest.mark.parametrize(
    "field",
    [
        "target_date",
        "git_sha",
        "golden_sha256",
        "raw_sha256",
        "started_at",
        "repeats",
        "bootstrap_seed",
    ],
)
def test_manifest_records_provenance(field: str) -> None:
    """원자료는 커밋 안 하므로 **manifest 가 그 실행을 지목하는 유일한 수단**이다."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert f'"{field}"' in src, f"manifest 에 `{field}` 가 없다"


# ── 7. 스모크 규약 — 사전등록 회차를 결과 보고 바꾸지 않는다 ────────────────


def test_smoke_protocol_forbids_a_full_one_repeat_probe() -> None:
    """전체를 1회로 먼저 돌려 반려율을 보고 3회 여부를 정하면 **사후 조정**이다."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert "`--limit 2 --repeats 1` 로만 한다" in src
    assert "결과를 보고 바꾸는 것" in src
    assert "본 실험 표본에서 제외" in src


def test_design_doc_carries_the_same_smoke_rule() -> None:
    doc = (_ROOT / "docs" / "experiments" / "m33-3arm-design.md").read_text(encoding="utf-8")
    assert "`--limit 2 --repeats 1` 로만 한다" in doc
    assert "전체 실행 전에" in doc
    # 재집계·run 파일 규칙도 설계에 있어야 한다.
    assert "저장된 `target_date` 를 쓴다" in doc
    assert "실행마다 새 원자료 파일" in doc


def test_call_count_range_is_two_to_four_per_case() -> None:
    """승인이면 2회(분해+검토), 반려면 4회(재분해 2 추가)."""
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    assert "{2 * n}~{4 * n}" in src
    assert "승인 2 / 반려 4" in src


# ═══════════════════════════════════════════════════════════════════════════
# 동작 테스트 — 문자열 검사가 못 잡는 것들
#
# ⚠️ 적대적 검증에서 **소스 문자열만 보는 테스트가 39개 중 21개**였고, 다음 변이가 전부
# 초록이었다: Δ **부호 뒤집기**, 부트스트랩 **90% CI 로 바꾸기**, `PRIMARY_REPEAT` 필터
# 제거(반복을 독립 표본으로), 승인인데도 재분해, **manifest 쓰기 통째로 삭제**, 출력을
# 고정 파일명으로(매 실행 덮어쓰기). 전부 사전등록 조항인데 무방비였다.
# 아래는 **실제로 호출해서** 값을 확인한다.
# ═══════════════════════════════════════════════════════════════════════════


class _FakeVerdicts:
    """`_arm_verdicts` 의 반환 자리. 스텁 `m26_core` 가 `.passed` 를 그대로 돌려준다."""

    def __init__(self, passed: bool) -> None:
        self.passed = passed


def _stub_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_arm_verdicts` → `m26_core` 경로를 가짜로 갈아끼운다(LLM·골든 없이 Δ 만 본다)."""
    monkeypatch.setattr(
        M, "_arm_verdicts", lambda case, plan, *, today: plan if plan is not None else None
    )
    monkeypatch.setattr(H, "m26_core", lambda v: (v.passed, {}))


def _row(case_id: str, *, a: bool | None, b: bool | None, c: bool | None) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "repeat": M.PRIMARY_REPEAT,
        "target_date": _TODAY.isoformat(),
        "plans": {
            "A_none": None if a is None else _FakeVerdicts(a),
            "B_feedback": None if b is None else _FakeVerdicts(b),
            "C_retry": None if c is None else _FakeVerdicts(c),
        },
    }


def test_delta_sign_is_b_minus_a_not_the_reverse(monkeypatch: pytest.MonkeyPatch) -> None:
    """**부호가 뒤집히면 결론이 정반대가 된다** — "④층 유지" ↔ "걷어내자".

    B(피드백 재분해)가 A(재분해 없음)보다 좋아지면 Δ 는 **양수**여야 한다.
    """
    _stub_scoring(monkeypatch)
    deltas, _ = M.paired_deltas(
        [_row("improved", a=False, b=True, c=True)], {}, "A_none", "B_feedback", today=_TODAY
    )
    assert deltas == [+1], "B 가 A 보다 좋아졌는데 Δ 가 +1 이 아니다"

    deltas, _ = M.paired_deltas(
        [_row("regressed", a=True, b=False, c=False)], {}, "A_none", "B_feedback", today=_TODAY
    )
    assert deltas == [-1], "B 가 A 보다 나빠졌는데 Δ 가 -1 이 아니다"


def test_pairing_drops_cases_undefined_in_any_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """한 arm 에서만 N/A 면 Δ 가 **서로 다른 케이스 집합의 차**가 된다(설계 §4.1)."""
    _stub_scoring(monkeypatch)
    rows = [_row("ok", a=False, b=True, c=True), _row("na-in-c", a=False, b=True, c=None)]
    deltas, dropped = M.paired_deltas(rows, {}, "A_none", "B_feedback", today=_TODAY)
    assert deltas == [+1] and dropped == ["na-in-c"]


def test_duplicate_cases_are_refused_because_the_unit_is_the_case() -> None:
    """행을 리샘플하면 상관된 표본으로 **구간이 거짓으로 좁아진다**.

    같은 날 두 실행을 이어붙이면 `base_date_of` 는 통과하므로 여기서 막아야 한다
    (실측: 16행 CI [-0.1875, 0] → 32행 CI [-0.1562, 0]).
    """
    rows = [_row("dup", a=True, b=True, c=True), _row("dup", a=True, b=True, c=True)]
    with pytest.raises(SystemExit, match="케이스가 중복"):
        M.paired_deltas(rows, {}, "A_none", "B_feedback", today=_TODAY)


def test_bootstrap_interval_is_the_registered_two_sided_95() -> None:
    """**백분위를 바꾸면 이 값이 바뀐다** — 90% 로 몰래 넓히거나 좁히는 것을 막는다."""
    deltas = [1, 0, 0, 1, -1, 0, 1, 0]
    point, lo, hi = M.paired_bootstrap_ci(deltas)
    assert point == pytest.approx(0.25)
    # 시드 42 · 10,000회 · 양측 95% 에서 재현되는 값. 셋 중 하나라도 바뀌면 달라진다.
    assert (lo, hi) == pytest.approx((-0.25, 0.625))
    assert M.paired_bootstrap_ci(deltas) == (point, lo, hi), "시드가 고정이 아니다"


def test_bootstrap_is_not_computed_at_a_narrower_percentile() -> None:
    """90% 구간은 95% 구간보다 **좁다** — 지금 값이 95% 쪽임을 교차 확인한다."""
    import random

    deltas = [1, 0, 0, 1, -1, 0, 1, 0]
    _, lo95, hi95 = M.paired_bootstrap_ci(deltas)
    rng = random.Random(M.BOOTSTRAP_SEED)
    s = sorted(sum(rng.choices(deltas, k=len(deltas))) / len(deltas) for _ in range(M.BOOTSTRAP_N))
    lo90, hi90 = s[int(0.05 * M.BOOTSTRAP_N)], s[int(0.95 * M.BOOTSTRAP_N)]
    assert (hi90 - lo90) < (hi95 - lo95), "구간이 90% 로 계산되고 있다"


def test_only_the_primary_repeat_enters_the_aggregate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**반복을 독립 표본으로 세면 안 된다**(설계 §4.2, L1-7A §4 에서 한 실수).

    repeat 1 행을 섞어도 집계 n 이 늘지 않아야 한다.
    """
    _stub_scoring(monkeypatch)
    monkeypatch.setattr(M, "load_stratum", lambda stratum: [])
    monkeypatch.setattr(M, "_arm_m18b", lambda case, plan, *, today: None)
    rows = [_row("c1", a=False, b=True, c=True), _row("c2", a=True, b=True, c=True)]
    extra = [dict(r, repeat=M.PRIMARY_REPEAT + 1) for r in rows]

    M.summarize(rows + extra, stratum="challenge")
    out = capsys.readouterr().out
    assert "n=2" in out, f"repeat 1 행이 표본에 섞였다 (반복을 독립 표본으로 셌다)\n{out}"
    assert "고유 2건" in out


def _write_run(tmp_path: Path, *, repeats: int = 3, limit: int | None = None) -> Path:
    raw = tmp_path / "20260904T120000.jsonl"
    raw.write_text('{"case_id":"x"}\n', encoding="utf-8")
    M.write_manifest(raw, stratum="challenge", today=_TODAY, repeats=repeats, limit=limit, n_rows=1)
    return raw


def test_manifest_is_actually_written_with_provenance(tmp_path: Path) -> None:
    """**`write_manifest` 를 통째로 지워도 문자열 테스트는 초록이었다.** 실제로 쓴다."""
    raw = _write_run(tmp_path)
    meta_path = raw.with_suffix(".meta.json")
    assert meta_path.exists(), "manifest 가 실제로 쓰이지 않았다"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for key in (
        "stratum",
        "target_date",
        "repeats",
        "limit",
        "rows",
        "started_at",
        "git_sha",
        "git_dirty",
        "golden_path",
        "golden_sha256",
        "raw_sha256",
        "raw_bytes",
        "primary_repeat",
        "bootstrap_n",
        "bootstrap_seed",
        "arms",
    ):
        assert key in meta, f"manifest 에 {key} 가 없다"
    # 해시는 **실제 파일**의 것이어야 한다 — 적어만 두고 틀리면 대조가 무의미하다.
    assert meta["raw_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert meta["raw_bytes"] == raw.stat().st_size
    assert (
        meta["golden_sha256"]
        == hashlib.sha256(M.STRATUM_PATHS["challenge"].read_bytes()).hexdigest()
    )
    assert meta["target_date"] == _TODAY.isoformat()
    assert (meta["primary_repeat"], meta["bootstrap_n"], meta["bootstrap_seed"]) == (
        M.PRIMARY_REPEAT,
        M.BOOTSTRAP_N,
        M.BOOTSTRAP_SEED,
    )


def test_reaggregation_refuses_a_changed_golden_set(tmp_path: Path) -> None:
    """🔴 **기준일 고정은 절반이다.** `summarize` 는 골든을 재집계 시점에 다시 읽는다.

    원자료를 한 글자도 안 바꿔도 골든이 바뀌면 M33 이 바뀐다(실측 -0.0625 → +0.0000).
    manifest 에 `golden_sha256` 을 **적어두기만 하고 대조하지 않으면 적은 의미가 없다.**
    """
    raw = _write_run(tmp_path)
    M.check_manifest(raw, stratum="challenge", allow_smoke=False)  # 지금은 통과한다

    meta_path = raw.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["golden_sha256"] = "0" * 64
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(SystemExit, match="골든셋이 실행 당시와 다르다"):
        M.check_manifest(raw, stratum="challenge", allow_smoke=False)


def test_reaggregation_refuses_a_run_without_a_manifest(tmp_path: Path) -> None:
    """manifest 가 없으면 **어느 골든·어느 커밋으로 만든 원자료인지 특정할 수 없다.**"""
    raw = tmp_path / "20260904T120000.jsonl"
    raw.write_text('{"case_id":"x"}\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="manifest 가 없다"):
        M.check_manifest(raw, stratum="challenge", allow_smoke=False)


def test_reaggregation_refuses_a_smoke_run_by_default(tmp_path: Path) -> None:
    """설계 §4.3 이 스모크를 **본 실험 표본에서 제외**한다 — 문서가 아니라 코드가 막는다.

    `--summarize-only` 의 기본값이 스모크 파일을 고르면 그대로 집계돼 버린다.
    """
    raw = _write_run(tmp_path, repeats=1, limit=2)
    with pytest.raises(SystemExit, match="스모크"):
        M.check_manifest(raw, stratum="challenge", allow_smoke=False)
    M.check_manifest(raw, stratum="challenge", allow_smoke=True)  # 명시하면 통과


def test_reaggregation_refuses_a_different_stratum(tmp_path: Path) -> None:
    """일반과 도전을 섞어 한 수치로 내지 않는다(설계 §3.4)."""
    raw = _write_run(tmp_path)
    with pytest.raises(SystemExit, match="층이 다르다"):
        M.check_manifest(raw, stratum="general", allow_smoke=False)


def test_base_date_refuses_rows_that_only_partly_carry_it() -> None:
    """옛/새 원자료를 이어붙인 경우 — 조용히 건너뛰면 **옛 행이 새 기준일로 채점된다.**"""
    with pytest.raises(SystemExit, match="target_date"):
        M.base_date_of([{"target_date": "2026-09-04"}, {}])


def test_latest_run_ignores_files_that_are_not_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`smoke.jsonl` 같은 이름이 문자열 정렬에서 이기면 **그게 기본 재집계 대상**이 된다."""
    d = tmp_path / "challenge"
    d.mkdir()
    (d / "20260904T120000.jsonl").write_text("", encoding="utf-8")
    (d / "smoke.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(M, "RUNS_DIR", tmp_path)
    got = M.latest_run("challenge")
    assert got is not None and got.name == "20260904T120000.jsonl"


def test_rel_path_does_not_raise_outside_the_repo() -> None:
    """`--run` 을 상대 경로로 주면 `relative_to` 가 던져 재집계가 죽었다."""
    assert M._rel(Path("eval/m33/x.jsonl"))
    assert M._rel(Path(tempfile.gettempdir()) / "y.jsonl")


def test_review_fallback_is_not_counted_as_an_approval() -> None:
    """🔴 검토 LLM 이 **실패하면 프로덕션 룰이 `approved=True` 를 돌려준다.**

    `_rule_review` 는 무한 cycle 을 막으려고 그렇게 설계됐고(HITL 이 최종 게이트),
    그래서 `state["review"]` 는 **절대 None 이 아니다.** `review is None` 만 보면
    LLM 타임아웃이 "승인" 으로 집계돼 **반려 집합이 조용히 줄고 M33 이 0 쪽으로
    편향된다.** `used_fallback` 을 봐야 한다.
    """
    assert FP._rule_review(_state()).approved is True, "폴백이 승인을 안 돌려준다면 전제가 바뀐 것"

    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    guard = src[
        src.index("state = await FP.review_plan") : src.index("rejected = FP.should_replan")
    ]
    assert 'state["used_fallback"]' in guard, "검토 폴백을 감지하지 않는다"
    assert '"review_fell_back"' in src, "행에 검토 폴백 여부를 남기지 않는다"


def test_review_fallback_marks_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """폴백한 행이 `fell_back` 으로 표시돼 집계에서 빠지는가 — **실제로 돌려본다.**"""

    async def _ok(state: Any, cfg: Any) -> Any:
        return state

    async def _decompose(state: Any, cfg: Any) -> Any:
        # 검토 폴백이면 `_dump` 에 닿기 전에 반환하므로 자리표시자면 충분하다.
        return {**state, "goal_plan": object(), "used_fallback": False}

    async def _review_falls_back(state: Any, cfg: Any) -> Any:
        # 프로덕션과 같은 모양: 룰 폴백이 승인을 돌려주고 used_fallback 이 선다.
        return {
            **state,
            "review": PlanReview(approved=True, feedback=[]),
            "replan_count": state["replan_count"] + 1,
            "used_fallback": True,
        }

    monkeypatch.setattr(M.FP, "validate_inputs", _ok)
    monkeypatch.setattr(M.FP, "decompose_goal", _decompose)
    monkeypatch.setattr(M.FP, "review_plan", _review_falls_back)

    case = next(c for c in M.load_stratum("challenge") if c["case_id"])
    row = asyncio.run(M.run_case(case, 0, today=_TODAY))
    assert row["fell_back"] is True, "검토 폴백이 '승인' 으로 집계된다"
    assert row["stage"] == "review"
    assert row["review_fell_back"] is True


def test_two_runs_in_the_same_second_do_not_overwrite(tmp_path: Path) -> None:
    """모듈 docstring 이 "실행마다 새 파일" 이라고 단언한다 — 같은 초에도 지켜야 한다.

    `--limit 2` 같은 짧은 실행에서 현실적으로 발생한다.
    """
    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    body = src[src.index("stamp = _dt.now()") : src.index("out_path.write_text")]
    assert "while out_path.exists()" in body, "같은 초에 두 번 실행하면 덮어쓴다"
    assert M.RUN_NAME_RE.match("20260904T120000") and M.RUN_NAME_RE.match("20260904T120000-1")


def test_approval_shares_one_plan_across_all_three_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    """승인이면 **B·C 도 재분해하지 않는다**(설계 §1.1) — 프로덕션 `should_replan` 과 같다.

    승인인데 재분해하면 (1) 호출이 2배로 늘고 (2) 승인 케이스가 Δ 에 **0 이 아닌 값을
    기여**해 M33 이 오염된다. 이 변이는 문자열 테스트가 못 잡았다.
    """
    calls: list[str] = []

    async def _ok(state: Any, cfg: Any) -> Any:
        return state

    async def _decompose(state: Any, cfg: Any) -> Any:
        calls.append("decompose")
        return {**state, "goal_plan": _stub_plan(len(calls)), "used_fallback": False}

    async def _approve(state: Any, cfg: Any) -> Any:
        calls.append("review")
        return {
            **state,
            "review": PlanReview(approved=True, feedback=[]),
            "replan_count": state["replan_count"] + 1,
            "used_fallback": False,
        }

    monkeypatch.setattr(M.FP, "validate_inputs", _ok)
    monkeypatch.setattr(M.FP, "decompose_goal", _decompose)
    monkeypatch.setattr(M.FP, "review_plan", _approve)

    case = M.load_stratum("challenge")[0]
    row = asyncio.run(M.run_case(case, 0, today=_TODAY))

    assert row["rejected"] is False and row["fell_back"] is False
    assert calls == ["decompose", "review"], f"승인인데 재분해했다: {calls}"
    plans = row["plans"]
    assert plans["A_none"] == plans["B_feedback"] == plans["C_retry"], (
        "승인 케이스인데 세 arm 의 계획이 다르다 — Δ 에 0 이 아닌 값을 기여하게 된다"
    )


def test_run_path_imports_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """본 실행 경로의 **지연 임포트가 실제로 풀리는가.**

    ⚠️ `main_async` 의 실행 분기는 실 LLM 을 부르므로 테스트가 안 탄다. 그 사각지대에서
    `reaction_backend.core.config` (없는 모듈)를 임포트하는 코드가 스모크까지 살아남았다.
    `--dry-run` 은 LLM 없이 그 분기를 **임포트 지점 너머까지** 통과한다.
    """
    args = argparse.Namespace(
        stratum="challenge",
        limit=2,
        repeats=1,
        dry_run=True,
        summarize_only=False,
        run=None,
        allow_smoke=False,
    )
    asyncio.run(M.main_async(args))  # 예외 없이 끝나야 한다


def test_lazy_imports_in_the_harness_all_resolve() -> None:
    """하네스 안의 **모든 지연 임포트**를 실제로 불러본다 — 오타는 실행 때까지 안 잡힌다."""
    import ast
    import importlib

    src = (_ROOT / "scripts" / "l1_7b_m33_run.py").read_text(encoding="utf-8")
    mods = {
        n.module
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0
    }
    names = {
        (n.module, a.name)
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0
        for a in n.names
    }
    for mod in sorted(mods):
        importlib.import_module(mod)  # 없으면 ModuleNotFoundError
    for mod, name in sorted(names):
        # 모듈이 있어도 **이름이 없을 수 있다** — `settings` 가 실제로 그랬다.
        assert hasattr(importlib.import_module(mod), name), f"{mod} 에 {name} 가 없다"
