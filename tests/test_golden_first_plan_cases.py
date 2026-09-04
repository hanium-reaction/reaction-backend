"""첫 계획 골든셋(L1-7) 무결성 — 회복·자료 골든셋과 같은 규약.

고정하는 것:

1. **커밋된 파일 == 생성기 출력.** 생성기를 고치고 파일을 안 다시 만들면 여기서 깨진다.
2. **블록별 건수**가 생성기 docstring 의 표와 일치한다.
3. **`defect_free_control` 이 루브릭 §3 의 회귀 4종을 실제로 갖는다** — 태그만 붙은 게
   아니라 저장된 계획이 그 속성을 실제로 만족하는지 계획을 열어 확인한다.
4. **심은 결함이 기준 계획을 실제로 바꿨다**(뮤테이션 가드). 이게 없으면 op 이 no-op 이어도
   `seeded_defect` 20건이 전부 초록이라, 검토기 recall 이 0 인 것과 결함이 없는 것을
   구별하지 못한다.
5. **결함이 ③층 불변식을 깨지 않는다.** 주입이 세션 길이 상한을 넘겨버리면 검토기가
   *구조적으로 발화 불가여야 할* 이유로 반려할 수 있고, 그러면 M27 이 오염된다
   (`test_first_plan_verifier_invariants.py` 가 고정한 §1.1 을 여기서 이어받는다).
6. **held-out 출처가 기록돼 있다.** 결함을 누가/무엇을 보고 썼는지가 파일에 남지 않으면
   L1-7B 의 순환성 완화 주장이 검증 불가능한 말이 된다.
"""

from __future__ import annotations

import json
import re

import pytest
from scripts import build_golden_first_plan_cases as builder
from scripts import check_seeded_defect_shortcuts as shortcuts

from reaction_backend.orchestrator import first_plan_adapter
from reaction_backend.schemas.planning import MilestoneDraft

# 룰이 붙인 채움 세션은 **제목이 아니라 `node_id` 접두사**로 판정한다.
# 사용자 작업 제목에도 "3회차" 가 실제로 들어 있어(`control-cert-standard` 의 기출 카드)
# 제목 정규식으로 거르면 진짜 사용자 작업이 같이 날아간다 — `eval/README.md` 의 경고.
_FILLER_NODE_PREFIX = "tmp-continue-"


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return builder.build_cases()


@pytest.fixture(scope="module")
def on_disk() -> list[dict]:
    text = builder.OUTPUT_PATH.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ── 1. 재현성 ─────────────────────────────────────────────────────────────


def test_file_on_disk_matches_the_generator(cases: list[dict]) -> None:
    """커밋된 골든셋이 생성기 출력과 **바이트 단위로** 같다."""
    assert builder.OUTPUT_PATH.exists(), (
        f"{builder.OUTPUT_PATH.name} 이 없다 — "
        "uv run python -m scripts.build_golden_first_plan_cases 를 돌리고 커밋할 것"
    )
    assert builder.OUTPUT_PATH.read_text(encoding="utf-8") == builder.to_jsonl(cases), (
        "커밋된 파일과 생성기 출력이 다르다 — 생성기를 고쳤으면 파일도 다시 만들어 커밋할 것"
    )


def test_generator_is_deterministic(cases: list[dict]) -> None:
    """두 번 돌려 같은 결과가 나온다 — 난수·현재시각이 새어 들어오면 깨진다."""
    assert builder.to_jsonl(builder.build_cases()) == builder.to_jsonl(cases)


# ── 2. 구성 ───────────────────────────────────────────────────────────────


def test_block_counts_match_the_spec(on_disk: list[dict]) -> None:
    counts: dict[str, int] = {}
    for case in on_disk:
        counts[case["block"]] = counts.get(case["block"], 0) + 1
    assert counts == builder.EXPECTED_COUNTS
    assert len(on_disk) == builder.EXPECTED_TOTAL


def test_case_ids_are_unique(on_disk: list[dict]) -> None:
    ids = [c["case_id"] for c in on_disk]
    assert len(ids) == len(set(ids)), "case_id 중복 — 하네스 결과가 덮어써진다"


def test_every_case_is_marked_synthetic(on_disk: list[dict]) -> None:
    """보고서에서 합성 비율을 숨길 수 없게 한다 (`eval/README.md` 규약)."""
    assert all(c["synthetic"] is True for c in on_disk)


