"""이력 골든셋 42건의 무결성 (L1-8).

이 골든셋은 "실행 이력이 계획을 바꾸는가" 의 **유일한 입력**이다. 여기가 조용히 망가지면
#466·#467 의 효과 주장 전체가 거짓 위에 서게 되므로 구조를 테스트로 고정한다.

특히 못 박는 것:
- **짝 불변식** — 같은 `pair_id` 의 `goal` 이 전 블록에서 **완전히 같아야** 한다. 이 골든셋의
  지표는 전부 대조군 대비 차이라, 목표가 조금이라도 다르면 그 차이가 이력의 효과로 둔갑한다.
- **교란 차단** — 실패·회복 블록의 연속 실패 수가 0 이어야 한다. 0 이 아니면 분량 감쇠
  (#467)가 동시에 걸려 "프롬프트가 읽었다" 와 "룰이 예산을 깎았다" 가 한 수치에 섞인다.
- **감쇠 블록은 실제로 감쇠를 일으키는가** — 프로덕션 함수(`dampened_density`)로 확인한다.
  숫자만 넣고 감쇠가 안 걸리면 그 블록은 대조군 6건이 하나 더 있는 것과 같다.
- **단언이 헛돌지 않는가** — `must_not_contain` 은 그 케이스 이력에 **실제로 들어간** 라벨과
  일치해야 한다. 안 그러면 누출 판정이 통과해도 아무것도 검증하지 않는다.
- **라벨이 마스터 시드 실값인가** — 지어낸 문구를 쓰면 누출 판정이 프로덕션 프롬프트에
  실제로 들어가는 문자열과 어긋나 영원히 통과한다.
- **재현성** — 생성기를 다시 돌리면 디스크 파일과 바이트 단위로 같아야 한다.
- **절대 날짜 부재** — 고정 날짜는 하루만 지나도 판정을 뒤집는다.
"""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest
from scripts.build_golden_history_cases import (
    BLAME_MARKERS,
    BLOCKS,
    EXPECTED_COUNTS,
    EXPECTED_TOTAL,
    GOALS,
    OUTPUT_PATH,
    build_cases,
    to_jsonl,
)

from reaction_backend.orchestrator.escalation import L1_CONSECUTIVE_FAILURE_THRESHOLD
from reaction_backend.orchestrator.first_plan_adapter import (
    context_from_outcome,
    dampened_density,
)
from tests.conftest import default_failure_tags, default_recovery_strategies

_ABSOLUTE_DATE = re.compile(r"\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b")


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    """디스크의 골든셋. 생성기 출력이 아니라 **커밋된 파일**을 읽는다."""
    assert OUTPUT_PATH.exists(), (
        f"골든셋 파일이 없다: {OUTPUT_PATH} — "
        "`uv run python -m scripts.build_golden_history_cases` 로 생성할 것"
    )
    return [json.loads(line) for line in OUTPUT_PATH.read_text(encoding="utf-8").splitlines()]


def test_file_on_disk_matches_the_generator(cases: list[dict]) -> None:
    """커밋된 파일 == 생성기 출력. 손으로 고치면 여기가 빨개진다."""
    assert to_jsonl(cases) == to_jsonl(build_cases())


def test_block_counts_match_the_spec(cases: list[dict]) -> None:
    counts = Counter(c["block"] for c in cases)
    assert len(cases) == EXPECTED_TOTAL
    assert dict(counts) == EXPECTED_COUNTS
    assert set(counts) == set(BLOCKS)


def test_every_block_shares_the_same_six_goals(cases: list[dict]) -> None:
    """짝 불변식 — 블록 간 차이는 **오직 `history`** 뿐이다.

    이 골든셋의 지표는 전부 "같은 목표의 대조군 대비 차이" 다. 목표 문구·세션 길이·마감이
    블록마다 조금이라도 다르면 그 차이가 이력의 효과로 둔갑한다. 자료 골든셋이 `no_material`
    을 M14 의 기준선으로 쓸 때와 같은 전제이고, 여기서는 블록이 7개라 훨씬 깨지기 쉽다.
    """
    by_pair: dict[str, list[dict]] = {}
    for case in cases:
        by_pair.setdefault(case["pair_id"], []).append(case)

    assert set(by_pair) == {g.key for g in GOALS}
    for pair_id, group in by_pair.items():
        assert len(group) == len(BLOCKS), f"{pair_id} 가 모든 블록에 있지 않다"
        goals = {json.dumps(c["goal"], ensure_ascii=False, sort_keys=True) for c in group}
        assert len(goals) == 1, f"{pair_id} 의 목표가 블록마다 다르다 — 대조가 성립하지 않는다"


