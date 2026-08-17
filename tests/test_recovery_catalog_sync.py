"""회복 전략 카탈로그 3자 동기화 — 시드(alembic) ↔ 테스트 픽스처(conftest) ↔ 설계(§6.10).

왜 필요한가:
- 회복 라우트 테스트는 전부 conftest 의 `default_recovery_strategies()`(시드 미러)를 쓴다.
  미러가 시드와 어긋나면 **모든 회복 테스트가 프로덕션에 없는 카탈로그를 검증**하게 된다 —
  fake 전면대체 패턴의 고전적 함정이고, 이 어긋남을 잡는 장치가 지금까지 없었다.
- 태그→전략 매핑은 DB 설계서 v0.7.1 §6.10(레포 요약: `docs/erd-diff.md`)이 진실 소스다.
  "매핑에 없는 태그 = 시드 갭"으로 오독하고 시드를 '보강'하면 설계서와 어긋난다 —
  실제로 그 실수가 제안 단계까지 갔다(#20 DoD 6 감사). 미커버가 **설계**임을 여기 핀한다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import reaction_backend
from tests.conftest import default_failure_tags, default_recovery_strategies

_VERSIONS_DIR = Path(reaction_backend.__file__).parent.parent.parent / "alembic" / "versions"
_SEED_FILE = _VERSIONS_DIR / "d09c105520b5_seed_master_data_v0_7_1_failure_tags_.py"

# recovery_strategy_catalog 는 두 마이그레이션에 걸쳐 채워진다 — 원본 9전략(d09c105520b5)
# + 태그 구멍/PARK 도달 경로를 메우는 신설 4전략(8680c4567ca6, PR #257).
# 시드가 갈라지면 이 목록에 새 파일을 추가하고 `_load_all_seeded_strategies` 가 합친다.
_STRATEGY_SEED_FILES = (
    _SEED_FILE,
    _VERSIONS_DIR / "8680c4567ca6_seed_recovery_strategy_gap_fill.py",
)


def _load_seed_module(path: Path = _SEED_FILE) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None, f"시드 파일 없음: {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_all_seeded_strategies() -> list[dict[str, Any]]:
    """RECOVERY_STRATEGIES 를 정의하는 모든 시드 마이그레이션을 합친다."""
    rows: list[dict[str, Any]] = []
    for path in _STRATEGY_SEED_FILES:
        module = _load_seed_module(path)
        rows.extend(module.RECOVERY_STRATEGIES)
    return rows


def test_conftest_strategies_mirror_seed_exactly() -> None:
    """conftest 카탈로그 미러 == **두 시드 마이그레이션의 합**. 필드 단위 전수 대조.

    한쪽만 고치면 여기서 터진다 — 시드를 바꾸는 마이그레이션은 반드시 미러도 함께.
    """
    import json

    seed_by_code = {s["code"]: s for s in _load_all_seeded_strategies()}
    fixture_by_code = {s.strategy_type: s for s in default_recovery_strategies()}

    assert set(seed_by_code) == set(fixture_by_code), "전략 코드 집합이 다르다"
    for code, row in seed_by_code.items():
        s = fixture_by_code[code]
        assert s.option_group == row["group"], f"{code}: group 불일치"
        assert s.label_ko == row["label_ko"], f"{code}: label 불일치"
        assert s.if_then_template == row["template"], f"{code}: template 불일치"
        assert s.min_recovery_unit_minutes == row["min_unit"], f"{code}: min_unit 불일치"
        assert s.primary_trigger_tags == json.loads(str(row["primary_tags"])), (
            f"{code}: primary_trigger_tags 불일치 — 룰 엔진 테스트 전체가 거짓이 된다"
        )
        assert s.allow_rest_mode == row["allow_rest"], f"{code}: allow_rest 불일치"
        assert s.display_priority == row["display_priority"], f"{code}: priority 불일치"


def test_conftest_failure_tags_mirror_seed_exactly() -> None:
    """13종 실패 태그 미러 == 시드 (코드·라벨·순서·활성)."""
    seed = _load_seed_module()
    seed_by_code = {t["code"]: t for t in seed.FAILURE_REASON_TAGS}
    fixture_by_code = {t.tag_code: t for t in default_failure_tags()}

    assert set(seed_by_code) == set(fixture_by_code), "태그 코드 집합이 다르다"
    for code, row in seed_by_code.items():
        t = fixture_by_code[code]
        assert t.label_ko == row["label_ko"], f"{code}: label 불일치"
        assert t.sort_order == row["sort_order"], f"{code}: sort_order 불일치"


def test_all_thirteen_tags_are_now_covered() -> None:
    """primary_trigger_tags 가 13태그 전부를 덮는다 (2026-08-17, 8680c4567ca6 이후).

    이 테스트는 **이전 설계를 뒤집은 결정**을 고정한다. 과거엔 `test_uncovered_tags_are_a_design_decision_not_a_gap`
    이 `{TIME_SHORTAGE, OVERRUN, AVOIDANCE}` 를 "갭이 아니라 설계"로 핀했다 —
    `tests/test_recovery_selection_coverage.py` 의 92개 입력 전수 열거가 그 설계의 실제
    귀결(패딩 카드만 받는 사용자, PARK 그룹 도달 불가 0/92)을 드러낸 뒤, 근거 대장
    (`docs/research/recovery-evidence-base.md` §4.1)이 신설 4전략으로 메우기로 했다.

    PARK_DEFAULT 는 여전히 `primary_trigger_tags=[]` 다 — **동적 조건**
    (`context_snapshot.overwhelm_level ≥ 4`)으로 트리거하도록 설계됐고, 그 캡처는
    #19-B-2 유예 중이다. PARK 자체는 GOAL_RECHECK(정적 태그)로 도달 가능해졌지만,
    PARK_DEFAULT 개별 전략은 여전히 미도달이다 — `select_strategies` 가 overwhelm 을
    인자로 받지 않기 때문(시그니처 확장은 이 PR 범위 밖).

    이 집합을 다시 바꾸려면(신규 태그 추가 등) 설계서 §6.10 과 `docs/erd-diff.md` 를
    함께 개정하고 이 테스트를 의식적으로 갱신할 것.
    """
    strategies = default_recovery_strategies()
    covered = {tag for s in strategies for tag in (s.primary_trigger_tags or [])}
    all_tags = {t.tag_code for t in default_failure_tags()}

    assert all_tags - covered == set(), f"아직 미커버: {all_tags - covered}"
    # 커버되는 태그가 유령을 참조하지 않는다 (오타 방어).
    assert covered <= all_tags, f"존재하지 않는 태그를 참조: {covered - all_tags}"


def test_park_default_itself_still_lacks_a_static_trigger() -> None:
    """PARK_DEFAULT(9전략 원본)는 여전히 `primary_trigger_tags=[]` — 지운 게 아니라

    동적 조건(overwhelm) 구현을 기다리는 중이다. GOAL_RECHECK(신설)가 PARK 그룹 자체는
    도달 가능하게 만들었지만, PARK_DEFAULT 라는 개별 전략은 아직 정적 태그로 못 뜬다.
    """
    strategies = {s.strategy_type: s for s in default_recovery_strategies()}
    assert strategies["PARK_DEFAULT"].primary_trigger_tags == []
    assert strategies["PARK_DEFAULT"].option_group == "PARK"
    # GOAL_RECHECK 가 같은 그룹에서 실제 도달 경로를 맡는다.
    assert strategies["GOAL_RECHECK"].option_group == "PARK"
    assert strategies["GOAL_RECHECK"].primary_trigger_tags != []


def test_carry_over_copy_does_not_promise_a_different_day() -> None:
    """CARRY_OVER 문구는 '다음 주'를 약속하지 않는다 — 날짜 규칙은 결정일 +1일 (#175).

    문구는 DB(카탈로그), 날짜는 코드(`recovery_target_date`) — 소스가 둘이라 다시 갈라질 수
    있다. 실제로 FREEZE_SLOT 이 '슬롯 예약 (다음 주)' 인데 +1일에 배치돼, 사용자가 HITL 로
    승인한 것과 다른 날짜가 나갔다. 여기서 두 소스의 방향을 고정한다.

    '비워두고 예약(hold)'도 금지어다 — 스키마에 슬롯 예약 개념이 없어 구현되지 않는다.
    """
    from datetime import date, timedelta

    from reaction_backend.orchestrator.recovery import recovery_target_date

    decided_on = date(2026, 7, 29)
    carry_over = [s for s in default_recovery_strategies() if s.option_group == "CARRY_OVER"]
    assert carry_over, "CARRY_OVER 전략이 사라졌다"

    for s in carry_over:
        assert recovery_target_date(decided_on, s.option_group) == decided_on + timedelta(days=1)
        for copy in (s.label_ko, s.if_then_template):
            assert "다음 주" not in copy, f"{s.strategy_type}: 문구가 코드 규칙(+1일)과 어긋난다"
            assert "비워두" not in copy, f"{s.strategy_type}: 슬롯 예약(hold)은 구현되지 않는다"