def test_no_absolute_dates_leak_into_cases(on_disk: list[dict]) -> None:
    """마감은 `deadline_offset_days` 상대값뿐이다.

    절대 날짜를 넣으면 하루만 지나도 '마감 임박'이 '마감 지남'이 되어 판정이 뒤집힌다
    (`s10_corners.py` 전례, `eval/README.md`).
    """
    blob = json.dumps(on_disk, ensure_ascii=False)
    leaked = re.findall(r"\d{4}-\d{2}-\d{2}", blob)
    assert leaked == [], f"절대 날짜가 케이스에 들어갔다: {sorted(set(leaked))[:5]}"


def test_decompose_cases_carry_slots_and_no_plan(on_disk: list[dict]) -> None:
    for case in on_disk:
        if case["kind"] != "decompose":
            continue
        assert "plan" not in case, f"{case['case_id']}: 분해 케이스가 계획을 들고 있다"
        assert case["interview"]["goal"]["title"]


def test_verify_cases_carry_a_plan(on_disk: list[dict]) -> None:
    for case in on_disk:
        if case["kind"] != "verify":
            continue
        plan = case["plan"]
        assert plan["action_items"], f"{case['case_id']}: 빈 계획"
        node_ids = {n["node_id"] for n in plan["goal_nodes"]}
        for item in plan["action_items"]:
            assert item["node_id"] in node_ids, (
                f"{case['case_id']}: action_item 이 없는 노드를 가리킨다 ({item['node_id']})"
            )


# ── 3. 경계값 격자 ────────────────────────────────────────────────────────


def test_constraint_edge_is_a_real_grid_around_the_capacity(on_disk: list[dict]) -> None:
    """`constraint_edge` 가 계획서가 요구한 ±5분 격자를 실제로 이룬다.

    격자점 하나하나가 반려율 곡선의 x 축이므로, 앵커별로 -5/0/+5 가 다 있어야
    "경계에서 급등하는가"를 물을 수 있다.
    """
    grid: dict[int, set[int]] = {}
    for case in on_disk:
        if case["block"] != "constraint_edge":
            continue
        edge = case["edge"]
        grid.setdefault(edge["anchor_min"], set()).add(edge["offset_min"])

    assert set(grid) == set(builder._EDGE_ANCHORS)
    for anchor, offsets in grid.items():
        assert offsets == set(builder._EDGE_OFFSETS), f"앵커 {anchor} 의 격자가 불완전하다"


def test_edge_offset_zero_actually_lands_on_the_capacity(on_disk: list[dict]) -> None:
    """offset=0 케이스의 집중 용량이 앵커와 같다.

    ⚠️ 15분 앵커의 -5(=10분)만 예외다 — `session_min_for` 의 하한이 15라 10 은 15로
    끌어올려진다. 그 경로 자체가 격자에 있어야 하는 이유이므로 예외로 통과시키되,
    **격자점이 실제로는 같은 용량**이라는 사실을 여기 못박는다.
    """
    for case in on_disk:
        if case["block"] != "constraint_edge":
            continue
        edge, goal = case["edge"], case["interview"]["goal"]
        slots = builder._EDGE_BASE._replace(session_length_min=goal["session_length_min"])
        capacity = first_plan_adapter.session_min_for(builder._outcome(slots))

        if edge["offset_min"] == 0:
            assert capacity == edge["anchor_min"]
        elif goal["session_length_min"] < 15:
            assert capacity == 15, "하한이 15가 아니게 됐다 — 격자 해석이 바뀐다"
        else:
            assert capacity == goal["session_length_min"]


# ── 4. 무결함 대조군 — 태그가 아니라 계획을 확인한다 ──────────────────────


def _control(on_disk: list[dict]) -> list[dict]:
    return [c for c in on_disk if c["block"] == "defect_free_control"]


def test_control_block_covers_every_required_regression_property(on_disk: list[dict]) -> None:
    """루브릭 §3 이 요구한 회귀 4종이 대조군에 **전부** 있다."""
    present: set[str] = set()
    for case in _control(on_disk):
        present.update(case["control_properties"])
    missing = set(builder.REQUIRED_CONTROL_PROPERTIES) - present
    assert not missing, f"대조군이 못 덮는 회귀 속성: {sorted(missing)}"