def test_every_case_points_at_its_own_control(cases: list[dict]) -> None:
    """비교 대상은 같은 목표의 대조군이다 — 블록 평균끼리 비교하지 않는다."""
    case_ids = {c["case_id"] for c in cases}
    for case in cases:
        target = case["expected"]["compare_to"]
        assert target in case_ids, f"{case['case_id']} 의 비교 대상 {target} 이 없다"
        assert target == f"no_history-{case['pair_id']}"


def test_only_the_damped_block_carries_a_failure_streak(cases: list[dict]) -> None:
    """교란 차단 — 실패·회복 블록에서 분량 감쇠가 동시에 걸리면 안 된다.

    연속 실패가 문턱을 넘으면 `dampened_density` 가 프리셋 단위로 예산을 깎는다. 그러면
    세션이 짧아진 게 프롬프트가 실패 사유를 읽어서인지 룰이 깎아서인지 구별할 수 없다.
    """
    for case in cases:
        streak = case["history"]["consecutive_goal_failures"]
        if case["block"] == "damped_density":
            assert streak >= L1_CONSECUTIVE_FAILURE_THRESHOLD
        else:
            assert streak == 0, f"{case['case_id']} 에 감쇠가 새어 들어왔다"


def test_damped_block_actually_lowers_the_preset(cases: list[dict]) -> None:
    """프리셋이 실제로 내려가는가 — 프로덕션 함수로 확인한다."""
    damped = [c for c in cases if c["block"] == "damped_density"]
    assert damped
    for case in damped:
        streak = case["history"]["consecutive_goal_failures"]
        assert dampened_density("standard", consecutive_goal_failures=streak) != "standard"


def test_damped_block_actually_lowers_the_volume_the_prompt_asks_for(cases: list[dict]) -> None:
    """**프리셋이 내려가는 것만으로는 부족하다** — 프롬프트가 요구하는 분량이 줄어야 한다.

    처음엔 목표가 빈도(`sessions_per_week`)를 말하게 만들었는데, `target_sessions_per_week`
    는 우선순위 1 로 사용자가 명시한 빈도를 **density 로 가감하지 않고 존중한다.** 그래서
    프리셋만 내려가고 총 분량·총 세션 수·주당 세션 수가 **한 숫자도 안 움직였다**
    (실측: 주3회·50분 목표에서 standard/light 둘 다 600분·12세션). 그 상태로 두면 이 블록은
    대조군이 6건 더 있는 것과 같은데, 하네스는 조용히 돌고 보고서는 "감쇠를 쟀다" 고 말한다.

    그래서 목표에서 빈도를 빼고 `weekly_hours` 로 바꿨다. 이 테스트는 그 선택이 실제로
    측정력을 주는지를 **프로덕션 프롬프트 변수**로 케이스마다 확인한다 — 슬롯 구성이
    바뀌거나 세션 상한(`_MAX_LLM_SESSIONS`)에 걸리기 시작하면 여기가 빨개진다.
    """
    from datetime import date

    from scripts.l1_8_run import build_outcome, prompt_vars_for

    today = date(2026, 9, 7)  # 고정일 — 마감 오프셋만으로 재현된다
    damped = [c for c in cases if c["block"] == "damped_density"]
    assert damped
    for case in damped:
        outcome = build_outcome(case, today=today)
        baseline = context_from_outcome(outcome, density="standard", target_date=today)[
            "prompt_vars"
        ]
        treated = prompt_vars_for(case, today=today)["prompt_vars"]
        assert int(treated["total_minutes"]) < int(baseline["total_minutes"]), (
            f"{case['case_id']}: 감쇠가 프롬프트 분량을 못 줄였다 — 이 블록이 대조군이 된다"
        )


