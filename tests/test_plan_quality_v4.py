"""`plan_quality_eval.v4` — 평가 후보 검토기의 계약을 불변식으로 고정한다.

이 파일이 지키는 것 네 가지:

1. **출력 계약** — `severity` 범위, 결함 코드 집합, 그리고 **승인 판정은 코드가 한다**.
2. **변수 계약** — `focus_capacity`·`session_length` 가 프롬프트에도 하네스에도 없다.
   루브릭 §1.2 는 "변수를 주고 보지 마라고 쓰면 지킨다는 보장이 없다" 고 못박았으므로,
   **변수 자체가 없는 것**을 테스트가 지킨다.
3. **프로덕션 격리** — v4 파일이 존재해도 ④층은 여전히 v3 를 부른다.
   ⚠️ 레지스트리의 `latest()` 는 **최고 버전을 자동 선택**한다. 그래서 프롬프트를
   `plan_quality.v4.md` 로 두면 파일이 있다는 것만으로 `planning/plan_quality` 가 v4 로
   해석된다. 평가 후보 이름을 `plan_quality_eval` 로 분리한 것이 1차 방어이고,
   그 분리가 유지되는지를 여기서 지킨다.
   ⚠️ **2026-09-06 갱신** — 이름 분리는 규율이지 구조가 아니었다(다른 사람이 다른
   이름을 고르면 끝난다). 이제 **호출부가 `@v3` 로 핀돼 있어** 파일 이름과 무관하게
   프로덕션이 안 움직인다. 그 계약은 `tests/test_plan_quality_version_pin.py` 가 든다.
4. **없는 `node_id` 를 조용히 받지 않는다** — 지어낸 노드는 위치 지목 실패로 세고,
   분모에서 빼주지 않는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from scripts import check_seeded_defect_shortcuts as shortcuts
from scripts import l1_7b_run as v3_harness
from scripts import l1_7b_v4_run as harness

from reaction_backend.prompts import registry
from reaction_backend.schemas.planning import (
    REJECT_SEVERITY_THRESHOLD,
    PlanFinding,
    PlanReviewV4,
    approved_from_findings,
)

_ROOT = Path(__file__).resolve().parent.parent
_V4_ID = "planning/plan_quality_eval@v4"
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _finding(**kw: Any) -> PlanFinding:
    base = {"defect": "D1", "severity": 2, "node_id": "l1", "message": "이렇게 묶어보면 어떨까요"}
    return PlanFinding.model_validate({**base, **kw})


# ── 1. 출력 계약 ────────────────────────────────────────────────────────────


def test_severity_is_bounded_to_the_rubric_scale() -> None:
    """1~3 밖의 severity 는 스키마가 막는다 — 4를 허용하면 임계값 스윕이 무의미해진다."""
    for bad in (0, 4, -1):
        with pytest.raises(ValueError):
            _finding(severity=bad)
    for good in (1, 2, 3):
        assert _finding(severity=good).severity == good


def test_defect_code_must_be_one_of_the_five() -> None:
    """자유 문자열을 받으면 M28a(유형 지목)를 기계가 못 센다."""
    with pytest.raises(ValueError):
        _finding(defect="D6")
    with pytest.raises(ValueError):
        _finding(defect="중복")


def test_node_id_and_message_cannot_be_empty() -> None:
    with pytest.raises(ValueError):
        _finding(node_id="")
    with pytest.raises(ValueError):
        _finding(message="")


def test_llm_does_not_decide_approval() -> None:
    """v4 출력에 `approved` 필드가 **없다.**

    있으면 LLM 이 임계값을 스스로 정하게 되고, `severity` 를 바꿔가며 운영점을 고르는
    일(M27b 대 M29 곡선)이 불가능해진다.
    """
    assert "approved" not in PlanReviewV4.model_fields
    assert set(PlanReviewV4.model_fields) == {"findings"}


def test_approval_is_derived_from_severity_by_code() -> None:
    assert approved_from_findings([]) is True
    assert approved_from_findings([_finding(severity=1)]) is True
    assert approved_from_findings([_finding(severity=1), _finding(severity=2)]) is False
    assert approved_from_findings([_finding(severity=3)]) is False
    # 임계값은 인자다 — 저장된 원자료에 다시 적용해 운영점을 고른다.
    assert approved_from_findings([_finding(severity=2)], threshold=3) is True
    assert approved_from_findings([_finding(severity=3)], threshold=3) is False


def test_empty_findings_is_the_default() -> None:
    assert PlanReviewV4().findings == []
    assert approved_from_findings(PlanReviewV4().findings) is True


# ── 2. 변수 계약 — 금지 변수가 **존재하지 않는다** ──────────────────────────


def test_v4_prompt_never_receives_layer3_invariants() -> None:
    """루브릭 §1.2 — `focus_capacity`·`session_length` 를 넘기지 않는다.

    ③층 `normalize_action_minutes` 가 이미 불변식으로 보장하므로(상한 초과 0/1,620),
    그 항목의 반려는 정의상 전부 오탐이다. 프롬프트 본문에서도 사라져야 한다.
    """
    body = registry.get(_V4_ID).body
    for banned in ("focus_capacity", "session_length"):
        assert banned not in body, f"v4 프롬프트가 {banned} 를 다시 들고 있다 (루브릭 §1.2 위반)"
        assert banned not in harness.review_variables_v4(_any_case())


def test_harness_variables_exactly_cover_the_prompt() -> None:
    """하네스가 넘기는 키 == 프롬프트가 요구하는 키.

    모자라면 `PromptRenderError` → 룰 폴백으로 강등돼 "집계 대상 0건" 이 된다.
    L1-7A 1차 실행이 정확히 그 사고로 34호출을 통째로 버렸다.
    """
    placeholders = set(_VAR_RE.findall(registry.get(_V4_ID).body))
    supplied = set(harness.review_variables_v4(_any_case()))
    assert placeholders == supplied, f"프롬프트 {sorted(placeholders)} vs 하네스 {sorted(supplied)}"


def test_v4_prompt_renders_with_only_the_harness_variables() -> None:
    text, tmpl = registry.render(_V4_ID, harness.review_variables_v4(_any_case()))
    assert "{{" not in text
    assert tmpl.full_id == _V4_ID


def test_v4_keeps_the_v3_tone_rules() -> None:
    """톤 규칙은 v3 와 동등해야 한다 — 금지어가 갈리면 두 버전의 출력을 비교할 수 없다."""
    v3 = registry.get("planning/plan_quality@v3").body
    v4 = registry.get(_V4_ID).body
    banned = re.search(r"금지어:(.+)", v3)
    assert banned is not None
    for word in re.findall(r'"([^"]+)"', banned.group(1)):
        assert f'"{word}"' in v4, f"v4 금지어에 {word!r} 가 빠졌다"
    assert "탓하거나" in v4


def test_v4_prompt_carries_all_five_defect_anchors() -> None:
    body = registry.get(_V4_ID).body
    for code in ("D1", "D2", "D3", "D4", "D5"):
        assert f"### {code}." in body, f"v4 프롬프트에 {code} 앵커가 없다"
    # 룰이 만든 회차 카드를 결함으로 세면 D1 오탐이 쏟아진다 (루브릭 §2 D1 경고).
    assert "tmp-continue-" in body


# ── 3. 프로덕션 격리 ───────────────────────────────────────────────────────


def test_production_review_still_resolves_to_v3() -> None:
    """④층이 **실제로 부르는** 프롬프트가 v3 로 해석된다.

    ⚠️ **`registry.get("...@v3").version == "3"` 로 쓰면 안 된다** — 그건 v3 파일이
    사라지지 않는 한 절대 실패하지 않는 항진명제이고, 호출부를 `@v4` 로 바꿔도 초록이다.
    그래서 **호출부 소스에서 실제 `prompt_id` 를 뽑아** 해석한다.

    ⚠️ **예전 서술 정정** — 초판은 "레지스트리가 버전을 안 붙이면 최고 버전을 고르므로
    `plan_quality.v4.md` 를 두는 순간 프로덕션이 v4 를 부른다" 고 적었다. 그 위험은
    실재했고, 이제 호출부가 `@v3` 로 핀돼 있다(`tests/test_plan_quality_version_pin.py`).
    """
    src = (_ROOT / "src" / "reaction_backend" / "orchestrator" / "first_plan.py").read_text(
        encoding="utf-8"
    )
    used = re.findall(r'prompt_id="(planning/plan_quality[^"]*)"', src)
    assert len(used) == 1, f"④층 호출이 하나가 아니다: {used}"

    assert registry.get(used[0]).version == "3", (
        f"프로덕션 ④층이 부르는 {used[0]!r} 가 v3 가 아니다 — 검토기가 바뀌었다."
    )


def test_v4_prompt_is_not_referenced_from_production_code() -> None:
    """`src/` 어디에서도 v4 를 **부르지** 않는다 — 평가 하네스 전용이다.

    주석·docstring 의 **언급**은 허용한다(스키마가 자기 짝 프롬프트를 적어두는 것은 옳다).
    막아야 하는 것은 `registry` 가 해석할 수 있는 **prompt_id 참조**뿐이다.
    """
    offenders = [
        str(p.relative_to(_ROOT))
        for p in (_ROOT / "src").rglob("*.py")
        if "planning/plan_quality_eval" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"프로덕션 코드가 v4 를 prompt_id 로 참조한다: {offenders}"


def test_production_orchestrator_still_calls_the_v3_prompt_id() -> None:
    """⚠️ 초판은 **버전 없는** `"planning/plan_quality"` 가 있는지를 검사했다 — 즉
    안전하지 않은 형태를 고정하고 있었다. 지금은 핀된 형태를 요구한다.

    계약 자체는 `tests/test_plan_quality_version_pin.py` 가 들고 있고, 여기서는
    v4 문서 맥락에서 같은 사실을 한 번 더 확인한다.
    """
    src = (_ROOT / "src" / "reaction_backend" / "orchestrator" / "first_plan.py").read_text(
        encoding="utf-8"
    )
    assert 'prompt_id="planning/plan_quality@v3"' in src


# ── 4. 없는 node_id 를 조용히 받지 않는다 ──────────────────────────────────


def _any_case() -> dict[str, Any]:
    return _case("d1-easy-cert")


def _case(case_id: str) -> dict[str, Any]:
    for row in harness.load_cases():
        if row["case_id"] == case_id:
            return row
    raise AssertionError(f"골든셋에 {case_id} 가 없다")


def _row(case: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {"case_id": case["case_id"], "block": case["block"], "repeat": 0, "findings": findings}


def test_hallucinated_node_id_is_flagged_not_accepted() -> None:
    case = _case("d1-easy-cert")
    fake = {"defect": "D1", "severity": 2, "node_id": "l999", "message": "m"}
    c = harness.classify_findings(_row(case, [fake]), case, threshold=2)
    assert c["has_invalid_node"] is True
    assert c["n_invalid_nodes"] == 1
    # 반려는 성립하지만(severity 2), 위치 지목은 실패다.
    assert c["approved"] is False
    assert c["hit_node"] is False


def test_real_node_id_is_not_flagged() -> None:
    case = _case("d1-easy-cert")
    real = sorted(harness.plan_leaf_ids(case))[0]
    c = harness.classify_findings(
        _row(case, [{"defect": "D1", "severity": 2, "node_id": real, "message": "m"}]),
        case,
        threshold=2,
    )
    assert c["has_invalid_node"] is False


def test_localization_needs_the_actual_injection_point() -> None:
    case = _case("d1-easy-cert")
    target = case["seeded"]["target_node_ids"][0]
    other = next(
        i for i in sorted(harness.plan_leaf_ids(case)) if i not in case["seeded"]["target_node_ids"]
    )
    hit = harness.classify_findings(
        _row(case, [{"defect": "D1", "severity": 2, "node_id": target, "message": "m"}]),
        case,
        threshold=2,
    )
    miss = harness.classify_findings(
        _row(case, [{"defect": "D1", "severity": 2, "node_id": other, "message": "m"}]),
        case,
        threshold=2,
    )
    assert hit["hit_node"] is True
    assert miss["hit_node"] is False


def test_severity_one_finding_does_not_earn_a_detection() -> None:
    """경계(severity 1)로 적은 것은 반려도 아니고 M27b 적중도 아니다."""
    case = _case("d1-easy-cert")
    target = case["seeded"]["target_node_ids"][0]
    c = harness.classify_findings(
        _row(case, [{"defect": "D1", "severity": 1, "node_id": target, "message": "m"}]),
        case,
        threshold=2,
    )
    assert c["approved"] is True
    assert c["hit_type"] is False
    assert c["hit_node"] is False


def test_wrong_defect_code_is_a_rejection_but_not_a_recall_hit() -> None:
    """M27gap 의 정의 그대로 — 반려는 했는데 틀린 이유로 했다."""
    case = _case("d1-easy-cert")
    target = case["seeded"]["target_node_ids"][0]
    c = harness.classify_findings(
        _row(case, [{"defect": "D5", "severity": 3, "node_id": target, "message": "m"}]),
        case,
        threshold=2,
    )
    assert c["approved"] is False  # M27a 는 잡힌다
    assert c["hit_type"] is False  # M27b 는 못 잡는다 → M27gap 에 들어간다


# ── 5. 지표 재료의 단일 구현 ───────────────────────────────────────────────


def test_m28_leak_list_comes_from_the_checker_only() -> None:
    """하네스가 누출 목록을 **자기가 다시 계산하지 않는다.**

    두 곳에 두면 갈린다 — 결함 재의뢰 3차 때 검사기에는 있고 테스트에는 없는 특징이
    생겼던 것과 같은 부류의 사고다.
    """
    cases = [json.loads(x) for x in _golden_lines()]
    leaked = shortcuts.m28_leaked_case_ids(cases)
    assert len(leaked) == 6, f"누출 목록이 바뀌었다: {leaked}"
    assert all(c["block"] == "seeded_defect" for c in cases if c["case_id"] in set(leaked))
    assert "m28_leaked_case_ids" in (_ROOT / "scripts" / "l1_7b_v4_run.py").read_text(
        encoding="utf-8"
    )


def test_binomial_bound_matches_the_v3_harness() -> None:
    """두 하네스의 Clopper-Pearson 구현이 갈리지 않는다."""
    for n in (12, 30, 50):
        for k in range(0, 4):
            assert harness.one_sided_upper_95(k, n) == pytest.approx(
                v3_harness.one_sided_upper_95(k, n), abs=1e-12
            )
    # 0/30 의 정확 상한 — rule of three(0.100) 가 아니라 0.095 다.
    assert harness.one_sided_upper_95(0, 30) == pytest.approx(0.0952, abs=5e-4)


def test_default_threshold_is_the_registered_one() -> None:
    assert REJECT_SEVERITY_THRESHOLD == 2
    assert harness.PRIMARY_REPEAT == 0


def _golden_lines() -> list[str]:
    path = _ROOT / "eval" / "golden_first_plan_cases.jsonl"
    return [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── 6. 집계 함수 `compute_metrics` — 분모를 정하는 코드 ─────────────────────
#
# ⚠️ 독립 검증(2026-09-03)이 이 자리가 **완전히 무방비**임을 뮤테이션으로 증명했다:
# 누출 제외 삭제 · 지어낸 노드를 분모에서 면제 · easy 한정 해제를 **동시에** 해도
# 22개 테스트가 전부 초록이었다. 분모를 정하는 코드가 이 작업의 유일한 산출물인데
# 테스트가 재료 함수(`classify_findings`)까지만 닿고 있었다.


def _leaked_easy_id() -> str:
    ids = shortcuts.m28_leaked_case_ids(harness.load_cases())
    return next(i for i in ids if "-easy-" in i)


def _plain_easy_id() -> str:
    """누출 목록에 **없는** easy 결함 케이스."""
    leaked = set(shortcuts.m28_leaked_case_ids(harness.load_cases()))
    return next(
        r["case_id"]
        for r in harness.load_cases()
        if r["block"] == "seeded_defect"
        and r["seeded"]["level"] == "easy"
        and r["case_id"] not in leaked
    )


def _reject_row(case_id: str, *, repeat: int = 0, node: str | None = None) -> dict[str, Any]:
    """그 케이스의 심은 유형·주입 지점을 정확히 짚은 severity 2 반려 한 건."""
    case = _case(case_id)
    return {
        "case_id": case_id,
        "block": case["block"],
        "repeat": repeat,
        "level": case["seeded"]["level"],
        "defect": case["seeded"]["defect"],
        "findings": [
            {
                "defect": case["seeded"]["defect"],
                "severity": 2,
                "node_id": node or case["seeded"]["target_node_ids"][0],
                "message": "m",
            }
        ],
    }


def _clean_row(case_id: str, *, repeat: int = 0) -> dict[str, Any]:
    case = _case(case_id)
    row: dict[str, Any] = {
        "case_id": case_id,
        "block": case["block"],
        "repeat": repeat,
        "findings": [],
    }
    if case["block"] == "seeded_defect":
        row["level"] = case["seeded"]["level"]
        row["defect"] = case["seeded"]["defect"]
    return row


def test_m28b_excludes_leaked_cases_from_the_denominator() -> None:
    """위치 누출 케이스는 M28b **분모에서 빠진다** — 제외를 지우면 이 테스트가 빨강."""
    leaked, plain = _leaked_easy_id(), _plain_easy_id()
    m = harness.compute_metrics([_reject_row(leaked), _reject_row(plain)], threshold=2)
    assert m["m28b"]["n"] == 1, "누출 케이스가 분모에 남아 있다"
    assert m["m28b"]["excluded"] == [leaked]
    # 제외 안 한 값도 함께 보고한다 — 둘 다 반려했으므로 2건.
    assert m["m28b"]["full_n"] == 2


def test_hallucinated_node_row_stays_in_the_m28b_denominator() -> None:
    """지어낸 노드는 **면제가 아니라 실패**다 — 분자에서 빠지고 분모에는 남는다."""
    plain = _plain_easy_id()
    m = harness.compute_metrics([_reject_row(plain, node="l999")], threshold=2)
    assert m["m28b"]["n"] == 1, "지어낸 노드 행이 분모에서 빠졌다 (면제하면 안 된다)"
    assert m["m28b"]["k"] == 0
    assert m["schema"]["rows_with_invalid"] == 1
    assert m["schema"]["invalid_findings"] == 1


def test_recall_denominator_is_easy_only() -> None:
    """boundary 는 통과가 정답이라 M27b·M27gap·M28a 분모에 들어가지 않는다."""
    boundary = next(
        r["case_id"]
        for r in harness.load_cases()
        if r["block"] == "seeded_defect" and r["seeded"]["level"] == "boundary"
    )
    plain = _plain_easy_id()
    m = harness.compute_metrics([_reject_row(plain), _reject_row(boundary)], threshold=2)
    assert sum(v["n"] for v in m["m27b"].values()) == 1, "boundary 가 recall 분모에 들어갔다"
    assert m["m27gap"]["n"] == 1
    assert m["m28a"]["n"] == 1
    # 그래도 M27a 에는 boundary 반려가 **오탐으로** 잡혀야 한다.
    bdefect = _case(boundary)["seeded"]["defect"]
    assert m["m27a"][bdefect]["boundary"]["rej"] == 1


def test_m29_uses_only_the_primary_repeat() -> None:
    """반복을 독립 표본으로 세지 않는다 — repeat 1 의 반려는 M29 에 안 들어간다."""
    ctl = next(r["case_id"] for r in harness.load_cases() if r["block"] == "defect_free_control")
    case = _case(ctl)
    node = case["plan"]["action_items"][0]["node_id"]
    bad = {
        "case_id": ctl,
        "block": "defect_free_control",
        "repeat": 1,
        "findings": [{"defect": "D1", "severity": 3, "node_id": node, "message": "m"}],
    }
    m = harness.compute_metrics([_clean_row(ctl), bad], threshold=2)
    assert m["m29"]["k"] == 0, "repeat 1 의 반려가 M29 에 섞였다"
    assert m["m29"]["n"] == 1
    # 전 반복 관찰은 따로 보고된다.
    assert m["control_findings_all_repeats"] == 1


def test_m27gap_counts_rejections_with_the_wrong_defect_code() -> None:
    plain = _plain_easy_id()
    case = _case(plain)
    wrong = "D5" if case["seeded"]["defect"] != "D5" else "D1"
    row = _reject_row(plain)
    row["findings"][0]["defect"] = wrong
    m = harness.compute_metrics([row], threshold=2)
    assert m["m27gap"]["k"] == 1
    assert m["m27gap"]["cases"][0]["named"] == [wrong]
    assert m["m28a"]["k"] == 0


def test_severity_one_is_not_a_rejection_anywhere() -> None:
    """severity 1 메모는 M27a·M27b·M28a·M28b 어디에서도 적중으로 세지 않는다."""
    plain = _plain_easy_id()
    row = _reject_row(plain)
    row["findings"][0]["severity"] = 1
    m = harness.compute_metrics([row], threshold=2)
    assert m["m27a"][_case(plain)["seeded"]["defect"]]["easy"]["rej"] == 0
    assert m["m27gap"]["n"] == 0
    assert m["m28a"]["n"] == 0


def test_threshold_sweep_recomputes_from_the_same_rows() -> None:
    """저장된 원자료에 임계값만 바꿔 다시 적용할 수 있다 (운영점 선택)."""
    plain = _plain_easy_id()
    m = harness.compute_metrics([_reject_row(plain)], threshold=2)
    assert m["sweep"][2]["m27b_k"] == 1
    assert m["sweep"][3]["m27b_k"] == 0, "severity 2 가 임계값 3 에서도 적중으로 셌다"


def test_fallback_rows_are_excluded_from_every_metric() -> None:
    """룰 폴백은 '검토기가 뭘 했나' 에 대해 아무것도 말하지 않는다."""
    ctl = next(r["case_id"] for r in harness.load_cases() if r["block"] == "defect_free_control")
    m = harness.compute_metrics(
        [{"case_id": ctl, "block": "defect_free_control", "repeat": 0, "fell_back": True}],
        threshold=2,
    )
    assert m["n_usable"] == 0
    assert m["n_fallback"] == 1
    assert "m29" not in m


# ── 7. 변수 계약 — 파생값까지 막혔는가 ─────────────────────────────────────


def test_estimated_minutes_never_reaches_the_verifier() -> None:
    """③층이 정한 분량은 카드 필드로도 넘어가면 안 된다 (루브릭 §1.2).

    변수 이름만 지우면 `action_items_json` 안의 `estimated_minutes` 로 같은 판단이 가능해
    §1.2 의 강제("변수 자체를 안 넘긴다")가 파생값에서 깨진다. 그리고 이 필드가 동시에
    M28b 위치 누출(`argmin(분량)`)의 원인이다.
    """
    for case in harness.load_cases():
        payload = harness.review_variables_v4(case)["action_items_json"]
        assert "estimated_minutes" not in payload, f"{case['case_id']} 에 분량이 남아 있다"
        items = json.loads(payload)
        assert items, "카드가 통째로 사라졌다"
        for item in items:
            assert "node_id" in item and "title" in item and "first_step" in item


def test_smoke_limit_reaches_the_control_block() -> None:
    """`--limit` 이 head 슬라이스면 M29 경로가 스모크에서 한 번도 안 돈다.

    골든셋 파일 순서상 `seeded_defect` 가 앞이라, 단순 슬라이스는 대조군 30건을 건너뛴다.
    **사전등록의 유일한 절대 임계값이 본 실행에서 처음 도는 코드가 된다.**
    """
    blocks = {c["block"] for c in harness.load_cases(limit=2)}
    assert blocks == {"defect_free_control", "seeded_defect"}


def test_harness_uses_the_schema_module_for_approval() -> None:
    """반려 판정을 하네스가 다시 구현하지 않는다 — 두 곳에 두면 갈린다."""
    src = (_ROOT / "scripts" / "l1_7b_v4_run.py").read_text(encoding="utf-8")
    assert "approved_from_findings(findings, threshold=threshold)" in src