def test_control_properties_are_true_of_the_stored_plan(on_disk: list[dict]) -> None:
    """태그만 붙이고 계획은 그렇지 않은 상태를 막는다.

    이게 없으면 `control_properties` 는 주석일 뿐이라, 대조군이 조용히 성질을 잃어도
    위 테스트가 초록이다 — 그러면 M29 의 분모가 '오탐이 나올 수 있는 계획'이 아니게 된다.
    """
    plans = builder.base_plans()
    for case in _control(on_disk):
        slots, _ = plans[case["case_id"]]
        outcome = builder._outcome(slots)
        minutes = [a["estimated_minutes"] for a in case["plan"]["action_items"]]
        filler = [
            a for a in case["plan"]["action_items"] if a["node_id"].startswith(_FILLER_NODE_PREFIX)
        ]
        props = set(case["control_properties"])
        cid = case["case_id"]

        if "session_equals_capacity" in props:
            capacity = first_plan_adapter.session_min_for(outcome)
            assert max(minutes) == capacity, (
                f"{cid}: 사용자 상한과 정확히 같은 세션이 없다 — 120분 사고를 재현할 수 없다"
            )
        if "sub_15_is_normal" in props:
            assert first_plan_adapter.planned_session_min_for(outcome) < 15, (
                f"{cid}: 이 조합의 평균 세션 길이가 15분 이상이 됐다 — 픽스처가 낡았다"
            )
            assert min(minutes) < 15, f"{cid}: 15분 미만 카드가 사라졌다"
        if "has_repeat_sessions" in props:
            assert filler, f"{cid}: 룰이 붙인 `N회차` 세션이 없다 — D1 오탐 회귀를 못 잡는다"
        if "mixed_lengths" in props:
            assert len(set(minutes)) > 1, f"{cid}: 길이가 전부 같다 — 정상 편차 회귀가 아니다"


def test_control_cases_expect_approval(on_disk: list[dict]) -> None:
    """대조군의 정답은 전부 승인이다 — 여기서 나온 반려가 곧 M29 의 분자다."""
    assert all(c["expected"]["approved"] is True for c in _control(on_disk))


# ── 5. 심은 결함 ──────────────────────────────────────────────────────────


def _seeded(on_disk: list[dict]) -> list[dict]:
    return [c for c in on_disk if c["block"] == "seeded_defect"]


def test_every_defect_code_and_level_is_covered(on_disk: list[dict]) -> None:
    """D1~D5 × easy/boundary × 기준계획 2개 격자가 빈칸 없이 찬다.

    M27 은 **유형별로** 보고해야 하므로(루브릭 §5), 한 유형이 비면 그 칸은 영원히 미측정이다.
    """
    grid = {
        (c["seeded"]["defect"], c["seeded"]["level"], c["seeded"]["base_plan"])
        for c in _seeded(on_disk)
    }
    expected = {
        (code, level, base)
        for code in builder.DEFECT_CODES
        for level in builder.DEFECT_LEVELS
        for base in builder.SEED_BASE_KEYS
    }
    assert grid == expected, f"빈칸: {sorted(expected - grid)} / 잉여: {sorted(grid - expected)}"


def test_seeded_target_nodes_exist_in_the_plan(on_disk: list[dict]) -> None:
    """M28 localization 의 정답 좌표가 실제 계획 안의 노드여야 한다."""
    for case in _seeded(on_disk):
        node_ids = {n["node_id"] for n in case["plan"]["goal_nodes"]}
        for target in case["seeded"]["target_node_ids"]:
            assert target in node_ids, (
                f"{case['case_id']}: 존재하지 않는 노드를 정답으로 지목한다 ({target})"
            )


def test_seeded_defect_actually_changes_the_base_plan(on_disk: list[dict]) -> None:
    """뮤테이션 가드 — 주입이 no-op 이면 여기서 잡는다.

    ⚠️ 이 테스트가 없으면 op 이 아무것도 안 해도 20건이 전부 초록이고, 검토기 recall 이
    0 으로 나와도 "검토기가 못 잡는다"인지 "애초에 결함이 없다"인지 구별할 수 없다.
    """
    plans = builder.base_plans()
    for case in _seeded(on_disk):
        _, base = plans[case["seeded"]["base_plan"]]
        base_payload = builder._plan_payload(base)
        assert case["plan"] != base_payload, (
            f"{case['case_id']}: 주입이 기준 계획을 바꾸지 않았다 (no-op)"
        )


def test_seeded_defects_do_not_break_the_layer3_invariant(on_disk: list[dict]) -> None:
    """주입된 결함이 세션 길이 상한을 넘지 않는다.

    넘기면 검토기가 **구조적으로 발화 불가여야 할** 항목(루브릭 §1.1)으로 반려할 수 있고,
    그 반려가 D1~D5 탐지로 잘못 집계된다. 결함 유형을 하나만 심는다는 계약이 깨지는 것.
    """
    plans = builder.base_plans()
    for case in _seeded(on_disk):
        slots, _ = plans[case["seeded"]["base_plan"]]
        capacity = first_plan_adapter.session_min_for(builder._outcome(slots))
        over = [
            a["title"] for a in case["plan"]["action_items"] if a["estimated_minutes"] > capacity
        ]
        assert not over, f"{case['case_id']}: 주입이 상한({capacity}분)을 넘겼다 — {over}"


