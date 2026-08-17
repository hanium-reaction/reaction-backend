"""룰 엔진 `select_strategies` 의 도달 가능 출력 공간 — **전수 열거** (L1-3 기준선).

왜 전수인가: 실패 사유는 계약상 **0~2개**다(`schemas/reflection.py` `max_length=2`,
api-contract "failureTags?(0~2)"). 13태그에서 0~2개를 고르는 조합은
`1 + 13 + C(13,2) = 92` 가지뿐이라, 표본이 아니라 **입력 공간 전체를 열거**할 수 있다.
따라서 아래 수치는 추정이 아니라 현재 시드에 대한 **완전한 특성 기술**이다.

⚠️ **이 파일은 현재 동작을 고정하는 characterization test 다.** 신규 전략 4종을 추가하면
여기가 의도적으로 빨강이 되어야 하고, 그때 수치를 갱신하는 것이 곧 "무엇이 바뀌었나" 의
증거가 된다 (`tests/test_recovery_catalog_sync.py::test_uncovered_tags_are_a_design_decision_not_a_gap`
와 같은 성격의 핀).

여기서 드러난 것 (2026-08-17, 시드 d09c105520b5 기준):
1. **PARK 그룹은 92개 입력 전부에서 단 한 번도 노출되지 않는다.** 세 겹으로 막혀 있다 —
   `primary_trigger_tags=[]`(매칭 불가) + `display_priority=90`(최하위라 패딩으로도 도달
   불가, 2장만 채우므로 10/50/70 이 먼저) + `select_strategies` 가 **overwhelm 을 인자로
   받지 않는다**(설계된 동적 트리거를 구현할 자리 자체가 없다).
2. 계약 문구는 "총 2~4장"이지만 실제 도달 가능한 카드 수는 **2장 또는 3장**뿐이다.
   4장은 서로 다른 4그룹이 매칭돼야 하는데 태그가 최대 2개라 원리적으로 불가능하다.
3. 매칭이 0건인 입력이 **7가지** 존재하고, 그 경우 노출되는 2장은 전부 패딩이다.
"""

from __future__ import annotations

import itertools

from reaction_backend.db.models.recovery_strategy_catalog import (
    RECOVERY_OPTION_GROUP_VALUES,
)
from reaction_backend.orchestrator.recovery import MAX_CARDS, MIN_CARDS, select_strategies
from tests.conftest import default_failure_tags, default_recovery_strategies

# ── 전수 열거 (계약: 실패 사유 0~2개) ────────────────────────────────────
MAX_FAILURE_TAGS = 2


def _all_contract_valid_inputs() -> list[list[str]]:
    tags = [t.tag_code for t in default_failure_tags()]
    combos: list[list[str]] = [[]]
    for size in range(1, MAX_FAILURE_TAGS + 1):
        combos.extend(list(c) for c in itertools.combinations(tags, size))
    return combos


def _is_padding(card: object, tags: set[str]) -> bool:
    """이 카드가 사용자의 실패 사유와 **무관하게** 채워졌는가."""
    primary = set(getattr(card, "primary_trigger_tags", None) or [])
    return not (tags & primary)


def test_input_space_is_exactly_92_combinations() -> None:
    """열거가 완전함을 먼저 고정한다 — 이 수가 틀리면 아래 모든 수치의 분모가 틀린다."""
    inputs = _all_contract_valid_inputs()
    assert len(inputs) == 1 + 13 + 78 == 92


def test_park_group_is_unreachable_for_every_contract_valid_input() -> None:
    """**PARK 은 도달 불가**. 92개 입력 전부에서 0회 노출.

    설계 문서(`db/models/recovery_strategy_catalog.py:20`)는 `PARK_DEFAULT ← overwhelm_level >= 4`
    라고 적어 두었지만, `select_strategies(failure_tags, strategies)` 는 overwhelm 을 **받지
    않는다**. 즉 `context_snapshot` 캡처(#19-B-2)가 완성돼도 이 함수는 여전히 PARK 를 낼 수
    없다 — 데이터 공백이 아니라 **시그니처 공백**이다.

    이 테스트가 빨강이 되는 경우는 둘뿐이고, 둘 다 의도된 변경이어야 한다:
    (a) PARK 전략에 `primary_trigger_tags` 를 부여했다
    (b) 선택 함수가 정서/부담 신호를 인자로 받게 됐다
    """
    strategies = default_recovery_strategies()
    exposures = {g: 0 for g in RECOVERY_OPTION_GROUP_VALUES}

    for tags in _all_contract_valid_inputs():
        for card in select_strategies(tags, strategies):
            exposures[card.option_group] += 1

    assert exposures["PARK"] == 0, (
        f"PARK 이 노출됐다 — 도달 가능해졌다면 이 핀을 의도적으로 갱신할 것: {exposures}"
    )
    # 나머지 3그룹은 실제로 쓰인다 — 전부 0이면 열거 자체가 잘못된 것이다.
    for group in ("DOWNSCOPE", "RESCHEDULE", "CARRY_OVER"):
        assert exposures[group] > 0, f"{group} 이 한 번도 안 나왔다 — 열거가 잘못됐다"


