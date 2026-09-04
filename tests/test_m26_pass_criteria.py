"""M26 통과 조건이 조용히 표류하지 않게 고정한다 (실험계획서 §5).

M26 은 L1-7A 의 **1차 지표**인데 M17~M25 의 "통과" 임계값이 없어 1차 실행이 산출하지
못했다. §5 에 조건을 등록하면서 그 조건이 **프로덕션 판정에 의존**하게 됐다.
그 판정이 사라지거나 이름이 바뀌면 등록된 조건이 계산 불가가 되는데, **문서는 여전히
"M17~M25 전부" 라고 적혀 있을 것이다.** 그 상태가 가장 나쁘다.

⚠️ **2026-09-03 정정 반영.** 이 파일의 초판은 M18 의 "반-세션 규칙" 을 지키고 있었는데,
독립 검증이 그 유도를 무너뜨려 M18 은 **미정**이 됐다. 초판 테스트를 그대로 뒀으면
**틀린 규칙을 테스트가 지켜 주는** 상태가 됐을 것이다.

⚠️ 이 테스트는 **M26 값을 계산하지 않는다.** 계산에는 M18 결정 · v2 재실행 · 스케줄러
경로가 아직 필요하다(§5 「M26 을 내려면 아직 남은 것」).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from reaction_backend.orchestrator import first_plan, first_plan_adapter

_ROOT = Path(__file__).resolve().parent.parent
_PLAN = _ROOT / "docs" / "experiments" / "experiment-plan-v1.md"

# §5 표의 각 행이 이름으로 지목한 프로덕션 판정 — (지표, 모듈, 속성).
#
# ⚠️ `cadence_shortfall_notice`·`horizon_coverage_notice` 는 **일부러 빠졌다.**
# 정정 후 M20·M22 는 그 함수들을 쓰지 않는다 — 전자는 UX 슬랙 `0.8` 을 상속하고,
# 후자는 **발화 자체가 결함이 아니다**(두 갈래 모두 정상 상황이라고 docstring 이 적는다).
_CRITERION_BACKING = [
    ("M17", first_plan_adapter, "session_min_for"),  # 밴드 상한
    ("M17", first_plan_adapter, "_MIN_ACTION_MINUTES"),  # 밴드 하한
    ("M18", first_plan_adapter, "horizon_minute_budget"),  # 임계값은 미정이나 기준량은 이것
    ("M19", first_plan_adapter, "_take_within_budget"),  # 초판이 빠뜨렸던 자리
    ("M21", first_plan, "_UNPLACED_MARKER"),
    ("M22", first_plan_adapter, "_MAX_PLAN_WEEKS"),  # 덮어야 할 날을 자르는 상한
    ("M23", first_plan_adapter, "missing_milestone_titles"),
    ("M24", first_plan_adapter, "drop_out_of_cycle_branches"),
    ("M24", first_plan_adapter, "cycle_milestone_window"),  # 카브아웃 조건의 근거
    ("M25", first_plan_adapter, "_WAITING_TITLE_RE"),
]


def _body() -> str:
    return _PLAN.read_text(encoding="utf-8")


@pytest.mark.parametrize(("metric", "module", "attr"), _CRITERION_BACKING)
def test_registered_criterion_still_has_its_production_judge(
    metric: str, module: object, attr: str
) -> None:
    assert hasattr(module, attr), (
        f"{metric} 의 통과 조건이 지목한 `{attr}` 가 사라졌다 — "
        "실험계획서 §5 의 M26 통과 조건을 함께 고쳐야 한다"
    )


# ── 정정이 지워지지 않게 ────────────────────────────────────────────────────


def test_m18_is_out_of_the_m26_core_and_reported_beside_it() -> None:
    """M18 은 **M26-core 의 AND 에 없다** — 주지표 둘을 나란히 둔다.

    M17·M19~M25 는 "위반이 있나 없나" 라 이진 판정이 자연스럽지만, M18 은 **"얼마나
    벗어났나"** 라 본래 연속량이다. 이진화하려면 임계값이 필요한데 그걸 정당화할 근거가
    없다 — 연속량을 억지로 AND 에 넣으면 **임계값 하나가 M26 전체를 흔든다.**
    """
    body = _body()
    assert "M26-core" in body
    assert "**M18 을 M26 의 AND 에서 뺀다.**" in body
    assert "둘 다 주지표다" in body
    # 항목별 표에 M18 행이 있으면 안 된다.
    start = body.index("#### 항목별 통과 조건 (M26-core)")
    table = body[start : body.index("####", start + 10)]
    assert "**M18**" not in table, "M18 이 아직 M26-core 표에 있다"
    assert "**M26-core = 위 8개의 AND.**" in body


def test_m18_threshold_is_recorded_as_a_product_policy_not_a_derivation() -> None:
    """반-세션 규칙이 **유도가 아니라 제품 정책**임을 문서가 말하는가.

    leaf 길이는 `planned_session_min_for` 의 **정수배로 만들어지지 않는다** —
    `normalize_action_minutes` 는 `[15분, 집중용량]` 밴드로 클램프만 하고, ADR-0009 D2 가
    *"평균을 상한으로 쓰면 길이가 내용을 따라갈 수 없다"* 며 일부러 입도를 없앴다.
    """
    body = _body()
    # 규칙 문자열이 문서에 **있는 것 자체는 정당하다** — 철회를 설명하려면 인용해야 한다.
    # 막아야 하는 것은 그것이 **살아 있는 기준으로 되돌아오는 것**이다.
    start = body.index("#### 항목별 통과 조건 (M26-core)")
    table = body[start : body.index("####", start + 10)]
    assert "planned_session_min_for / 2" not in table, "철회된 반-세션 규칙이 기준표로 되살아났다"
    assert "**유도가 아니었다.**" in body
    assert "팀이 고르는 제품 정책이다" in body
    assert "정수배로" in body
    # 관측 데이터로 정책을 확정하지 말라는 경고.
    assert "지금 관측한 데이터로 그" in body


def test_m18_is_reportable_today_without_a_threshold() -> None:
    """임계값이 없어도 M18 은 **분포로 완전히 보고된다** — 미정이 곧 미보고가 아니다."""
    body = _body()
    assert "임계값 없이도 완전히 계산된다" in body
    assert "M18a" in body and "M18b" in body


def test_the_three_withdrawn_claims_stay_withdrawn() -> None:
    """철회한 세 주장이 되살아나지 않는가."""
    body = _body()
    assert "초판을 철회한다" in body
    # ① "임계값을 고르지 않았다" 과장
    assert '"임계값을 고르지 않았다" 는 주장을 철회한다' in body
    # M18 이 M26-core 에서 빠지면서 "미정 1" 이 사라졌다 — 이제 유도 6 · 선택 2 뿐이다.
    assert "**유도 6 · 선택 2.**" in body
    assert "미정 1" not in body, "M18 이 다시 M26-core 안의 미정 항목으로 돌아갔다"
    # ② M22 함수 오용
    assert "의도된 동작을 결함으로 셌다" in body
    # ③ M18 입도 유도
    assert "또 다른 임의 밴드" in body


def test_inherited_thresholds_are_named_not_hidden() -> None:
    """상속하던 임계값 두 개를 이름으로 적었는가.

    `0.8` 은 결과 문서가 **1차 실행의 결함으로 직접 지목한 숫자**다. 그게 프로덕션 함수를
    경유해 뒷문으로 들어오는 것을 적어 두지 않으면 아무도 모른다.
    """
    body = _body()
    assert "_CADENCE_OK_RATIO = 0.8" in body
    assert "1차 실행의 결함 #2 로 직접 지목한 숫자" in body


def test_honesty_disclosure_lists_all_six_seen_metrics() -> None:
    """무엇을 봤는지 축소하지 않았는가.

    초판은 M17·M18·M19·M25 넷만 적었는데 결과 문서는 M23(0.000)·M24(0.000)도 보고한다 —
    **하필 "0건" 기준을 이미 만족한다고 아는 둘**이라 가장 방어가 필요한 자리였다.
    """
    body = _body()
    assert "M17·M18·M19·M23·M24·M25" in body
    assert "완전한 사전등록이 아니다" in body
    # 계측기 자체가 M18 관측을 보고 조정됐다는 사실도 남아 있어야 한다.
    assert "1e76779" in body


def test_na_and_unmeasured_are_separated() -> None:
    """**"해당 없음(N/A)" 과 "미측정" 은 다르다** — 이 절에서 가장 중요한 구분.

    마일스톤이 없는 28건은 데이터가 빠진 게 아니라 **애초에 지킬 마일스톤이 없다.**
    실패로 세면 부당하고, 케이스를 통째로 빼면 **M26 이 사실상 마일스톤 6건만의 지표**가
    된다. 반대로 스케줄러 부재는 진짜 도구 부재라 산출 자체를 막아야 한다.
    """
    body = _body()
    assert '"해당 없음" 과 "미측정" 은 다르다' in body
    assert "N/A (해당 없음)" in body
    # N/A 는 중립 — 케이스는 분모에 남는다.
    assert "AND 에서 빠지고, 케이스는 분모에 **남는다**" in body
    # 미측정은 산출 자체를 막는다.
    assert "M26-core 를 산출하지 않는다" in body
    # M23 의 N/A 규모를 숫자로 적었는가.
    assert "28/34 이 N/A" in body
    assert "적용 사례 수" in body
    # M24 카브아웃 조건 오인용 정정은 유지.
    assert "can_refill" in body and "한 건도 못 걷어낸다" in body


def test_m23_is_conditional_not_a_blocker() -> None:
    """M23 분모 0 은 blocker 가 아니라 N/A 처리 대상이다."""
    body = _body()
    assert "blocker 가 아니라 **N/A 처리 대상**" in body or "N/A 처리 대상" in body


def test_repeat_aggregation_rule_is_registered() -> None:
    """반복 3회를 어떻게 접는지 — 초판에 없던 자유도.

    34개 중 9개가 회차마다 M17 판정이 갈리므로 이 규칙 하나로 M26 이 크게 움직인다.
    """
    body = _body()
    assert "PRIMARY_REPEAT = 0" in body
    assert "반복을 독립 표본으로 세면" in body


def test_remaining_blocker_is_wiring_not_a_metric_gap() -> None:
    """M26-core 를 막는 것은 이제 **지표가 아니라 배선**이다.

    초판이 blocker 로 적은 것은 전부 해소됐다 — v2 재실행(#417) · M23 N/A 처리 ·
    M18 분리 · 배치 경로(#418). 남은 것은 `l1_7_schedule_eval`(배치)과 `l1_7_run`(분해)이
    **따로 돌아** M17~M25 를 같은 계획 위에서 못 낸다는 것뿐이다.

    ⚠️ 이 구분이 중요하다 — "지표를 더 설계해야 한다" 와 "코드를 합치면 된다" 는 다른 일이고,
    남은 것을 지표 문제로 적어 두면 다음 사람이 설계를 다시 연다.
    """
    body = _body()
    assert "⬜ **유일한 blocker (배선)**" in body
    assert "남은 것은 지표 문제가 아니라 배선이다" in body
    assert "같은 계획 위에서" in body
    # 해소된 넷이 이름으로 적혀 있는가.
    for done in ("#417", "N/A 처리 대상", "M26-core 에서 뺐다", "#418"):
        assert done in body, f"해소 항목 '{done}' 이 사라졌다"
    assert "부분 집합의 AND 를 M26-core 라 부르지 않는다" in body


def test_m33_records_three_arms_and_pairing_and_floor_effect() -> None:
    """M33 절 — arm 수·페어링·바닥 효과."""
    body = _body()
    assert "arm 은 셋이다" in body, "초판의 '두 arm' 오기가 되살아났다"
    assert "M18 은 M33 에 안 들어간다" in body
    assert "서로 다른 케이스 집합의 차" in body, "분모 페어링 요구가 사라졌다"
    assert "바닥 효과 경고" in body


def test_m26_core_covers_every_metric_except_m18() -> None:
    """M26-core 표에 **M18 을 뺀 여덟 개**가 전부 있는가. 하나라도 비면 AND 가 정의되지 않는다."""
    body = _body()
    start = body.index("#### 항목별 통과 조건 (M26-core)")
    table = body[start : body.index("####", start + 10)]
    for n in [17, 19, 20, 21, 22, 23, 24, 25]:
        assert re.search(rf"\*\*M{n}\*\*", table), f"M{n} 의 통과 조건이 M26-core 표에 없다"
    assert not re.search(r"\*\*M18\*\*", table), "M18 은 M26-core 에 들어가면 안 된다"


def test_chosen_criteria_are_marked_as_chosen() -> None:
    """M20·M22 는 **선택**으로 표시돼야 한다 — 유도인 척하면 안 된다."""
    body = _body()
    start = body.index("#### 항목별 통과 조건")
    table = body[start : body.index("####", start + 10)]
    assert table.count("**선택**") >= 2, "선택한 기준이 선택으로 표시돼 있지 않다"
    assert "슬랙 없음" in table