def test_seeded_defects_respect_layer3_volume_caps(on_disk: list[dict]) -> None:
    """주입된 계획이 ③층의 **총량** 상한 두 개를 지킨다 — 개수(케이던스)와 분 예산.

    ⚠️ 위 `..._do_not_break_the_layer3_invariant` 는 **항목별** 분 상한만 본다. 총량은
    항목을 하나도 상한 위로 올리지 않으면서 초과할 수 있다 — `insert_item` 이 정확히 그
    경로였다. 2026-09-02 감사에서 20건 중 **8건**(d1·d5 의 easy/boundary × cert/portfolio)이
    개수·분 예산을 동시에 넘긴 상태였고, 28개 테스트가 전부 통과하면서 그랬다.

    이게 왜 골든셋을 무효로 만드는가: 루브릭 §1.2 는 총 분량·세션 개수를 "③층이 보장하므로
    ④층이 검사할 필요 없는 것" 으로 면제해 뒀다. 그 면제가 데이터에서 거짓이면 검토기의
    반려가 심은 결함 때문인지 총량 초과 때문인지 못 가르고, `boundary`(통과가 정답) 케이스의
    반려가 오탐으로, `easy` 케이스의 반려가 D1/D5 탐지 성공으로 잘못 집계된다(M27 부풀림).
    """
    plans = builder.base_plans()
    for case in _seeded(on_disk):
        slots, _ = plans[case["seeded"]["base_plan"]]
        outcome = builder._outcome(slots)
        items = case["plan"]["action_items"]

        cap = first_plan_adapter.cadence_session_cap(
            outcome, "standard", target_date=builder.BASE_DATE
        )
        if cap is not None:
            assert len(items) <= cap, (
                f"{case['case_id']}: 세션 {len(items)}개 > 케이던스 상한 {cap}개 — "
                "③층이 못 만드는 계획이다"
            )

        budget = first_plan_adapter.horizon_minute_budget(
            outcome, "standard", target_date=builder.BASE_DATE
        )
        total = sum(a["estimated_minutes"] or 0 for a in items)
        # `_take_within_budget` 의 출력 불변식은 **`total <= budget` 이거나 항목이 1개**다.
        # 첫 항목은 예산을 넘어도 버려지지 않지만(계획이 통째로 비는 것보다 낫다는 판단),
        # 그 분량은 `used` 에 그대로 누적돼 **두 번째 항목부터는 첫 항목까지 포함해** 검사된다
        # (`if kept and used + item > budget_min: break`). 따라서 예산을 넘긴 계획이 2개 이상의
        # 항목을 가질 수는 없다.
        #
        # ⚠️ 처음엔 이 자리에 `total - head < budget` 이라고 썼는데 **틀렸다.** 첫 항목을
        # 빼고 세면 `[120, 15]`(예산 100) 같은 2항목 계획을 통과시키는데, 프로덕션은
        # `[120]` 만 남긴다. 탐지기로서 얼마나 샜는지 실측: 이 커밋이 고친 8건 중
        # **1건만** 잡았다(나머지는 위 개수 단언이 먼저 터져서 빨강이 됐을 뿐이다).
        # `cadence_session_cap` 은 빈도가 없으면 `None` 을 돌려주므로, 그런 기준 계획에
        # 결함이 추가되면 개수 단언이 없어 이 단언 혼자 서게 된다.
        assert total <= budget or len(items) == 1, (
            f"{case['case_id']}: 총 {total}분 > 분 예산 {budget}분인데 항목이 {len(items)}개 — "
            "③층이 못 만드는 계획이다 (예산 초과는 항목 1개일 때만 가능)"
        )