def test_four_cards_are_structurally_unreachable() -> None:
    """계약은 "2~4장"이지만 실제 도달 범위는 2~3장이다.

    4장은 서로 다른 4그룹이 동시에 매칭돼야 하는데, 태그가 최대 2개이므로 매칭되는 그룹은
    최대 2개다(+ 패딩으로 채워도 `MIN_CARDS` 까지만 채운다). `MAX_CARDS=4` 는 현재
    **도달 불가능한 상한**이다.
    """
    strategies = default_recovery_strategies()
    counts = {len(select_strategies(tags, strategies)) for tags in _all_contract_valid_inputs()}

    assert counts == {2, 3}, f"도달 가능한 카드 수가 바뀌었다: {sorted(counts)}"
    assert MIN_CARDS == 2
    assert MAX_CARDS == 4, "상한이 바뀌었으면 위 주장(4장 불가)을 재검토할 것"


def test_padding_rate_over_the_whole_input_space_is_one_third() -> None:
    """전 입력 공간의 패딩률 = 62/186 (33.3%).

    '패딩' = 그 카드의 `primary_trigger_tags` 와 사용자의 실패 사유의 교집합이 공집합.
    `MIN_CARDS=2` 때문에 매칭 그룹이 0~1개인 입력에서는 패딩이 **강제**된다.

    ⚠️ 이 수치는 입력을 균등 가중한 것이다. 실사용 분포는 태그 선택 빈도에 따라 달라지므로
    보고서에 "실사용 패딩률"로 옮겨 쓰면 안 된다 — 분모를 반드시 명시할 것.
    """
    strategies = default_recovery_strategies()
    total = padding = 0

    for tags in _all_contract_valid_inputs():
        tag_set = set(tags)
        for card in select_strategies(tags, strategies):
            total += 1
            if _is_padding(card, tag_set):
                padding += 1

    assert (padding, total) == (62, 186), f"패딩률이 바뀌었다: {padding}/{total}"


def test_seven_inputs_get_no_matching_card_at_all() -> None:
    """매칭 0건 입력 7가지 — 노출되는 2장이 전부 사용자의 사유와 무관하다.

    미커버 3태그(TIME_SHORTAGE/OVERRUN/AVOIDANCE)의 단독 3가지 + 그들끼리의 조합 3가지
    + 태그 미선택 1가지 = 7. 신규 전략이 이 3태그를 덮으면 **0이 되어야 한다.**
    """
    strategies = default_recovery_strategies()
    zero_match = [
        tags
        for tags in _all_contract_valid_inputs()
        if all(_is_padding(card, set(tags)) for card in select_strategies(tags, strategies))
    ]

    assert len(zero_match) == 7, f"매칭 0건 입력이 바뀌었다: {zero_match}"

    uncovered = {"TIME_SHORTAGE", "OVERRUN", "AVOIDANCE"}
    for tags in zero_match:
        assert set(tags) <= uncovered, (
            f"미커버 3태그 밖의 입력이 매칭 0건이 됐다: {tags} — 회귀 가능성"
        )


def test_uncovered_tags_produce_only_padding() -> None:
    """미커버 태그 단독 입력은 **패딩 2장**만 받는다 — 사용자는 자기 사유와 무관한

    선택지를 보게 된다. 이것이 '회복이 약하다'의 구조적 원인 중 하나다.
    """
    strategies = default_recovery_strategies()
    for tag in ("TIME_SHORTAGE", "OVERRUN", "AVOIDANCE"):
        cards = select_strategies([tag], strategies)
        assert len(cards) == MIN_CARDS
        assert all(_is_padding(c, {tag}) for c in cards), (
            f"{tag}: 매칭 카드가 생겼다 — 신규 전략이 들어왔다면 이 핀을 갱신할 것"
        )


def test_golden_set_padding_rate_is_higher_by_design() -> None:
    """골든셋(120건) 패딩률 = 141/240 (58.8%) — 전 입력 공간(33.3%)보다 높다.

    골든셋이 미커버 3태그를 **의도적으로 과표집**했기 때문이다(uncovered_tag 블록 12건 +
    적대적 10건이 AVOIDANCE/HARD_TO_START 에 몰려 있다). 두 수치는 분모가 다르므로
    보고서에서 섞어 쓰면 안 된다 — 이 테스트가 그 차이를 명시적으로 기록한다.
    """
    import json

    from scripts.build_golden_recovery_cases import OUTPUT_PATH

    strategies = default_recovery_strategies()
    cases = [json.loads(line) for line in OUTPUT_PATH.read_text(encoding="utf-8").splitlines()]

    total = padding = 0
    for case in cases:
        tags = case["failure_tags"]
        tag_set = set(tags)
        for card in select_strategies(tags, strategies):
            total += 1
            if _is_padding(card, tag_set):
                padding += 1

    assert (padding, total) == (141, 240), f"골든셋 패딩률이 바뀌었다: {padding}/{total}"
    assert padding / total > 0.33, "골든셋은 미커버 태그를 과표집하므로 전 공간보다 높아야 한다"
