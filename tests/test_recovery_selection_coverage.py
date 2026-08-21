"""룰 엔진 `select_strategies` 의 도달 가능 출력 공간 — **전수 열거** (L1-3 기준선).

왜 전수인가: 실패 사유는 계약상 **0~2개**다(`schemas/reflection.py` `max_length=2`,
api-contract "failureTags?(0~2)"). 13태그에서 0~2개를 고르는 조합은
`1 + 13 + C(13,2) = 92` 가지뿐이라, 표본이 아니라 **입력 공간 전체를 열거**할 수 있다.
따라서 아래 수치는 추정이 아니라 현재 시드에 대한 **완전한 특성 기술**이다.

⚠️ **이 파일은 현재 동작을 고정하는 characterization test 다.** 시드가 바뀌면 여기가
의도적으로 빨강이 되어야 하고, 그때 수치를 갱신하는 것이 곧 "무엇이 바뀌었나" 의 증거가
된다 (`tests/test_recovery_catalog_sync.py` 와 같은 성격의 핀).

## 2026-08-17 갱신 — 신설 4전략(8680c4567ca6, PR #257) **전후** 비교

첫 버전(PR #256)은 원본 9전략 기준 기준선을 잡았다. `TIMEBOX_REBUDGET` /
`BUFFER_INSERT` / `SELF_FORGIVENESS_NANO` / `GOAL_RECHECK` 4종이 들어오면서 그 기준선이
바뀌었고, 아래는 **바뀐 뒤**의 값이다(이전 값은 각 테스트 docstring 에 대조로 남긴다).

| 지표 | 이전(9전략) | 이후(13전략) |
|---|---|---|
| PARK 노출 | **0 / 92** | **25 / 92** (전부 실매칭, 패딩 0) |
| 패딩률(전 공간) | 62/186 = 33.3% | 28/203 = **13.8%** |
| 매칭 0건 입력 | 7가지 | **1가지**(태그 미선택뿐) |
| 카드 수 분포 | {2, 3} | {2, 3, **4**} |
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


def test_park_group_is_reachable_via_goal_recheck() -> None:
    """**PARK 이 도달 가능해졌다.** 92개 입력 중 25건에서 노출, 전부 실매칭(패딩 0).

    이전(9전략, PR #256): PARK 노출 0/92 — `primary_trigger_tags=[]`(매칭 불가) +
    `display_priority=90`(패딩으로도 도달 불가) + `select_strategies` 가 overwhelm 을
    받지 않아(시그니처 공백) `PARK_DEFAULT` 의 동적 트리거를 구현할 자리가 없었다.

    지금: `GOAL_RECHECK`(PARK, `primary_trigger_tags=["AVOIDANCE","PRIORITY_SHIFT"]`)가
    기존 9전략과 **완전히 같은 정적 태그 매칭 경로**로 PARK 를 연다.

    ⚠️ 이 92건 전수 열거는 **`overwhelm_level` 을 넘기지 않는다**(기본값 None) — 그래서
    `PARK_DEFAULT` 자체는 이 테스트 안에서는 여전히 도달 불가다(`primary_trigger_tags=[]`
    그대로). `select_strategies` 는 이제 동적 트리거를 받을 수 있지만(시그니처는
    확장됐다 — `test_park_default_reachable_via_overwhelm_dynamic_trigger` 참조),
    그 인자가 없는 이 기본 특성 기술에는 영향이 없다. 여기서 도달 가능해진 것은
    **PARK 그룹**(정적 경로)이지 `PARK_DEFAULT` 전략 개별이 아니다.
    """
    strategies = default_recovery_strategies()
    exposures = dict.fromkeys(RECOVERY_OPTION_GROUP_VALUES, 0)
    park_padding = 0

    for tags in _all_contract_valid_inputs():
        ts = set(tags)
        for card in select_strategies(tags, strategies):
            exposures[card.option_group] += 1
            if card.option_group == "PARK" and _is_padding(card, ts):
                park_padding += 1

    assert exposures["PARK"] == 25, f"PARK 노출 횟수가 바뀌었다: {exposures}"
    assert park_padding == 0, "PARK 노출이 전부 실매칭이어야 한다(GOAL_RECHECK 는 패딩으로 안 뜬다)"
    for group in ("DOWNSCOPE", "RESCHEDULE", "CARRY_OVER"):
        assert exposures[group] > 0, f"{group} 이 한 번도 안 나왔다 — 열거가 잘못됐다"


def test_four_cards_is_reachable_when_two_tags_span_all_four_groups() -> None:
    """카드 수 분포 = {2, 3, 4} — **4장이 이제 도달 가능하다.**

    이전(9전략): 태그가 최대 2개인데 매칭 그룹이 최대 2개뿐이라 4장(4그룹 동시)은
    원리적으로 불가능했다({2, 3}만 도달). GOAL_RECHECK 가 PARK 를 열면서, **한 태그가
    두 그룹에 걸치는 조합**이 가능해졌다 — 예: `["PRIORITY_SHIFT", "FATIGUE"]` 는
    FATIGUE 가 DOWNSCOPE(`DOWNSCOPE_DEFAULT`)+RESCHEDULE(`ACTIVE_RECOVERY`) 를,
    PRIORITY_SHIFT 가 CARRY_OVER(`CARRYOVER_DEFAULT`)+PARK(`GOAL_RECHECK`) 를 열어
    2태그로 4그룹이 전부 채워진다.
    """
    strategies = default_recovery_strategies()
    counts = {len(select_strategies(tags, strategies)) for tags in _all_contract_valid_inputs()}

    assert counts == {2, 3, 4}, f"도달 가능한 카드 수가 바뀌었다: {sorted(counts)}"
    assert MIN_CARDS == 2
    assert MAX_CARDS == 4

    # 위 주장의 구체 사례 — 이 조합이 실제로 4장을 낸다는 것을 직접 확인한다.
    four = select_strategies(["PRIORITY_SHIFT", "FATIGUE"], strategies)
    assert {c.option_group for c in four} == set(RECOVERY_OPTION_GROUP_VALUES)


def test_padding_rate_over_the_whole_input_space_dropped_by_more_than_half() -> None:
    """전 입력 공간의 패딩률 = 28/203 (13.8%) — 신설 전(62/186=33.3%)의 절반 이하.

    '패딩' = 그 카드의 `primary_trigger_tags` 와 사용자의 실패 사유의 교집합이 공집합.
    분모(총 카드 수)가 186→203으로 늘어난 것은 카드 수 분포에 4장 케이스가 생겼기 때문이다.

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

    assert (padding, total) == (28, 203), f"패딩률이 바뀌었다: {padding}/{total}"


def test_padding_rate_guard_actually_detects_a_removed_tag_mapping() -> None:
    """가드 — 위 패딩률 28/203 이 실제로 카탈로그 구조에 민감한가.

    하드코딩된 값을 고정하는 것만으로는 "계산이 실제로 돈다"를 증명하지 못한다 — 계산
    로직이 통째로 죽어 있어도(예: 상수를 리턴하도록 실수로 고쳐도) 우연히 값이 맞으면
    이 핀은 계속 초록이다. 여기서 카탈로그 사본에서 `TIMEBOX_REBUDGET` 의 `TIME_SHORTAGE`
    매핑을 일부러 지우고 같은 계산을 다시 돌려 값이 **실제로 달라지는지** 확인한다 —
    팀 표준 가드 패턴(`tests/test_content_registry.py` 의 스캐너 가드와 같은 성격).
    """
    strategies = default_recovery_strategies()
    target = next(s for s in strategies if s.strategy_type == "TIMEBOX_REBUDGET")
    assert "TIME_SHORTAGE" in target.primary_trigger_tags, (
        "fixture 전제가 틀렸다 — TIMEBOX_REBUDGET 이 더 이상 TIME_SHORTAGE 를 매핑하지 않는다"
    )
    target.primary_trigger_tags = [t for t in target.primary_trigger_tags if t != "TIME_SHORTAGE"]

    total = padding = 0
    for tags in _all_contract_valid_inputs():
        tag_set = set(tags)
        for card in select_strategies(tags, strategies):
            total += 1
            if _is_padding(card, tag_set):
                padding += 1

    assert (padding, total) != (28, 203), "뮤턴트인데도 정상 카탈로그와 값이 같다 — 가드가 무력하다"
    assert (padding, total) == (35, 201), f"기대한 뮤턴트 값과 다르다: {padding}/{total}"


def test_only_the_no_tag_input_gets_no_matching_card() -> None:
    """매칭 0건 입력 = **1가지뿐**(태그를 아예 안 고른 경우).

    이전(9전략): 7가지(미커버 3태그 단독 3 + 그들끼리 조합 3 + 태그 미선택 1). 신설
    4전략이 TIME_SHORTAGE/OVERRUN/AVOIDANCE 를 전부 덮으면서 6가지가 사라졌다.
    남은 1가지(태그 미선택)는 애초에 태그 매칭으로 해소할 수 없는 입력이라 그대로 둔다 —
    이 경우 `MIN_CARDS` 패딩이 여전히 "선택지를 항상 보여준다"는 원래 취지대로 동작한다.
    """
    strategies = default_recovery_strategies()
    zero_match = [
        tags
        for tags in _all_contract_valid_inputs()
        if all(_is_padding(card, set(tags)) for card in select_strategies(tags, strategies))
    ]

    assert zero_match == [[]], f"매칭 0건 입력이 바뀌었다: {zero_match}"


def test_previously_uncovered_tags_now_produce_real_matches() -> None:
    """`TIME_SHORTAGE` / `OVERRUN` / `AVOIDANCE` 단독 입력이 더 이상 패딩만 받지 않는다.

    이전(9전략)에는 이 3태그가 어떤 전략에도 안 걸려 매칭 0건·패딩 2장이었다
    (`tests/test_recovery_catalog_sync.py::test_all_thirteen_tags_are_now_covered` 가
    커버리지 자체를, 여기서는 **실제로 카드가 실매칭으로 뜨는지**를 확인한다).
    """
    strategies = default_recovery_strategies()
    expected_strategy = {
        "TIME_SHORTAGE": "TIMEBOX_REBUDGET",
        "OVERRUN": "TIMEBOX_REBUDGET",  # BUFFER_INSERT 와 같은 그룹, priority 낮은 쪽이 이김
        "AVOIDANCE": "SELF_FORGIVENESS_NANO",
    }
    for tag, expected_code in expected_strategy.items():
        cards = select_strategies([tag], strategies)
        real_matches = [c for c in cards if not _is_padding(c, {tag})]
        assert real_matches, f"{tag}: 여전히 패딩만 받는다"
        assert real_matches[0].strategy_type == expected_code, (
            f"{tag}: 선두 실매칭이 {real_matches[0].strategy_type} — 기대: {expected_code}"
        )


def test_avoidance_alone_surfaces_both_downscope_and_park_without_padding() -> None:
    """AVOIDANCE 단독 입력은 이제 **패딩 없이 2장**을 받는다(DOWNSCOPE + PARK 각 1장).

    회피는 착수 장벽(→ 축소/분해)과 목표 자체에 대한 의구심(→ 재확인) 둘 다로 이어질 수
    있다는 것이 근거 대장의 설계 의도였다(`SELF_FORGIVENESS_NANO` + `GOAL_RECHECK`).
    두 전략이 서로 다른 그룹이라 동시에 노출된다 — 같은 그룹 동시 노출 금지 규칙과 충돌하지 않는다.
    """
    strategies = default_recovery_strategies()
    cards = select_strategies(["AVOIDANCE"], strategies)

    assert len(cards) == MIN_CARDS
    assert all(not _is_padding(c, {"AVOIDANCE"}) for c in cards), "패딩이 섞여 있다"
    assert {c.option_group for c in cards} == {"DOWNSCOPE", "PARK"}
    assert {c.strategy_type for c in cards} == {"SELF_FORGIVENESS_NANO", "GOAL_RECHECK"}


def test_golden_set_padding_rate_also_dropped() -> None:
    """골든셋(120건) 패딩률 = 79/243 (32.5%) — 신설 전(141/240=58.8%)에서 절반 가까이 하락.

    여전히 전 입력 공간(13.8%)보다 높은 이유는 골든셋이 경계·적대적 케이스(태그 미선택,
    3태그 계약위반 등)를 의도적으로 과표집했기 때문이다 — 두 수치는 분모가 다르므로
    보고서에서 섞어 쓰면 안 된다.
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

    assert (padding, total) == (79, 243), f"골든셋 패딩률이 바뀌었다: {padding}/{total}"


# ── PARK_DEFAULT 동적 트리거 (overwhelm_level) ────────────────────────────
#
# 위 92건 전수 열거는 전부 overwhelm_level 을 안 넘긴다 — 그래서 PARK_DEFAULT 는 거기서
# 여전히 0/92 다(그대로 유효한 특성 기술). 아래는 그 인자를 실제로 쓸 때의 동작만 별도로
# 고정한다 — 시그니처가 새로 생긴 경로라 전수 열거에 섞으면 "무엇 때문에 바뀌었나"가
# 흐려진다.


def test_park_default_unreachable_without_overwhelm_argument() -> None:
    """`overwhelm_level` 을 아예 안 넘기면(기본값) PARK_DEFAULT 는 태그만으로 못 뜬다.

    실 데이터가 없는 지금의 프로덕션 호출부(`api/routes/recovery.py`)와 같은 조건 —
    이 테스트가 빨강이 되면 기본 동작이 바뀐 것이다.
    """
    strategies = default_recovery_strategies()
    cards = select_strategies([], strategies)
    assert "PARK_DEFAULT" not in {c.strategy_type for c in cards}


def test_park_default_reachable_via_overwhelm_dynamic_trigger() -> None:
    """`overwhelm_level >= 4` 면 태그 매칭이 하나도 없어도 PARK_DEFAULT 가 뜬다.

    카탈로그 설계("PARK_DEFAULT ← overwhelm_level >= 4")를 그대로 확인한다 — 태그 없이도
    PARK 슬롯이 패딩이 아니라 이 전략의 **실제 트리거**로 채워져야 한다.
    """
    strategies = default_recovery_strategies()
    cards = select_strategies([], strategies, overwhelm_level=4)

    park_cards = [c for c in cards if c.option_group == "PARK"]
    assert len(park_cards) == 1
    assert park_cards[0].strategy_type == "PARK_DEFAULT"


def test_park_default_trigger_respects_the_threshold_boundary() -> None:
    """임계값은 **4 이상**이다 — 3에서는 여전히 트리거되지 않는다(경계 정확성)."""
    strategies = default_recovery_strategies()
    cards = select_strategies([], strategies, overwhelm_level=3)
    assert "PARK_DEFAULT" not in {c.strategy_type for c in cards}


def test_park_default_trigger_does_not_steal_a_real_tag_match() -> None:
    """실 태그 매칭이 있으면 overwhelm 트리거는 그 자리를 뺏지 않는다.

    `AVOIDANCE` 단독 입력은 GOAL_RECHECK(같은 PARK 그룹, 실매칭)를 이미 연다
    (`test_avoidance_alone_surfaces_both_downscope_and_park_without_padding`).
    GOAL_RECHECK 의 점수(실매칭 1)와 PARK_DEFAULT 의 동적 트리거 점수(1)가 동점일 때,
    display_priority 가 더 낮은(=우선순위 높은) GOAL_RECHECK(85 < PARK_DEFAULT 90)가
    이겨야 한다 — 동점 규칙이 overwhelm 에도 그대로 적용됨을 확인한다.
    """
    strategies = default_recovery_strategies()
    cards = select_strategies(["AVOIDANCE"], strategies, overwhelm_level=4)

    park_cards = [c for c in cards if c.option_group == "PARK"]
    assert len(park_cards) == 1
    assert park_cards[0].strategy_type == "GOAL_RECHECK"


def test_is_padding_helper_cannot_see_the_overwhelm_trigger() -> None:
    """⚠️ `_is_padding` 은 태그 교집합만 본다 — overwhelm 으로 뜬 PARK_DEFAULT 도 "패딩"으로

    잡힌다(`primary_trigger_tags=[]` 라 태그 교집합이 항상 공집합이므로). 실제로는 진짜
    트리거(overwhelm>=4)로 채워진 카드인데 구조적으로 패딩과 구분이 안 된다는 뜻이다.
    L1-3 패딩률에 `overwhelm_level` 이 실제로 흘러들어오는 날, 이 함정을 먼저 볼 것 —
    `_is_padding` 을 안 고치면 정상 동작하는 PARK_DEFAULT 노출이 패딩률을 부풀린다.
    """
    strategies = default_recovery_strategies()
    cards = select_strategies([], strategies, overwhelm_level=4)
    park_default = next(c for c in cards if c.strategy_type == "PARK_DEFAULT")

    assert _is_padding(park_default, set()), (
        "이 assert 가 깨지면 _is_padding 이 이미 고쳐진 것 — 위 경고 주석을 지울 것"
    )


# ── L2 단서 전환 강제 (escalation_level) ──────────────────────────────────
#
# 근거 대장 §5.2 "ENVIRONMENT_SHIFT 선두 강제" — overwhelm_level(규칙 5, 매칭 없을 때만
# 채움)보다 강한 개입이다. 실 태그 매칭이 더 높은 점수를 가진 경우에도 밀어낸다.


def test_environment_shift_not_forced_without_escalation_argument() -> None:
    """`escalation_level` 을 아예 안 넘기면(기본값) 기존 점수 규칙 그대로 — NANO_STEP 이 이긴다."""
    strategies = default_recovery_strategies()
    cards = select_strategies(["HARD_TO_START"], strategies)
    assert cards[0].strategy_type == "NANO_STEP"


def test_environment_shift_forced_lead_even_when_a_higher_scoring_card_exists() -> None:
    """`escalation_level="L2"` 면, 다른 태그가 더 높은 점수(실매칭)를 가져도 ENVIRONMENT_SHIFT
    가 맨 앞으로 온다 — overwhelm_level 의 "매칭 없을 때만" 보다 강한 개입.

    `HARD_TO_START` 는 NANO_STEP(같은 DOWNSCOPE 그룹, 실매칭)을 이미 1등으로 올린다
    (`test_environment_shift_not_forced_without_escalation_argument`). L2 에서는 그 카드가
    선두 자리에서 빠지고 ENVIRONMENT_SHIFT 로 교체된다.
    """
    strategies = default_recovery_strategies()
    cards = select_strategies(["HARD_TO_START"], strategies, escalation_level="L2")

    assert cards[0].strategy_type == "ENVIRONMENT_SHIFT"
    assert "NANO_STEP" not in {c.strategy_type for c in cards}, (
        "동시 노출 1카드 규칙 — 같은 DOWNSCOPE 그룹에 두 카드가 같이 뜨면 안 된다"
    )


def test_environment_shift_forced_lead_preserves_other_groups() -> None:
    """DOWNSCOPE 슬롯만 교체되고, 다른 그룹 카드는 그대로 남는다(카드 개수 불변)."""
    strategies = default_recovery_strategies()
    without = select_strategies(["PRIORITY_SHIFT", "FATIGUE"], strategies)
    forced = select_strategies(["PRIORITY_SHIFT", "FATIGUE"], strategies, escalation_level="L2")

    assert len(forced) == len(without)
    assert forced[0].strategy_type == "ENVIRONMENT_SHIFT"
    other_groups_before = {
        c.option_group: c.strategy_type for c in without if c.option_group != "DOWNSCOPE"
    }
    other_groups_after = {
        c.option_group: c.strategy_type for c in forced if c.option_group != "DOWNSCOPE"
    }
    assert other_groups_before == other_groups_after


def test_environment_shift_forcing_is_noop_when_strategy_missing_from_catalog() -> None:
    """카탈로그에 `ENVIRONMENT_SHIFT` 가 없거나(비활성 포함) 조용히 no-op — 카드가 안 깨진다."""
    strategies = [
        s for s in default_recovery_strategies() if s.strategy_type != "ENVIRONMENT_SHIFT"
    ]
    with_l2 = select_strategies(["HARD_TO_START"], strategies, escalation_level="L2")
    without_l2 = select_strategies(["HARD_TO_START"], strategies)

    assert [c.strategy_type for c in with_l2] == [c.strategy_type for c in without_l2]


def test_environment_shift_forcing_only_triggers_on_l2_not_l0_or_l1() -> None:
    """`escalation_level` 이 L0/L1 이면 이 규칙은 비활성 — L2 전용이다."""
    strategies = default_recovery_strategies()
    for level in ("L0", "L1"):
        cards = select_strategies(["HARD_TO_START"], strategies, escalation_level=level)
        assert cards[0].strategy_type == "NANO_STEP", f"{level} 에서 강제가 발동했다"


# ── L1 축소→분해 (escalation_level) ───────────────────────────────────────
#
# 근거 대장 §5.2 "then_clause 를 '전체를 15분만'(축소) 금지 → '하위 단계 정확히 하나'
# (분해)" — DOWNSCOPE_DEFAULT("오늘은 절반만, 가능한 만큼만")만 이 스타일이라 그것만 뺀다.


def test_downscope_default_reachable_without_escalation_argument() -> None:
    """`escalation_level` 을 아예 안 넘기면(기본값) DOWNSCOPE_DEFAULT 가 정상적으로 뜬다."""
    strategies = default_recovery_strategies()
    cards = select_strategies(["FATIGUE"], strategies)
    assert "DOWNSCOPE_DEFAULT" in {c.strategy_type for c in cards}


def test_downscope_default_survives_at_l0() -> None:
    """`escalation_level="L0"` 이면 이 규칙은 비활성 — L1/L2 전용이다."""
    strategies = default_recovery_strategies()
    cards = select_strategies(["FATIGUE"], strategies, escalation_level="L0")
    assert "DOWNSCOPE_DEFAULT" in {c.strategy_type for c in cards}


def test_downscope_default_excluded_at_l1_falls_back_to_nano_step_via_padding() -> None:
    """L1 이면 DOWNSCOPE_DEFAULT 는 후보에서 아예 빠지고, 패딩이 다음 우선순위 DOWNSCOPE
    전략(NANO_STEP — 분해 스타일)으로 채운다. 강제 로직 없이 기존 패딩만으로 분해가 이긴다.

    `FATIGUE` 는 DOWNSCOPE_DEFAULT(DOWNSCOPE) 와 ACTIVE_RECOVERY(RESCHEDULE) 둘 다 실매칭
    시키는 태그 — DOWNSCOPE_DEFAULT 가 빠지면 DOWNSCOPE 슬롯은 실매칭 0 이 되어 패딩 대상이
    된다.
    """
    strategies = default_recovery_strategies()
    cards = select_strategies(["FATIGUE"], strategies, escalation_level="L1")

    types = {c.strategy_type for c in cards}
    assert "DOWNSCOPE_DEFAULT" not in types
    assert "NANO_STEP" in types, f"패딩이 분해 스타일로 안 채워졌다: {types}"
    assert "ACTIVE_RECOVERY" in types, "무관한 RESCHEDULE 실매칭까지 건드리면 안 된다"


def test_downscope_default_excluded_at_l2_too() -> None:
    """레벨은 아래에서 위로 누적된다(§5.2 "순서의 근거") — L2 에서도 DOWNSCOPE_DEFAULT 는
    여전히 후보에서 빠져 있어야 한다(비록 L2 자체가 ENVIRONMENT_SHIFT 로 그 슬롯을 다시
    덮어써서 관찰 가능한 카드 목록에는 어차피 안 나오지만, 배제 자체는 유지돼야 한다).
    """
    strategies = default_recovery_strategies()
    cards = select_strategies(["FATIGUE"], strategies, escalation_level="L2")
    assert "DOWNSCOPE_DEFAULT" not in {c.strategy_type for c in cards}


def test_downscope_default_exclusion_is_a_pure_removal_not_a_forced_swap() -> None:
    """분해 스타일이 원래 뭘 골랐어도(다른 전략) 억지로 NANO_STEP 을 밀어넣지 않는다 —
    "빼기만" 한다는 규칙 설명을 직접 검증. `HARD_TO_START` 단독이면 애초에 DOWNSCOPE_DEFAULT
    가 후보도 아니라 L1 을 켜도 카드가 완전히 동일해야 한다.
    """
    strategies = default_recovery_strategies()
    without = select_strategies(["HARD_TO_START"], strategies)
    with_l1 = select_strategies(["HARD_TO_START"], strategies, escalation_level="L1")
    assert [c.strategy_type for c in with_l1] == [c.strategy_type for c in without]