def test_sibling_order_index_is_unique_within_each_parent(on_disk: list[dict]) -> None:
    """같은 부모 아래 `order_index` 는 유일하다 — **이 골든셋 안에서**.

    ⚠️ **근거 정정 (2026-09-02 감사).** 원래 이 자리에 "③층이 enumerate 로 매기므로
    프로덕션 트리에는 중복이 없다" 고 썼는데 **사실이 아니다.** 계획 트리의 `order_index` 는
    LLM 초안 값을 그대로 복사한다(`first_plan_adapter.py` `n.order_index = nd.order_index`).
    enumerate 는 채움 leaf · 마일스톤 트리 · 룰 폴백 계획 빌더(`first_plan.py`, 분해 타임아웃
    시 프로덕션 경로)에만 쓰인다 — 정상 LLM 경로에는 없다. 프로덕션도 중복을 낼 수 있다.

    그래도 이 불변식을 유지하는 이유는 다르다: 이 골든셋의 기준 계획은 `_raw_plan` 이
    enumerate 로 만든 것이라 중복이 없고, 주입이 그 성질을 깨면 결함 작성자가 의도하지
    않은 **두 번째 변화**가 섞여 검토기의 반려 원인을 못 가른다. 골든셋을 좁히는 선택이지
    프로덕션 보장의 재현이 아니다.

    ⚠️ 이 불변식은 2026-09-01 감사에서 **실제로 깨져 있었다** — `insert_item` 이 뒤 형제를
    안 밀어 4건에서 중복이 났고, 삽입 지점이 branch 끝인 케이스는 우연히 피해가서 데이터에
    따라 나타났다 사라졌다 했다. 세션 길이 불변식만 보던 테스트로는 안 잡혔다.
    """
    for case in on_disk:
        if case["kind"] != "verify":
            continue
        by_parent: dict[str, list[tuple[str, int]]] = {}
        for node in case["plan"]["goal_nodes"]:
            parent = node["parent_id"]
            if parent is None:
                continue
            by_parent.setdefault(parent, []).append((node["node_id"], node["order_index"]))
        for parent, kids in by_parent.items():
            indexes = [i for _, i in kids]
            assert len(indexes) == len(set(indexes)), (
                f"{case['case_id']}: 부모 {parent} 아래 order_index 가 중복된다 ({sorted(kids)}) "
                "— ③층이 만들 수 없는 트리다"
            )


def test_boundary_cases_are_expected_to_pass(on_disk: list[dict]) -> None:
    """`boundary` 는 '덜 심한 결함'이 아니라 **결함처럼 보이는 정상**이다.

    정답이 통과이므로, 여기서 나온 반려는 M29 와 같은 성격의 오탐이다. easy 만으로는
    120분 사고 같은 '정당한 값에 대한 반려'를 재현할 수 없어 이 수준을 따로 둔다.
    """
    for case in _seeded(on_disk):
        expected_approved = case["seeded"]["level"] == "boundary"
        assert case["expected"]["approved"] is expected_approved


# ── 6. held-out 출처 ──────────────────────────────────────────────────────


def test_seeded_defects_record_held_out_provenance() -> None:
    """결함을 누가·무엇을 보고 썼는지가 파일에 남는다.

    계획서 L1-7B 의 순환성 완화(held-out fault design)는 **기록이 없으면 검증 불가능한
    주장**이다. 보고서에 "다른 주체가 설계했다"고 쓰려면 그 조건이 레포에 있어야 한다.
    """
    seeded = builder.load_seeded_defects()
    prov = seeded["provenance"]
    for field in ("author_model", "authored_at", "shown", "withheld", "verified_by"):
        assert prov.get(field), f"provenance.{field} 가 비어 있다"

    assert prov["author_model"] != prov["rubric_author_model"], (
        "결함 작성자와 루브릭 작성자가 같은 모델이다 — held-out 이 아니다"
    )
    withheld = " ".join(prov["withheld"])
    assert "rubric-first-plan-v1.md" in withheld, (
        "루브릭 앵커를 가렸다는 기록이 없다 — 가리지 않았다면 held-out 이 성립하지 않는다"
    )


def test_seeded_defect_entries_are_well_formed() -> None:
    seeded = builder.load_seeded_defects()
    ids = [d["defect_id"] for d in seeded["defects"]]
    assert len(ids) == len(set(ids)) == builder.EXPECTED_COUNTS["seeded_defect"]
    for entry in seeded["defects"]:
        assert entry["defect"] in builder.DEFECT_CODES
        assert entry["level"] in builder.DEFECT_LEVELS
        assert entry["base_plan"] in builder.SEED_BASE_KEYS
        assert entry["rationale"].strip(), f"{entry['defect_id']}: 근거가 비었다"
        assert entry["operation"]["op"] in {
            "replace_title",
            "replace_first_step",
            "swap_order",
            "insert_item",
        }


# ── 6. 지름길 탐지 — 판정은 `scripts/check_seeded_defect_shortcuts.py` 에 위임한다 ─────
#
# ⚠️ 판정 로직을 여기 복붙하면 두 곳이 갈린다. 실제로 3차 재의뢰 때 검사기에는 있고
# 테스트에는 없는 특징이 생겼고, 작성자가 **검사기가 세는 것만** 지웠다. 단일 진실 소스로
# 둔다 — 스크립트는 pytest 없이도 돌아야 하므로(결함 작성자가 직접 검사한다) 그쪽이 원본이다.
#
# 검사기는 **0 을 목표로 하지 않는다.** 아는 지름길은 `KNOWN_SHORTCUTS` 에 사유와 함께
# 등록돼 있고, 그것들은 `eval/README.md` 「M27·M28 을 읽을 때」에서 지표 해석의 한계로
# 보고된다. 이 테스트가 지키는 것은 **새로 생기지 않는 것**이다.