def test_recovery_blocks_differ_only_in_the_outcome_word(cases: list[dict]) -> None:
    """회복 두 블록은 **같은 전략**이어야 한다 — 다르면 '전략 차이'와 '결과 차이'가 섞인다."""
    worked = {
        c["pair_id"]: c["history"]["recovery_contexts"]
        for c in cases
        if c["block"] == "recovery_worked"
    }
    rejected = {
        c["pair_id"]: c["history"]["recovery_contexts"]
        for c in cases
        if c["block"] == "recovery_rejected"
    }
    assert set(worked) == set(rejected)
    for pair_id, rows in worked.items():
        assert [r["strategy_type"] for r in rows] == [r["strategy_type"] for r in rejected[pair_id]]
        assert [r["outcome"] for r in rows] == ["worked"]
        assert [r["outcome"] for r in rejected[pair_id]] == ["rejected"]


def test_leak_assertions_actually_have_something_to_catch(cases: list[dict]) -> None:
    """`must_not_contain` == 그 케이스 이력의 라벨 전부.

    비면 단언이 통과해도 아무것도 검증하지 않고, 이력에 없는 문구가 실리면 영원히 통과한다.
    """
    for case in cases:
        history = case["history"]
        labels = [c["label_ko"] for c in history["failure_contexts"]] + [
            c["label_ko"] for c in history["recovery_contexts"]
        ]
        assert case["assertions"]["must_not_contain"] == labels
        has_history = bool(labels)
        assert has_history == (case["block"] not in ("no_history", "damped_density"))


def test_labels_come_from_the_seeded_master_tables(cases: list[dict]) -> None:
    """라벨은 **마스터 시드 실값**이다 — 지어내면 누출 판정이 프로덕션과 어긋난다.

    시드 미러(conftest)를 쓰는 이유는 그 미러가 `test_recovery_catalog_sync.py` 로 이미
    실제 마이그레이션에 고정돼 있기 때문이다. DB 없이도 같은 강도로 검사된다.
    """
    tag_labels = {t.tag_code: t.label_ko for t in default_failure_tags()}
    strategy_labels = {s.strategy_type: s.label_ko for s in default_recovery_strategies()}

    for case in cases:
        for row in case["history"]["failure_contexts"]:
            assert tag_labels.get(row["tag_code"]) == row["label_ko"]
        for row in case["history"]["recovery_contexts"]:
            assert strategy_labels.get(row["strategy_type"]) == row["label_ko"]


def test_blame_markers_are_not_already_covered_by_the_global_filter() -> None:
    """전역 금지어 필터가 잡는 말을 여기서 또 세지 않는다.

    "또 못"·"실패"·"못했" 는 `enforce()` 가 이미 치환한다. 그걸 이 골든셋의 지표로 두면
    필터 성능을 프롬프트 성능으로 착각하게 된다 — 회복 골든셋이 전역 사전을 저장하지 않는
    것과 같은 이유다. 여기 남는 건 **필터가 안 잡는** 표현뿐이어야 한다.
    """
    from reaction_backend.safety.banned_words import enforce

    assert BLAME_MARKERS
    for marker in BLAME_MARKERS:
        assert not enforce(marker).changed, f"'{marker}' 는 이미 전역 필터가 잡는다"


def test_no_absolute_dates_leak_into_cases(cases: list[dict]) -> None:
    """마감은 상대 오프셋뿐 — 절대 날짜는 하루만 지나도 판정이 뒤집힌다."""
    for case in cases:
        blob = json.dumps(case, ensure_ascii=False)
        assert not _ABSOLUTE_DATE.search(blob), f"{case['case_id']} 에 절대 날짜가 있다"
        assert isinstance(case["goal"]["deadline_offset_days"], int)


def test_every_case_is_marked_synthetic(cases: list[dict]) -> None:
    """전량 합성 — 보고서가 합성 비율을 반드시 명시해야 한다."""
    assert all(c["synthetic"] is True for c in cases)
