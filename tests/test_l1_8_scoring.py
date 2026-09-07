"""L1-8 하네스의 채점이 맞는가 — **LLM 없이** 합성 계획으로 검증한다.

이 하네스의 지표(M36~M39)는 사람 라벨도 심판 LLM 도 안 쓰고 계획 JSON 에서 결정적으로
나온다. 그러면 채점 코드는 실행 없이 테스트할 수 있고, **테스트해야 한다** — 채점이 조용히
틀리면 42 × 반복 회의 실 LLM 호출이 통째로 못 쓰는 숫자가 된다(L1-7 1차가 집계만 저장해
재감사가 불가능했던 것과 같은 계열의 사고).

특히 못 박는 것:
- **짝 단위 뺄셈** — 블록 평균끼리 빼면 안 된다. 목표마다 세션 길이가 다르므로 블록에
  빠진 케이스가 하나만 있어도 평균 차이가 이력의 효과로 둔갑한다.
- **누출은 이력이 실린 케이스만 분모** — 대조군은 애초에 누출될 문구가 없다.
- **폴백 행은 채점에서 빠진다** — `score` 가 없으면 집계가 건너뛴다.
"""

from __future__ import annotations

from typing import Any

from scripts.l1_8_run import CONTROL_BLOCK, plan_text, score_case, summarize


def _plan(*items: tuple[str, int, str]) -> dict[str, Any]:
    return {
        "goal_nodes": [{"title": "루트"}],
        "action_items": [
            {"title": t, "estimated_minutes": m, "first_step": f} for t, m, f in items
        ],
    }


def _case(block: str, pair: str, *, must_not_contain: list[str]) -> dict[str, Any]:
    return {
        "case_id": f"{block}-{pair}",
        "block": block,
        "pair_id": pair,
        "assertions": {"must_not_contain": must_not_contain},
    }


def _row(case: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "block": case["block"],
        "pair_id": case["pair_id"],
        "case": case,
        "score": score_case(case, plan),
    }


def test_plan_text_covers_everything_the_user_reads() -> None:
    """제목만 보면 안 된다 — `first_step` 에 이력 문구가 새는 게 더 흔하다."""
    text = plan_text(_plan(("정규화 복습", 50, "교재 3장 펴기")))
    assert "정규화 복습" in text
    assert "교재 3장 펴기" in text
    assert "루트" in text


def test_score_case_counts_minutes_and_catches_leaks() -> None:
    case = _case("failure_goal_scoped", "sqld", must_not_contain=["계획이 너무 컸어요"])
    leaked = _plan(("계획이 너무 컸어요 — 이번엔 작게", 30, "시작"))
    score = score_case(case, leaked)
    assert score["mean_minutes"] == 30
    assert score["sessions"] == 1
    assert score["leaked"] == ["계획이 너무 컸어요"]

    clean = score_case(case, _plan(("정규화 복습", 40, "교재 펴기"), ("조인 연습", 20, "문제 3개")))
    assert clean["mean_minutes"] == 30  # (40 + 20) / 2
    assert clean["leaked"] == []


def test_score_case_flags_blame_only_when_present() -> None:
    case = _case("failure_goal_scoped", "sqld", must_not_contain=["계획이 너무 컸어요"])
    assert score_case(case, _plan(("자꾸 미루던 부분부터", 30, "시작")))["blamed"] == ["자꾸"]
    assert score_case(case, _plan(("정규화 복습", 30, "시작")))["blamed"] == []


def test_m36_subtracts_within_a_pair_not_across_block_means() -> None:
    """짝 단위 뺄셈 — 목표마다 세션 길이가 다르므로 블록 평균끼리 빼면 거짓말이 된다.

    여기서 대조군은 sqld 30분 / thesis 90분이고 처치군은 각각 20분 / 80분이다. 두 목표 모두
    **10분씩** 줄었으므로 정답은 −10 이다. 블록 평균끼리 빼도 우연히 −10 이 나오지만,
    처치군에서 thesis 가 빠지면(폴백 등) 블록 평균 뺄셈은 20 − 60 = **−40** 이라는 엉뚱한
    수를 낸다. 그 경우까지 −10 이 나오는지 확인한다.
    """
    rows = [
        _row(_case(CONTROL_BLOCK, "sqld", must_not_contain=[]), _plan(("a", 30, "s"))),
        _row(_case(CONTROL_BLOCK, "thesis", must_not_contain=[]), _plan(("b", 90, "s"))),
        _row(
            _case("failure_goal_scoped", "sqld", must_not_contain=["계획이 너무 컸어요"]),
            _plan(("c", 20, "s")),
        ),
        _row(
            _case("failure_goal_scoped", "thesis", must_not_contain=["계획이 너무 컸어요"]),
            _plan(("d", 80, "s")),
        ),
    ]
    assert summarize(rows)["M36_volume_delta_min"]["failure_goal_scoped"] == -10.0

    # thesis 처치군이 빠지면 짝이 하나 줄 뿐, 남은 짝의 차이는 그대로 −10 이어야 한다.
    without_thesis = [r for r in rows if r["case_id"] != "failure_goal_scoped-thesis"]
    result = summarize(without_thesis)
    assert result["M36_volume_delta_min"]["failure_goal_scoped"] == -10.0
    assert result["M36_pairs"]["failure_goal_scoped"] == 1