def test_no_new_shortcut_appears(on_disk: list[dict]) -> None:
    """기준선에 없는 **새** 지름길이 생기면 빨강."""
    exact_bad, range_bad, lexical_bad = shortcuts.find_offenders(on_disk)
    new, _known = shortcuts.split_by_baseline(exact_bad + range_bad + lexical_bad)
    joined = "\n  ".join(new)
    assert not new, (
        f"이 변경이 **새** 지름길을 만들었다 — 검토기가 내용을 안 읽고 그 유형의 "
        f"M27 을 얻을 수 있다:\n  {joined}"
    )


def test_known_shortcut_baseline_is_not_stale(on_disk: list[dict]) -> None:
    """기준선에 있는데 더 이상 안 잡히는 항목이 없다 — 목록이 사실과 어긋나면 안 된다.

    등록된 지름길은 **보고서가 인용할 한계**다. 데이터가 좋아져서 사라졌는데 목록에 남아
    있으면 없는 한계를 보고하게 되고, 반대로 목록이 낡으면 래칫이 헐거워진다.
    """
    exact_bad, range_bad, lexical_bad = shortcuts.find_offenders(on_disk)
    _new, known = shortcuts.split_by_baseline(exact_bad + range_bad + lexical_bad)
    stale = sorted(set(shortcuts.KNOWN_SHORTCUTS) - {shortcuts._key_of(x) for x in known})
    joined = "\n  ".join(stale)
    assert not stale, f"KNOWN_SHORTCUTS 에 있는데 이제 안 잡힌다 — 목록에서 지울 것:\n  {joined}"


# ── 7. 마일스톤 — M23·M24 가 계산 가능한가 ─────────────────────────────────


def _slots_from_case(case: dict) -> builder.Slots:
    """저장된 인터뷰 페이로드로 `Slots` 를 되짚는다 — 하네스가 하는 것과 같은 복원."""
    interview, goal = case["interview"], case["interview"]["goal"]
    return builder.Slots(
        key=case["case_id"],
        title=goal["title"],
        category=goal["category"],
        success_image=goal["success_image"],
        current_level=goal["current_level"],
        deadline_offset_days=goal["deadline_offset_days"],
        session_length_min=goal["session_length_min"],
        weekly_hours=goal["weekly_hours"],
        frequency_per_week=goal["frequency_per_week"],
        focus_duration_min=interview["focus_duration_min"],
        role=interview["role"],
        season=interview["season"],
        preferred_time=interview["preferred_time"],
        approach_note=goal.get("approach_note"),
    )


def test_milestone_block_actually_carries_milestones(on_disk: list[dict]) -> None:
    """`milestone_fixed` 6건이 확정 마일스톤 목록을 **실제로** 담는다.

    ⚠️ 2026-09-02 이전에는 블록 이름만 `milestone_fixed` 이고 마일스톤이 **한 건도**
    없었다. 그래서 M23 의 분모가 66건 전부에서 0 이었고 `drop_out_of_cycle_branches` 가
    발화하지 않아 M24 도 못 쟀다 — **M26(L1-7A 1차 지표)이 정의되지 않은 항을 AND 로
    묶고 있었다.** 이름과 내용이 어긋나 있어서 아무도 몰랐다.
    """
    block = [c for c in on_disk if c["block"] == "milestone_fixed"]
    assert len(block) == builder.EXPECTED_COUNTS["milestone_fixed"]
    for case in block:
        milestones = case["interview"].get("milestones")
        assert milestones, f"{case['case_id']}: 확정 마일스톤이 없다 — M23 분모가 0 이 된다"
        for m in milestones:
            assert m["title"].strip(), f"{case['case_id']}: 제목이 빈 마일스톤"
        cursor = case["interview"]["milestone_cursor"]
        assert 0 <= cursor < len(milestones), (
            f"{case['case_id']}: 커서 {cursor} 가 목록({len(milestones)}) 밖 — 이번 주기가 빈다"
        )


