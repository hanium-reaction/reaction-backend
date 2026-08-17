"""회복 골든셋 120건의 무결성 (L1-0).

이 골든셋은 L1 오프라인 평가 전체의 **공통 입력**이다. 여기가 조용히 망가지면 프롬프트
비교·커버리지·패딩률 전부가 거짓 위에 서게 되므로, 구조를 테스트로 고정한다.

특히 못 박는 것:
- 블록별 건수 — 사양(`docs/experiments/experiment-plan-v1.md` §2 L1-0)과 1:1
- **재현성** — 생성기를 다시 돌리면 디스크의 파일과 바이트 단위로 같아야 한다.
  안 그러면 "이 수치는 이 골든셋에서 나왔다"를 나중에 증명할 수 없다.
- **적대적 케이스가 실제로 적대적인가** — `must_not_contain` 이 회고 문구와 아무 관계도
  없으면 그 단언은 공허하다(통과해도 아무것도 검증하지 않는다).
- 태그 오타 없음 — 존재하지 않는 태그는 룰 엔진에서 조용히 매칭 0이 된다.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from scripts.build_golden_recovery_cases import (
    ADVERSARIAL,
    ALL_TAGS,
    BASE_DATE,
    EXPECTED_COUNTS,
    EXPECTED_TOTAL,
    OUTPUT_PATH,
    UNCOVERED_TAGS,
    build_cases,
    to_jsonl,
)

# 실패 사유 상한 — `schemas/reflection.py::...failure_tags = Field(..., max_length=2)`
# 및 api-contract "failureTags?(0~2)". 골든셋은 이 계약을 넘지 않아야 하고, 넘는 케이스는
# **의도된 계약 위반 케이스 1건뿐**이어야 한다.
MAX_FAILURE_TAGS = 2
INTENTIONAL_VIOLATION_ID = "boundary-three-tags-contract-violation"


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    """디스크의 골든셋. 생성기 출력이 아니라 **커밋된 파일**을 읽는다."""
    assert OUTPUT_PATH.exists(), (
        f"골든셋 파일이 없다: {OUTPUT_PATH} — "
        "`uv run python -m scripts.build_golden_recovery_cases` 로 생성할 것"
    )
    return [json.loads(line) for line in OUTPUT_PATH.read_text(encoding="utf-8").splitlines()]


def test_total_and_block_counts_match_the_spec(cases: list[dict]) -> None:
    """사양의 표와 1:1. 블록 하나가 줄면 그 블록이 검증하던 축이 통째로 사라진다."""
    assert len(cases) == EXPECTED_TOTAL

    from collections import Counter

    actual = Counter(c["block"] for c in cases)
    assert dict(actual) == EXPECTED_COUNTS
    assert sum(EXPECTED_COUNTS.values()) == EXPECTED_TOTAL, "사양 표 자체가 합이 안 맞는다"


def test_file_on_disk_matches_the_generator(cases: list[dict]) -> None:
    """재현성 — 생성기를 다시 돌려도 같은 파일이어야 한다.

    난수·현재시각을 쓰면 여기서 터진다. 그게 이 테스트의 목적이다: 보고서의 수치가
    "이 커밋의 이 골든셋"에서 나왔다는 것을 증명할 수 있어야 한다.
    """
    regenerated = to_jsonl(build_cases())
    assert regenerated == OUTPUT_PATH.read_text(encoding="utf-8"), (
        "생성기 출력과 커밋된 파일이 다르다 — 생성기를 고쳤으면 파일도 다시 생성해 커밋할 것"
    )


def test_case_ids_are_unique(cases: list[dict]) -> None:
    ids = [c["case_id"] for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"중복 case_id: {dupes}"


def test_every_case_is_labelled_synthetic(cases: list[dict]) -> None:
    """합성 데이터임을 숨길 수 없게 한다 — 라이브 recovery_attempts 가 0건이라 전량 합성이다."""
    assert all(c["synthetic"] is True for c in cases)


def test_no_phantom_tags(cases: list[dict]) -> None:
    """존재하지 않는 태그는 룰 엔진에서 조용히 매칭 0이 된다 — 오타가 결과를 왜곡한다."""
    used = {tag for c in cases for tag in c["failure_tags"]}
    assert used <= set(ALL_TAGS), f"카탈로그에 없는 태그: {used - set(ALL_TAGS)}"


def test_all_thirteen_tags_appear_somewhere(cases: list[dict]) -> None:
    """13태그 전부가 입력으로 등장해야 커버리지를 논할 수 있다."""
    used = {tag for c in cases for tag in c["failure_tags"]}
    assert used == set(ALL_TAGS), f"입력에 없는 태그: {set(ALL_TAGS) - used}"


def test_only_one_case_violates_the_two_tag_contract(cases: list[dict]) -> None:
    """실패 사유는 0~2개(계약). 상한을 넘는 케이스는 **의도된 1건**뿐이어야 한다.

    이 케이스가 없어지면 "3개가 들어오면 거부되는가"를 아무도 확인하지 않게 된다.
    반대로 실수로 늘어나면 골든셋 전체가 계약 위반 입력으로 오염된다.
    """
    violators = [c["case_id"] for c in cases if len(c["failure_tags"]) > MAX_FAILURE_TAGS]
    assert violators == [INTENTIONAL_VIOLATION_ID]


def test_uncovered_block_targets_exactly_the_three_unmatched_tags(cases: list[dict]) -> None:
    """보강 블록은 정확히 {TIME_SHORTAGE, OVERRUN, AVOIDANCE} 를 겨눈다.

    이 3태그는 현 시드에서 어떤 `primary_trigger_tags` 에도 없다
    (`tests/test_recovery_catalog_sync.py::test_uncovered_tags_are_a_design_decision_not_a_gap`).
    신규 전략이 들어오면 그 테스트와 함께 여기도 의식적으로 갱신할 것.
    """
    block = [c for c in cases if c["block"] == "uncovered_tag"]
    tags = {tag for c in block for tag in c["failure_tags"]}
    assert tags == set(UNCOVERED_TAGS)
    # 태그당 4건 — 맥락(부담·연속실패)을 흔들어야 신규 전략의 승격을 관측할 수 있다.
    for tag in UNCOVERED_TAGS:
        n = sum(1 for c in block if c["failure_tags"] == [tag])
        assert n == 4, f"{tag}: {n}건 (4건이어야 한다)"


def test_adversarial_cases_are_actually_adversarial(cases: list[dict]) -> None:
    """`must_not_contain` 이 공허한 단언이 아님을 보장한다.

    적대적 케이스의 핵심은 **사용자가 자기 자신을 원인으로 지목한 회고**다. 금지 문구가
    그 회고와 아무 관계도 없으면, 나중에 그 단언을 통과시켜도 아첨 여부를 검증한 게 아니다.
    → 각 케이스마다 금지 문구 중 **최소 하나는 회고 문구에 실제로 등장**해야 한다.
      (나머지는 LLM 이 만들어낼 수 있는 패러프레이즈 방어분이라 등장하지 않아도 된다.)
    """
    block = [c for c in cases if c["block"] == "adversarial"]
    assert len(block) == len(ADVERSARIAL)

    for c in block:
        phrases = c["assertions"]["must_not_contain"]
        assert phrases, f"{c['case_id']}: 금지 문구가 비어 있어 단언이 공허하다"
        memo = c["reflection_memo"]
        assert any(p in memo for p in phrases), (
            f"{c['case_id']}: 금지 문구 {phrases} 중 어느 것도 회고 문구에 없다 "
            f"— 이 케이스는 적대적이지 않다: {memo!r}"
        )


def test_adversarial_memos_carry_self_blame_not_situational_attribution(cases: list[dict]) -> None:
    """적대적 회고는 **사람**을 원인으로 지목한다 (상황이 아니라).

    자기지시 표현이 없으면 그냥 평범한 실패 회고이고, 아첨 테스트가 성립하지 않는다.
    ⚠️ 이 문구들은 **사용자 입력**이라 금지어 필터(`safety/banned_words`)의 대상이 아니다.
    필터는 시스템 출력에만 걸린다 — 사용자가 쓴 말을 검열하는 장치가 아니다.
    """
    self_referential = ("제가", "저는", "저만", "제 ", "내가")
    for c in cases:
        if c["block"] != "adversarial":
            continue
        memo = c["reflection_memo"]
        assert any(m in memo for m in self_referential), (
            f"{c['case_id']}: 자기지시 표현이 없어 자기비난 회고가 아니다: {memo!r}"
        )


def test_all_failures_are_in_the_past(cases: list[dict]) -> None:
    """실패는 이미 일어난 사건이다 — 미래 날짜면 회고가 열릴 수 없어 케이스가 성립하지 않는다."""
    for c in cases:
        day = date.fromisoformat(c["execution"]["plan_start_at_kst"][:10])
        assert day <= BASE_DATE, f"{c['case_id']}: 미래 실패 {day}"


def test_completion_status_is_recovery_eligible(cases: list[dict]) -> None:
    """회복 카드는 failed / partial_done 에서만 생성된다 (`routes/recovery.py::_ELIGIBLE_STATUSES`).

    다른 상태가 섞이면 그 케이스는 애초에 회복 경로를 타지 않는다.
    """
    from reaction_backend.api.routes.recovery import _ELIGIBLE_STATUSES

    for c in cases:
        status = c["execution"]["completion_status"]
        assert status in _ELIGIBLE_STATUSES, f"{c['case_id']}: 회복 대상이 아닌 상태 {status}"


def test_partial_done_case_exists_and_is_distinguishable(cases: list[dict]) -> None:
    """partial_done 이 최소 1건 있어야 한다 — '소폭 미달에 위로 문구를 붙이지 않는다'는

    규칙(A8, Prinsen 2018)을 검증할 유일한 입력이다.
    """
    partials = [c for c in cases if c["execution"]["completion_status"] == "partial_done"]
    assert partials, "partial_done 케이스가 없어 조건부 acknowledgment 를 검증할 수 없다"


def test_context_fields_are_within_documented_ranges(cases: list[dict]) -> None:
    """overwhelm_level 은 1~5 (context_snapshot.overwhelm_level SmallInteger, 설계 §5)."""
    for c in cases:
        ow = c["context"]["overwhelm_level"]
        assert 1 <= ow <= 5, f"{c['case_id']}: overwhelm={ow}"
        assert c["context"]["consecutive_failure_count"] >= 0


def test_every_case_has_a_reflection_memo(cases: list[dict]) -> None:
    """회고 문구가 없으면 프롬프트의 `context_summary` 가 비어 LLM 비교가 무의미해진다."""
    for c in cases:
        assert c["reflection_memo"].strip(), f"{c['case_id']}: 회고 문구 없음"
