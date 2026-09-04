"""M33·M34 3-arm 설계가 **결과를 보고 바뀌지 않게** 고정한다.

사전등록의 목적은 "결과를 본 뒤 유리한 쪽을 고르는 것" 을 막는 데 있다. 문서만 두면
조용히 바뀌고, 바뀐 사실을 아무도 모른다 — 이 세션에서 문서가 낡는 것을 **네 번** 겪었다.

⚠️ 이 파일은 **실험을 실행하지 않는다.** 설계 문서의 핵심 조항이 살아 있는지만 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DOC = _ROOT / "docs" / "experiments" / "m33-3arm-design.md"

# 격자가 **범위가 아니라 정확한 값**이라는 조항 — 이게 빠지면 나중에 값을 고를 여지가 남는다.
B_EXACT = "`>=` · `<=` 를 남기지 않는다"


def _body() -> str:
    return _DOC.read_text(encoding="utf-8")


def test_design_doc_exists() -> None:
    assert _DOC.exists(), "3-arm 설계 문서가 사라졌다"


# ── 1. 세 arm 의 정의 ───────────────────────────────────────────────────────


def test_three_arms_share_one_initial_plan() -> None:
    """초기 분해를 arm 마다 새로 돌리면 ④층 효과와 분해기 변동이 섞인다."""
    body = _body()
    assert "동일한 초기 계획을 세 arm 이 공유한다" in body


@pytest.mark.parametrize(
    ("arm", "mark"),
    [
        ("없음", "**안 한다** — 초기 계획 그대로"),
        ("상세 피드백", "검토기가 낸 `feedback[]` 전문"),
        ("빈 피드백", "**비운다**"),
    ],
)
def test_each_arm_has_an_exact_definition(arm: str, mark: str) -> None:
    """arm 의 차이가 **한 가지**로 특정돼 있어야 한다."""
    body = _body()
    assert arm in body and mark in body, f"{arm} arm 의 정의가 흐려졌다"


def test_b_and_c_differ_only_in_the_feedback_variable() -> None:
    """B·C 는 `review_feedback` **하나만** 다르다 — 나머지가 다르면 M34 가 오염된다."""
    body = _body()
    assert "`review_feedback` 변수 **하나만** 다르다" in body


def test_arm_a_still_calls_the_verifier() -> None:
    """A 는 검토기를 **부르되 재분해만 안 한다.**

    아예 안 부르면 어느 케이스가 반려됐는지 몰라 **M30 의 분모가 정의되지 않는다.**
    """
    body = _body()
    assert "검토기는 **부르되 그 결과로 재분해하지 않는다.**" in body
    assert "M30 의 분모" in body


def test_replan_happens_only_on_rejection() -> None:
    """승인이면 **B·C 도 재분해하지 않는다** — 프로덕션 `should_replan` 과 같은 흐름이어야
    실제 제품과 같은 비교가 된다."""
    body = _body()
    assert '재분해는 **공통 검토 결과가 "반려" 일 때만** 일어난다' in body
    assert "B·C 도 재분해하지 않고 최초 계획을 보존한다" in body
    assert "승인 케이스는 세 arm 이 같으므로" in body


def test_paired_bootstrap_is_fixed_in_advance() -> None:
    """CI 산출법을 안 적으면 나중에 유리한 방법을 고를 여지가 남는다."""
    body = _body()
    assert "케이스 단위 페어드 부트스트랩" in body
    assert "10,000회" in body and "양측 95%" in body and "시드 **42**" in body
    assert "행(계획)이 아니라 케이스를 리샘플한다" in body
    assert "섞어서 한 구간을 만들지 않는다" in body


def test_m34_is_a_separate_run_not_a_follow_up_measurement() -> None:
    """v3 → v4 로 바뀌면 **반려 판정 자체가 달라져** 재분해 대상 집합이 같지 않다."""
    body = _body()
    assert "M34 는 같은 실험의 후속 측정이 아니라 별도 실행이다" in body
    assert "케이스 단위로 대조하지 않는다" in body


def test_stratum_generator_is_committed() -> None:
    """격자가 **문서가 아니라 코드**로 있어야 재현된다."""
    assert (_ROOT / "scripts" / "build_challenge_stratum.py").exists()
    assert (_ROOT / "eval" / "golden_challenge_stratum.jsonl").exists()
    body = _body()
    assert "build_challenge_stratum.py" in body


def test_grid_coverage_is_not_presented_as_causal_axis_effect() -> None:
    """축별 표를 **인과적 "축 효과" 로 읽지 말라**는 경고가 있는가.

    `weekly_hours` 반올림 때문에 경계 축을 바꾸면 **주당 볼륨과 파생 세션 길이도 함께
    바뀐다.** 한 축을 단독 원인으로 귀속할 수 없다.
    """
    body = _body()
    assert "격자 커버리지 확인" in body
    assert '인과적인 "축 효과" 로 읽으면 안 된다' in body
    assert "한 축을 단독 원인으로 귀속할 수 없다" in body


def test_rounding_policy_is_stated_and_the_overclaim_withdrawn() -> None:
    """**반올림 정책**과 그것이 축을 섞는다는 사실이 적혀 있는가.

    초판은 *"세션 길이를 빈도와 무관하게 일정하게 만든다"* 고 썼는데 **hard 쪽에서만
    성립한다** — easy(90분)는 4.5h→4, 10.5h→10 으로 반올림돼 파생 세션이 80·86분이 된다.
    """
    body = _body()
    assert "round(frequency × session_length ÷ 60)" in body
    assert "반올림 때문에 축이 깨끗하게 분리되지 않는다" in body
    assert "초판이" in body and "**과장이었다**" in body
    # 정확한 표현이 남아 있는가.
    assert "`frequency_per_week` 는 **케이던스**" in body
    assert "`weekly_hours` 는" in body and "**근사적인 볼륨**" in body
    assert "완전히 직교하지 않는다" in body


def test_dead_axis_is_kept_and_recorded() -> None:
    """**경계 축을 빼지 않는다** — 빼는 것도 사후 조정이다."""
    body = _body()
    assert "경계 축은 이 격자에서 M20·M21 을 갈라내지 못했다" in body
    # 문서에서 줄바꿈이 어디 오든 잡히게 공백을 지우고 본다.
    assert "뺀다면그것도사후조정" in "".join(body.split())


def test_the_absolute_window_axis_error_is_recorded() -> None:
    """초판이 활동창을 **절대 시간**으로 적어 변별력이 0이었던 사실."""
    body = _body()
    assert "초판의 활동창 축은 틀렸다" in body
    assert "활동창은 세션 길이보다 짧을 때만 물린다" in body
    assert "하드코딩" in body


# ── 2. 실행 순서 ────────────────────────────────────────────────────────────


def test_arms_are_interleaved_not_batched() -> None:
    """한 arm 을 몰아 돌리면 시간·모델 변동이 그 arm 에만 실린다."""
    body = _body()
    assert "**한 arm 을 몰아서 돌리지 않는다.**" in body
    assert "case1-A · case1-B · case1-C" in body


# ── 3. challenge stratum — 결과를 보고 고르지 않는다 ────────────────────────


def test_challenge_stratum_is_defined_by_inputs_not_results() -> None:
    """**실패한 7건을 골라 쓰면 안 된다** — 그건 결과를 보고 표본을 고르는 것이다."""
    body = _body()
    assert "실패한 7건을 골라 쓰면 안 된다" in body
    assert "어떤 케이스가 실패했는지 **모르는 상태에서도 같은 집합이 나와야 한다.**" in body
    assert "결과가 아니라 슬롯 값만으로 결정된다" in body


@pytest.mark.parametrize("axis", ["**경계**", "**빈도**", "**활동창**", "**마일스톤**"])
def test_all_four_stratum_axes_are_registered(axis: str) -> None:
    """네 축이 전부 남아 있는가 — 하나 빠지면 격자가 달라진다."""
    assert axis in _body(), f"challenge stratum 의 축 '{axis}' 가 사라졌다"


def test_stratum_size_is_fixed_in_advance() -> None:
    body = _body()
    assert "16건" in body
    assert B_EXACT in body


def test_ceiling_effect_is_recorded() -> None:
    """왜 stratum 이 필요한가 — 천장 효과가 근거다."""
    body = _body()
    assert "최대 7/34 = 0.206" in body
    assert "천장 효과" in body


def test_general_and_challenge_are_reported_separately() -> None:
    """난이도가 다른 표본을 섞으면 '우리가 고른 난이도 배합' 을 재게 된다."""
    body = _body()
    assert "둘을 합쳐 단일 성과 수치를 만들지 않는다" in body
    assert "일반 34건 / challenge 16건 두 벌" in body


# ── 4. 보고 규칙 ────────────────────────────────────────────────────────────


def test_m26core_and_m18_are_reported_together() -> None:
    body = _body()
    assert "M26-core 와 M18 을 항상 나란히 낸다" in body


def test_pairing_is_required_for_the_delta() -> None:
    """한 arm 에서만 N/A 가 되면 Δ 가 서로 다른 케이스 집합의 차가 된다."""
    body = _body()
    assert "세 arm 모두에서 M26-core 가 정의된 케이스만" in body


def test_repeat_rule_matches_the_rest_of_the_project() -> None:
    body = _body()
    assert "PRIMARY_REPEAT" in body
    assert "반복을 독립 표본으로" in body


# ── 5. 해석 규칙 — 결과를 보기 전에 고정 ────────────────────────────────────


def test_negative_result_leads_to_removing_the_layer_not_tuning_it() -> None:
    """M33 ≤ 0 의 결론이 사전에 박혀 있는가.

    이걸 미리 안 적으면 음수가 나왔을 때 "프롬프트를 더 다듬자" 로 흘러간다.
    """
    body = _body()
    assert "이 층을 룰로 대체하거나 걷어내자" in body


def test_approval_rate_is_not_a_quality_metric() -> None:
    """검토기가 자기 판정으로 자기를 채점하는 구조를 막는다."""
    body = _body()
    assert "검토기는 **측정 도구가 아니라 측정 대상**이다" in body
    assert "승인율 상승을 수렴 증거로 쓰지 않는다" in body


def test_undetermined_sign_does_not_get_forced() -> None:
    body = _body()
    assert "부호를 억지로 만들지 않는다" in body


# ── 6. 실행 전 blocker 가 정직하게 적혀 있는가 ──────────────────────────────


def test_blockers_distinguish_m33_from_m34() -> None:
    """**M33 은 v3 로 나오고 M34 는 v4 가 필요하다** — 리드타임이 다르다."""
    body = _body()
    assert "M33 은 v3 로도 나온다" in body
    assert "M34·M30·M31 은 v4 가 필요하다" in body
    assert "M34 를 안 낸 사실을 함께 적는다" in body


# ── 7. 재집계 조건 · 검토기 폴백 (2026-09-04 추가) ──────────────────────────


def test_reaggregation_guard_covers_more_than_the_base_date() -> None:
    """**기준일 고정은 절반이다** — 재집계는 골든을 그 시점에 다시 읽는다."""
    body = _body()
    assert "기준일을 고정해도 **절반밖에 못 막는다.**" in body
    assert "원자료를 한 글자도 안 바꿔도 골든이 바뀌면" in body
    assert "`M33 = -0.0625` 에서 `+0.0000`" in body


@pytest.mark.parametrize(
    "clause",
    [
        "manifest 의 `golden_sha256` = 현재 골든 해시",
        "모든 행에 `target_date` 가 있고 하나뿐이다",
        "manifest 의 `limit` 이 비어 있다(= 스모크가 아니다)",
        "케이스가 중복되지 않는다",
    ],
)
def test_each_reaggregation_check_is_registered(clause: str) -> None:
    """조항을 하나 빼는 것도 사후 조정이다."""
    assert clause in _body(), f"재집계 확인 조항 '{clause}' 가 사라졌다"


def test_case_is_the_sampling_unit_is_enforced_not_just_stated() -> None:
    """문서가 "케이스 단위" 라고 적어도 **코드가 막지 않으면** 소용없다."""
    body = _body()
    assert "**코드가 막지 않으면**" in body
    assert "`[-0.1875, 0]` → 32행 `[-0.1562, 0]`" in body


def test_review_fallback_is_not_counted_as_approval() -> None:
    """검토 LLM 타임아웃이 "승인" 으로 집계되면 **M33 이 0 쪽으로 편향**된다."""
    body = _body()
    assert "검토기 폴백은 **승인이 아니다**" in body
    assert "Δ 가 0 이 아닌 **유일한** 집합" in body
    assert "`review_fell_back`" in body