def test_milestone_cases_make_m23_and_m24_computable(on_disk: list[dict]) -> None:
    """마일스톤 케이스가 **프로덕션 함수를 실제로 발화시킨다.**

    담고만 있고 파이프라인이 안 쓰면 지표는 여전히 계산 불가다. 그래서 케이스를 세는 게
    아니라 `cycle_milestone_window`(M23 의 분모)와 `drop_out_of_cycle_branches`(M24)를
    직접 돌려 결과가 비지 않는지 본다.
    """
    exercised: dict[str, int] = {}
    for case in [c for c in on_disk if c["block"] == "milestone_fixed"]:
        interview = case["interview"]
        outcome = builder._outcome(_slots_from_case(case))
        all_ms = [
            MilestoneDraft(title=m["title"], summary=m["summary"]) for m in interview["milestones"]
        ]
        # ⚠️ `horizon_weeks` 를 **프로덕션과 같은 함수로 구한다.** 2026-09-02 에 여기 `2` 를
        # 하드코딩했다가 감사 5차에 걸렸다 — 2 는 만다라 유래 목표 전용 값이고
        # (`max_plan_weeks_for(is_mandala_derived=True)`), 이 케이스들은 전부 기본
        # `_MAX_PLAN_WEEKS`(4)를 쓴다. 그 탓에 창 크기가 실제보다 작게 나와 **아래 assert 가
        # 거짓 초록**이었다. 하드코딩된 상수로 프로덕션 동작을 흉내내면 안 된다.
        horizon_weeks = first_plan_adapter._horizon_weeks(builder.BASE_DATE, outcome.horizon)
        window = first_plan_adapter.cycle_milestone_window(
            all_ms,
            cursor=interview["milestone_cursor"],
            horizon_weeks=horizon_weeks,
            full_horizon_weeks=first_plan_adapter.full_horizon_weeks(
                builder.BASE_DATE, outcome.horizon
            ),
        )
        assert window, f"{case['case_id']}: 이번 주기 마일스톤이 비었다 — M23 분모가 0 이다"

        # 전체 마일스톤을 branch 로 갖는 원안을 만들면, 구간 밖은 잘려나가야 한다.
        raw = builder._raw_plan(
            [
                builder.Step(
                    branch=m.title,
                    title=f"{m.title} 1단계",
                    minutes=interview["goal"]["session_length_min"] or 50,
                    first_step="자료 열기",
                )
                for m in all_ms
            ],
            goal_title=interview["goal"]["title"],
            category=interview["goal"]["category"],
        )
        _kept, dropped = first_plan_adapter.drop_out_of_cycle_branches(raw, window)
        exercised[case["case_id"]] = len(dropped)

    # ⚠️ **케이스마다 잘리기를 요구하면 안 된다.** `cycle_milestone_window` 의 규칙이
    # `round(남은 마일스톤 × 이번 창 ÷ 남은 기간)` 이라, 마감이 계획 지평(4주) 안에 있는
    # 목표는 창이 전부를 덮는 게 **맞는 동작**이다 — 다음 주기가 없으니 미룰 것도 없다.
    #
    # 처음엔 전 케이스에 `assert dropped` 를 걸었는데, 그건 위에서 `horizon_weeks=2` 를
    # 하드코딩했을 때만 초록이었다(감사 5차). 실제 값으로는 `milestone-contest`(마감 21일,
    # 지평 3주)·`milestone-defense` 가 **정당하게** 0 을 낸다. 잘못된 상수가 잘못된
    # 단언을 가려주고 있었다.
    #
    # 그래서 블록 **전체**가 M24 를 잴 수 있는지를 본다 — 구간이 진부분집합인 케이스가
    # 없으면 이 블록은 범위 이탈을 영원히 못 잰다.
    measurable = {k: v for k, v in exercised.items() if v > 0}
    assert len(measurable) >= 2, (
        f"구간 밖 branch 가 잘리는 케이스가 {len(measurable)}건뿐이다 — M24 를 못 잰다. "
        f"케이스별 잘린 수: {exercised}. 마감이 계획 지평 안인 목표는 창이 전부를 덮는 게 "
        "정상이므로, **마감이 먼 목표**가 블록에 남아 있어야 한다"
    )


# ── 8. M18 이 양방향으로 성립하는가 ────────────────────────────────────────