def test_m37_denominator_is_cases_that_carry_history() -> None:
    """대조군은 누출될 문구가 없으므로 분모에 넣지 않는다 — 넣으면 누출률이 희석된다."""
    rows = [
        _row(_case(CONTROL_BLOCK, "sqld", must_not_contain=[]), _plan(("a", 30, "s"))),
        _row(
            _case("failure_goal_scoped", "sqld", must_not_contain=["계획이 너무 컸어요"]),
            _plan(("계획이 너무 컸어요", 20, "s")),
        ),
        _row(
            _case("failure_goal_scoped", "thesis", must_not_contain=["계획이 너무 컸어요"]),
            _plan(("정규화 복습", 20, "s")),
        ),
    ]
    result = summarize(rows)
    assert result["M37_leak_rate"] == 0.5  # 이력 실린 2건 중 1건 — 대조군은 분모 밖
    assert result["M37_leaks"] == ["계획이 너무 컸어요"]


def test_m39_prefers_the_goal_scoped_signal() -> None:
    """범위 접두어를 읽으면 '이 목표' 쪽 변화가 '전체 목표' 쪽보다 크거나 같아야 한다."""
    rows = [
        _row(_case(CONTROL_BLOCK, "sqld", must_not_contain=[]), _plan(("a", 60, "s"))),
        _row(_case("failure_goal_scoped", "sqld", must_not_contain=["x"]), _plan(("b", 30, "s"))),
        _row(_case("failure_user_scoped", "sqld", must_not_contain=["x"]), _plan(("c", 50, "s"))),
    ]
    assert summarize(rows)["M39_scope_sensitivity"] == 1.0

    # 뒤집힌 경우 — 전체 목표 쪽이 더 크게 움직였다.
    flipped = [
        _row(_case(CONTROL_BLOCK, "sqld", must_not_contain=[]), _plan(("a", 60, "s"))),
        _row(_case("failure_goal_scoped", "sqld", must_not_contain=["x"]), _plan(("b", 55, "s"))),
        _row(_case("failure_user_scoped", "sqld", must_not_contain=["x"]), _plan(("c", 20, "s"))),
    ]
    assert summarize(flipped)["M39_scope_sensitivity"] == 0.0


def test_fallback_rows_are_excluded_from_scoring() -> None:
    """폴백은 `score` 가 없다 — 집계가 건너뛰어야지, 0분으로 세면 안 된다."""
    rows: list[dict[str, Any]] = [
        _row(_case(CONTROL_BLOCK, "sqld", must_not_contain=[]), _plan(("a", 30, "s"))),
        _row(_case("failure_goal_scoped", "sqld", must_not_contain=["x"]), _plan(("b", 20, "s"))),
        {
            "case_id": "failure_goal_scoped-thesis",
            "block": "failure_goal_scoped",
            "pair_id": "thesis",
            "case": _case("failure_goal_scoped", "thesis", must_not_contain=["x"]),
            "fell_back": True,
        },
    ]
    result = summarize(rows)
    assert result["M36_pairs"]["failure_goal_scoped"] == 1
    assert result["M36_volume_delta_min"]["failure_goal_scoped"] == -10.0


def test_summary_is_empty_not_crashing_without_any_rows() -> None:
    """원자료가 비어도 죽지 않는다 — 비율은 0 이 아니라 None(분모 없음)이다."""
    result = summarize([])
    assert result["M36_volume_delta_min"] == {}
    assert result["M37_leak_rate"] is None
    assert result["M39_scope_sensitivity"] is None
