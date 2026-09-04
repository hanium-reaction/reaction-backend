"""M26-core 집계를 불변식으로 고정한다 (실험계획서 §5 「M26 통과 조건」).

⚠️ **집계 함수가 검증 밖에 있으면 분모·N/A·제외 규칙을 동시에 뒤집어도 전 테스트가
초록이 된다.** L1-7B v4 하네스에서 실제로 그랬고, 독립 검토가 뮤테이션으로 증명했다.
그래서 `summarize_core` 를 순수 함수로 분리하고 여기서 직접 부른다.

이 파일이 지키는 것:

1. **M18 은 AND 에 없다** — 연속량을 이진화하면 임계값 하나가 M26 전체를 흔든다.
2. **N/A 는 중립** — 실패도 통과도 아니고, 케이스는 분모에 남는다.
3. **빈 AND 를 통과로 두지 않는다** — "아무것도 안 재고 통과" 경로를 막는다.
4. **1차 추정은 `PRIMARY_REPEAT`** — 반복을 독립 표본으로 세지 않는다.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts import l1_7_run as H
from scripts.l1_7_schedule_eval import NA, _NotApplicable


def _row(**kw: Any) -> dict[str, Any]:
    """M17·M19·M25 통과, 마일스톤 없음(M23·M24 N/A) 인 기본 행."""
    base: dict[str, Any] = {
        "case_id": "c1",
        "repeat": 0,
        "m17_over_ceiling": 0,
        "m17_under_floor": 0,
        "m19_truncated": 0,
        "m25_waiting": 0,
        "m24_out_of_cycle": 0,
        "m24_measurable": False,
    }
    return {**base, **kw}


def _sched(m20: Any = True, m21: Any = True, m22: Any = True) -> dict[str, Any]:
    return {"m20": m20, "m21": m21, "m22": m22}


# ── 1. M18 은 M26-core 에 없다 ──────────────────────────────────────────────


def test_m18_is_not_part_of_the_core_and() -> None:
    """M18 이 아무리 나빠도 M26-core 판정을 바꾸지 못한다."""
    assert "m18" not in H.CORE_METRICS
    assert set(H.CORE_METRICS) == {"m17", "m19", "m20", "m21", "m22", "m23", "m24", "m25"}
    good = _row(m18a_ratio=0.2, m18b_ratio=0.2)  # 심한 과소 생성
    verdict, _ = H.m26_core(H.core_verdicts(good, _sched()))
    assert verdict is True, "M18 이 AND 에 새어 들어갔다"


# ── 2. N/A 는 중립 ──────────────────────────────────────────────────────────


def test_na_does_not_fail_the_and() -> None:
    """마일스톤이 없어 M23 이 N/A 여도 나머지가 통과면 통과다."""
    v = H.core_verdicts(_row(), _sched())
    assert v["m23"] is NA and v["m24"] is NA
    verdict, applied = H.m26_core(v)
    assert verdict is True
    assert applied == 6, "N/A 두 개가 적용 수에 들어갔다"


def test_na_does_not_pass_the_and_either() -> None:
    """N/A 가 통과로 세지면 적용 수가 부풀려진다."""
    v = H.core_verdicts(_row(), _sched())
    _, applied = H.m26_core(v)
    assert applied < len(H.CORE_METRICS)


def test_milestone_case_applies_m23() -> None:
    """마일스톤이 있으면 M23 이 적용된다 — 누락 0 이면 통과."""
    v = H.core_verdicts(_row(m23_window=2, m23_missing=0), _sched())
    assert v["m23"] is True
    v_bad = H.core_verdicts(_row(m23_window=2, m23_missing=1), _sched())
    assert v_bad["m23"] is False
    assert H.m26_core(v_bad)[0] is False


def test_m24_is_na_when_drift_is_impossible() -> None:
    """창이 남은 마일스톤 전부를 덮으면 이탈이 **원리적으로 불가능**하다 — 미측정."""
    v = H.core_verdicts(_row(m24_measurable=False, m24_out_of_cycle=0), _sched())
    assert v["m24"] is NA
    v2 = H.core_verdicts(_row(m24_measurable=True, m24_out_of_cycle=0), _sched())
    assert v2["m24"] is True


def test_missing_schedule_makes_placement_metrics_na_not_fail() -> None:
    """배치를 안 돌렸으면 M20·M21·M22 는 **N/A** 다 — 실패가 아니다."""
    v = H.core_verdicts(_row(), None)
    assert v["m20"] is NA and v["m21"] is NA and v["m22"] is NA
    verdict, applied = H.m26_core(v)
    assert verdict is True  # 나머지 셋이 통과하므로
    assert applied == 3


# ── 3. 빈 AND 를 통과로 두지 않는다 ─────────────────────────────────────────


def test_all_na_is_not_a_pass() -> None:
    """적용된 지표가 하나도 없으면 **N/A** 다.

    빈 AND 를 참으로 두는 것이 "아무것도 안 재고 통과" 를 만드는 경로다.
    """
    v = dict.fromkeys(H.CORE_METRICS, NA)
    verdict, applied = H.m26_core(v)
    assert isinstance(verdict, _NotApplicable)
    assert applied == 0


def test_one_failure_fails_the_whole_case() -> None:
    """AND 다 — 하나라도 실패면 그 계획은 실패다(macro 원칙)."""
    v = H.core_verdicts(_row(m17_over_ceiling=1), _sched())
    assert H.m26_core(v)[0] is False


# ── 4. 집계 — 분모·적용 수·반복 규칙 ────────────────────────────────────────


def test_summary_reports_applicable_and_na_per_metric() -> None:
    """지표마다 **적용 사례 수**를 함께 낸다 (§5 규칙).

    "M23 통과율 1.00" 만 실으면 독자가 34건으로 읽는다 — 실제 적용은 6건이다.
    """
    rows = [_row(case_id="a"), _row(case_id="b", m23_window=1, m23_missing=0)]
    s = H.summarize_core(rows, {"a": _sched(), "b": _sched()})
    assert s["per_metric"]["m23"] == {"pass": 1, "fail": 0, "na": 1}
    assert s["per_metric"]["m17"] == {"pass": 2, "fail": 0, "na": 0}


def test_summary_uses_only_the_primary_repeat() -> None:
    """반복을 독립 표본으로 세지 않는다 — L1-7B 가 M29 에 쓰는 규칙과 같다."""
    rows = [
        _row(case_id="a", repeat=0),
        _row(case_id="a", repeat=1, m17_over_ceiling=9),  # 다른 회차의 실패
    ]
    s = H.summarize_core(rows, {"a": _sched()})
    assert s["n_cases"] == 1
    assert s["pass"] == 1 and s["fail"] == 0


def test_summary_breaks_down_by_applied_count() -> None:
    """적용 지표 수가 다른 케이스를 **한 비율로 뭉개지 않는다.**"""
    rows = [
        _row(case_id="a"),  # 6개 적용
        _row(case_id="b", m23_window=1, m23_missing=0, m24_measurable=True),  # 8개
    ]
    s = H.summarize_core(rows, {"a": _sched(), "b": _sched()})
    assert set(s["by_applied"]) == {6, 8}
    assert s["by_applied"][6]["pass"] == 1
    assert s["by_applied"][8]["pass"] == 1


def test_all_na_case_stays_in_the_denominator_as_na() -> None:
    """전 지표 N/A 인 케이스는 통과·실패 어느 쪽으로도 안 센다."""
    rows = [_row(case_id="a", m17_over_ceiling=0)]
    # 배치 없음 + 마일스톤 없음이어도 M17·M19·M25 는 남으므로, 억지로 전부 N/A 를 만든다.
    v = dict.fromkeys(H.CORE_METRICS, NA)
    verdict, _ = H.m26_core(v)
    assert isinstance(verdict, _NotApplicable)
    s = H.summarize_core(rows, {})
    assert s["pass"] + s["fail"] + s["na"] == s["n_cases"]


@pytest.mark.parametrize("metric", ["m20", "m21", "m22"])
def test_placement_failure_fails_the_core(metric: str) -> None:
    """배치 지표 실패도 AND 를 떨어뜨린다 — 제약만의 지표가 아니다."""
    s = _sched(**{metric: False})  # type: ignore[arg-type]
    assert H.m26_core(H.core_verdicts(_row(), s))[0] is False
