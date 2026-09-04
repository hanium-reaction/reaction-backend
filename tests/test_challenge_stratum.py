"""challenge stratum 16건이 **결과를 안 보고도 재현되는가.**

⚠️ 사전등록의 핵심은 "결과를 본 뒤 유리한 표본을 고르는 것" 을 막는 데 있다. 격자를
`>=`·`<=` 같은 **범위**로 적으면 나중에 5/6/7 중 무엇을 고를지 여지가 남는다 —
이 파일은 **양쪽 값이 전부 정확한 수**로 고정돼 있는지 지킨다.

그리고 **활동창이 실제로 스케줄러에 도달하는지** 회귀로 잡는다. 초판은 두 outcome builder
가 `09:00-23:00` 을 **하드코딩**해서, 케이스에 좁은 활동창을 넣어도 배치에 아무 영향이
없었다 — 그 축이 변별력을 못 가졌다.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from scripts import build_challenge_stratum as B
from scripts import l1_7_run as H
from scripts import l1_7_schedule_eval as S

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "eval" / "golden_challenge_stratum.jsonl"
_TODAY = date(2026, 9, 3)


def _saved() -> list[dict[str, Any]]:
    return [json.loads(x) for x in _OUT.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── 1. 재현성 — 결과를 안 봐도 같은 16건 ────────────────────────────────────


def test_generator_is_deterministic() -> None:
    assert B.build() == B.build()


def test_saved_file_matches_the_generator() -> None:
    """저장본이 생성기와 갈리면 **어느 쪽이 사전등록인지 모른다.**"""
    assert _saved() == B.build(), "재생성 결과가 저장본과 다르다 — 생성기를 다시 돌려라"


def test_exactly_sixteen_cases_one_per_combination() -> None:
    cases = _saved()
    assert len(cases) == 16
    combos = {tuple(c["axes"][a] for a in B.AXES) for c in cases}
    assert len(combos) == 16, "조합이 중복되거나 빠졌다"


def test_case_ids_encode_the_combination() -> None:
    """id 만 보고 어떤 조합인지 알 수 있어야 한다 — 결과 표에서 역추적한다."""
    for c in _saved():
        tag = "".join(c["axes"][a][0] for a in B.AXES)
        assert c["case_id"] == f"chal-{tag}"


# ── 2. 범위가 아니라 **정확한 값** ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("table", "hard", "easy"),
    [
        (B.SESSION_LENGTH, 120, 90),
        (B.FREQUENCY, 7, 3),
        (B.MILESTONE_COUNT, 2, 0),
    ],
)
def test_both_sides_are_exact_numbers(table: dict[str, int], hard: int, easy: int) -> None:
    """`>=`·`<=` 를 남기면 나중에 값을 고를 여지가 생긴다."""
    assert table == {"hard": hard, "easy": easy}


def test_activity_window_is_an_exact_pair_on_both_sides() -> None:
    assert B.ACTIVITY_WINDOW == {"hard": ("21:00", "22:00"), "easy": ("09:00", "23:00")}


def test_focus_duration_is_fixed_across_all_cases() -> None:
    """집중 용량이 변하면 **경계 축이 `session_length` 하나로 안 갈린다.**"""
    assert B.FOCUS_DURATION_MIN == 120
    assert {c["interview"]["focus_duration_min"] for c in _saved()} == {120}


def test_weekly_hours_is_derived_to_hold_session_length_constant() -> None:
    """빈도를 올릴 때 세션이 짧아지면 **빈도 축이 활동창 축과 섞인다.**"""
    for c in _saved():
        g = c["interview"]["goal"]
        assert g["weekly_hours"] == round(g["frequency_per_week"] * g["session_length_min"] / 60)


def test_hard_window_is_narrower_than_the_session() -> None:
    """활동창 축은 **절대 시간이 아니라 세션 길이와의 관계**로 정의된다.

    초판은 "≤ 6시간" 이라 적었는데 실측하면 변별력이 0이었다 — 24분 세션은 1시간 창에도
    하루 하나씩 들어간다. 세션보다 좁아야 발화한다.
    """
    start, end = B.ACTIVITY_WINDOW["hard"]
    width = (int(end[:2]) - int(start[:2])) * 60 + (int(end[3:]) - int(start[3:]))
    assert width < B.SESSION_LENGTH["hard"], "활동창-hard 가 세션보다 넓다 — 변별력이 없다"
    e_start, e_end = B.ACTIVITY_WINDOW["easy"]
    e_width = (int(e_end[:2]) - int(e_start[:2])) * 60
    assert e_width > B.SESSION_LENGTH["hard"]


# ── 3. 활동창이 **실제로 스케줄러에 도달하는가** (회귀) ─────────────────────


@pytest.mark.parametrize("builder", [H.build_outcome, S.build_outcome])
def test_activity_window_reaches_the_outcome(builder: Any) -> None:
    """두 builder 모두 입력 계약에서 읽는가 — 하드코딩 회귀."""
    case = _saved()[0]
    narrow = copy.deepcopy(case)
    narrow["interview"]["activity_start"] = "21:00"
    narrow["interview"]["activity_end"] = "22:00"
    wide = copy.deepcopy(case)
    wide["interview"]["activity_start"] = "09:00"
    wide["interview"]["activity_end"] = "23:00"
    assert builder(narrow, today=_TODAY).availability.activity_window.start == "21:00"
    assert builder(wide, today=_TODAY).availability.activity_window.end == "23:00"


def test_missing_window_falls_back_to_the_original_default() -> None:
    """일반 골든셋 84건에는 이 필드가 없다 — 기존 수치가 바뀌면 안 된다."""
    plain = S.load_decompose_cases()[0]
    assert "activity_start" not in plain["interview"]
    win = S.build_outcome(plain, today=_TODAY).availability.activity_window
    assert (win.start, win.end) == ("09:00", "23:00")


def test_narrow_window_actually_changes_placement() -> None:
    """**도달만으로는 부족하다** — 배치가 실제로 달라져야 축이 산다."""
    case = next(c for c in _saved() if c["axes"]["window"] == "hard")
    wide = copy.deepcopy(case)
    wide["interview"]["activity_start"], wide["interview"]["activity_end"] = B.ACTIVITY_WINDOW[
        "easy"
    ]
    o_n, o_w = S.build_outcome(case, today=_TODAY), S.build_outcome(wide, today=_TODAY)
    n_placed, n_warn, n_act, _ = S.place(S.rule_only_plan(o_n, today=_TODAY), o_n, today=_TODAY)
    w_placed, _, _, _ = S.place(S.rule_only_plan(o_w, today=_TODAY), o_w, today=_TODAY)
    assert len(w_placed) > 0, "넓은 창에서도 배치가 안 된다 — 케이스 자체가 이상하다"
    assert len(n_placed) < len(w_placed), "좁은 창이 배치를 전혀 안 바꿨다 — 축이 죽어 있다"
    assert S._unplaced_count(n_warn) > 0


# ── 4. 마일스톤 축은 M23 을 **적용시키는 것**이 목적이다 ────────────────────


def test_milestone_axis_turns_m23_from_na_to_applicable() -> None:
    """이 축은 M20·M21 을 실패시키려는 게 아니다 — M23·M24 를 **적용**시키는 것이다."""
    hard = next(c for c in _saved() if c["axes"]["milestone"] == "hard")
    easy = next(c for c in _saved() if c["axes"]["milestone"] == "easy")
    o_h = H.build_outcome(hard, today=_TODAY)
    o_e = H.build_outcome(easy, today=_TODAY)
    assert len(H.cycle_window(hard, o_h, _TODAY)) == 2
    assert len(H.cycle_window(easy, o_e, _TODAY)) == 0


# ── 5. 일반 골든셋과 섞이지 않는다 ──────────────────────────────────────────


def test_stratum_ids_do_not_collide_with_the_general_set() -> None:
    general = {c["case_id"] for c in S.load_decompose_cases()}
    stratum = {c["case_id"] for c in _saved()}
    assert not (general & stratum), "id 가 겹치면 두 표본이 섞인다"


def test_stratum_has_its_own_block_label() -> None:
    assert {c["block"] for c in _saved()} == {"challenge_stratum"}
