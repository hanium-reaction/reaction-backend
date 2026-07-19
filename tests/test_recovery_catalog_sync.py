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

_SEED_FILE = (
    Path(reaction_backend.__file__).parent.parent.parent
    / "alembic"
    / "versions"
    / "d09c105520b5_seed_master_data_v0_7_1_failure_tags_.py"
)


def _load_seed_module() -> Any:
    spec = importlib.util.spec_from_file_location("seed_master_data", _SEED_FILE)
    assert spec is not None and spec.loader is not None, f"시드 파일 없음: {_SEED_FILE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conftest_strategies_mirror_seed_exactly() -> None:
    """conftest 카탈로그 미러 == 시드 마이그레이션. 필드 단위 전수 대조.

    한쪽만 고치면 여기서 터진다 — 시드를 바꾸는 마이그레이션은 반드시 미러도 함께.
    """
    import json

    seed = _load_seed_module()
    seed_by_code = {s["code"]: s for s in seed.RECOVERY_STRATEGIES}
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


def test_uncovered_tags_are_a_design_decision_not_a_gap() -> None:
    """primary_trigger_tags 에 안 걸리는 태그 = 정확히 {TIME_SHORTAGE, OVERRUN, AVOIDANCE}.

    이것은 **갭이 아니라 DB 설계서 §6.10 의 설계**다(`docs/erd-diff.md` 매핑표와 일치):
    - PARK_DEFAULT 는 태그가 아니라 **동적 조건**(context_snapshot.overwhelm_level ≥ 4)으로
      트리거하도록 설계됐고, 그 캡처는 #19-B-2 유예 중이다.
    - 미커버 태그는 select_strategies 의 패딩("항상 선택지를 보여준다")이 받는다.

    이 집합을 바꾸려면(예: TIME_SHORTAGE→RESCHEDULE 정적 매핑 추가) 설계서 §6.10 과
    `docs/erd-diff.md` 를 함께 개정하고 이 테스트를 의식적으로 갱신할 것 — DoD 문구만 보고
    시드를 '보강'하는 실수를 막는 마지막 방어선이다.
    """
    strategies = default_recovery_strategies()
    covered = {tag for s in strategies for tag in (s.primary_trigger_tags or [])}
    all_tags = {t.tag_code for t in default_failure_tags()}

    assert all_tags - covered == {"TIME_SHORTAGE", "OVERRUN", "AVOIDANCE"}
    # 커버되는 태그가 유령을 참조하지 않는다 (오타 방어).
    assert covered <= all_tags, f"존재하지 않는 태그를 참조: {covered - all_tags}"