def test_m18_is_two_sided_for_some_cases(on_disk: list[dict]) -> None:
    """분량 비율(M18)이 **1.0 을 넘을 수 있는** 케이스가 골든셋에 있어야 한다.

    ⚠️ **2026-09-02 1차 실행은 이 성질이 없는 채로 돌았다.** `planned_session_min_for` 는
    `min(주당분 ÷ 빈도, 집중용량)` 인데, 주당 시간이 넉넉하면 그 값이 **집중용량으로
    클램프**된다. 그러면 프롬프트가 "평균 세션 길이" 와 "세션 길이 상한" 을 **같은 숫자**로
    인쇄하고, LLM 이 개수를 지키면서 상한만 안 넘겨도

        총 분량 ≤ 세션수 × 상한 = 세션수 × 평균 = 예산

    이 되어 **M18 이 산술적으로 1.0 을 넘을 수 없다.** 34케이스 중 32개가 그 상태였고
    102행 중 88행이 1.0 초과 불가였다. 그 상태에서 낸 "과소 생성 85/102" 는 모델의 행동이
    아니라 **슬롯 구성의 성질**이었다(`docs/experiments/l1-7-results.md` §6.1).

    양방향이 아닌 지표로 "과소 생성" 을 주장하면 안 된다. 이 테스트는 그 주장을 할 수 있는
    최소 조건 — **상한이 안 걸리는 케이스가 실재하는가** — 을 고정한다.
    """
    two_sided: list[str] = []
    for case in on_disk:
        if case["kind"] != "decompose":
            continue
        outcome = builder._outcome(_slots_from_case(case))
        if first_plan_adapter.planned_session_min_for(outcome) < first_plan_adapter.session_min_for(
            outcome
        ):
            two_sided.append(case["case_id"])

    decompose = [c for c in on_disk if c["kind"] == "decompose"]
    assert len(two_sided) >= 6, (
        f"M18 이 1.0 을 넘을 수 있는 케이스가 {len(two_sided)}건뿐이다 "
        f"({len(decompose)}건 중) — 그러면 M18 은 단방향 지표이고 '과소 생성' 을 주장할 수 "
        f"없다. `weekly_hours ÷ frequency_per_week < session_length_min` 인 조합을 넣을 것. "
        f"현재 가능한 케이스: {sorted(two_sided)}"
    )


def test_control_block_is_large_enough_for_the_m29_threshold(on_disk: list[dict]) -> None:
    """무결함 대조군이 **30건 이상**이어야 한다 — M29 의 사전등록 임계값을 확인할 수 있게.

    M29(`false_reject_rate`)는 실험계획서 §5 에서 **유일하게 절대 임계값이 사전 고정된**
    축이다(≤ 0.10). 그런데 표본이 작으면 **0건 반려여도 그 임계값을 확인했다고 말할 수
    없다**. 0건일 때의 단측 95% 이항 상한은 `1 − 0.05^(1/n)` 이다:

        12건 → 0건 반려여도 상한 0.221   ← "≤0.10 을 확인했다" 고 못 한다
        30건 → 0건 반려면   상한 0.095   ← 비로소 말할 수 있다

    ⚠️ rule of three(`3/n`)는 **근사**다. 0/30 을 `3/30 = 0.100` 으로 적으면 임계값과
    같은 값이 되어 "겨우 걸친다" 로 읽히는데, 정확값은 **0.095** 로 임계값 아래다.
    문서에는 정확값을 쓴다.

    ⚠️ **반복 3회를 90건으로 세면 안 된다.** 같은 케이스의 반복은 상관이 있어 독립 표본이
    아니다. 분모는 **케이스 수**다.

    ⚠️ 그리고 30건은 **0건 반려일 때만** 성립하는 하한이다. 1건이라도 반려되면
    (1/30 = 0.033) Clopper-Pearson 상한이 다시 0.10 을 넘는다 — 그때는 표본을 더 늘려야 한다.

    ⚠️ **반복 3회를 90건으로 세면 안 된다.** 같은 케이스의 반복은 상관이 있어 독립 표본이
    아니다. **M29 의 1차 추정 단위는 30개 고유 대조군의 사전 지정된 1회 호출**이고,
    2·3회차는 M32(일관성)와 보조 분석 몫이다.
    """
    controls = [c for c in on_disk if c["block"] == "defect_free_control"]
    assert len(controls) >= 30, (
        f"무결함 대조군이 {len(controls)}건뿐이다 — 0건 반려여도 단측 95% 상한이 "
        f"{1 - 0.05 ** (1 / max(len(controls), 1)):.3f} 라 M29 의 사전등록 임계값(≤0.10)을 "
        f"확인할 수 없다. "
        "케이스를 30건 이상으로 늘릴 것 (반복은 분모가 아니다)."
    )


def test_control_block_spans_multiple_domains(on_disk: list[dict]) -> None:
    """대조군이 한 도메인에 몰려 있지 않다.

    M29 는 "검토기가 **정상 계획**을 얼마나 해치는가" 인데, 대조군이 학습 계열에만 있으면
    그 수치는 학습 도메인의 오탐률일 뿐이다. 표본을 늘리면서 도메인을 같이 넓히지 않으면
    숫자만 커지고 주장 범위는 그대로다.
    """
    cats = {
        c["interview"]["goal"]["category"] for c in on_disk if c["block"] == "defect_free_control"
    }
    assert len(cats) >= 5, f"대조군 카테고리가 {sorted(cats)} 뿐이다 — 도메인을 넓힐 것"
