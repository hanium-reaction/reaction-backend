"""정책 후보 산출 룰 (`orchestrator/policy_update.py`, #168) — 순수 함수.

DB·LLM 없이 KPI → 파라미터 변환만 검증한다. 라우트 배선은 `test_policy_snapshot.py`,
실 SQL 은 `test_policy_snapshot_sql.py`.
"""

from __future__ import annotations

from reaction_backend.orchestrator.policy_update import (
    PolicyCandidate,
    PolicyInputs,
    baseline_policy,
    build_candidate,
)


def _current(**overrides: object) -> PolicyCandidate:
    execution = {"daily_max_load": 180, "buffer_ratio": 0.2, "no_touch_zones": []}
    execution.update(overrides.get("execution", {}))  # type: ignore[arg-type]
    return PolicyCandidate(
        behavioral_profile={"attention_span": 30, "success_buffer": 0.0},
        execution_constraints=execution,
        interaction_style={"recovery_tone": "normal"},
        recovery_policy={"min_recovery_step_minutes": 10},
    )


def test_no_kpi_means_no_change() -> None:
    """지표가 전부 None 이면 아무것도 바꾸지 않는다 — 억지로 새 버전을 만들지 않는다."""
    out = build_candidate(_current(), PolicyInputs())
    assert out.changes == []
    assert out.reason_for_update is None
    assert out.execution_constraints["daily_max_load"] == 180


def test_low_adherence_lowers_daily_load() -> None:
    out = build_candidate(_current(), PolicyInputs(adherence_rate=0.4))
    assert out.execution_constraints["daily_max_load"] == 144  # 180 * 0.8
    change = next(c for c in out.changes if c.field_name == "daily_max_load")
    assert change.before == 180
    assert change.after == 144
    assert "40%" in change.why, f"근거에 실제 숫자가 없다 — {change.why}"


def test_high_adherence_raises_daily_load() -> None:
    """학습 루프는 양방향이어야 한다 — 잘 지키면 조금 늘린다."""
    out = build_candidate(_current(), PolicyInputs(adherence_rate=0.95))
    assert out.execution_constraints["daily_max_load"] == 198  # 180 * 1.1


def test_mid_adherence_leaves_load_alone() -> None:
    """경계 사이(0.6~0.9)는 건드리지 않는다 — 흔들리는 보정은 신뢰를 깎는다."""
    for rate in (0.6, 0.75, 0.9):
        out = build_candidate(_current(), PolicyInputs(adherence_rate=rate))
        assert not [c for c in out.changes if c.field_name == "daily_max_load"], rate


def test_daily_load_is_clamped_at_the_floor() -> None:
    """룰이 반복돼도 극단값으로 안 간다 — 하한 60분."""
    out = build_candidate(
        _current(execution={"daily_max_load": 60}), PolicyInputs(adherence_rate=0.1)
    )
    assert out.execution_constraints["daily_max_load"] == 60
    assert not [c for c in out.changes if c.field_name == "daily_max_load"], (
        "변화 없으면 기록도 없다"
    )


def test_low_resilience_shrinks_recovery_step() -> None:
    out = build_candidate(_current(), PolicyInputs(resilience_rate=0.2))
    assert out.recovery_policy["min_recovery_step_minutes"] == 5
    assert any(c.field_name == "min_recovery_step_minutes" for c in out.changes)


def test_recovery_step_has_a_floor() -> None:
    current = PolicyCandidate(
        behavioral_profile={},
        execution_constraints={},
        interaction_style={},
        recovery_policy={"min_recovery_step_minutes": 5},
    )
    out = build_candidate(current, PolicyInputs(resilience_rate=0.1))
    assert out.recovery_policy["min_recovery_step_minutes"] == 5
    assert out.changes == []


def test_high_delay_raises_success_buffer() -> None:
    out = build_candidate(_current(), PolicyInputs(avg_delay_minutes=45.0))
    assert out.behavioral_profile["success_buffer"] == 0.1


def test_drain_window_is_proposed_as_no_touch_zone() -> None:
    out = build_candidate(_current(), PolicyInputs(drain_point_window="수요일 오후"))
    assert out.execution_constraints["no_touch_zones"] == ["수요일 오후"]


def test_drain_window_is_not_added_twice() -> None:
    """이미 들어 있는 구간은 다시 제안하지 않는다 — 매주 같은 변경이 뜨면 안 된다."""
    current = _current(execution={"no_touch_zones": ["수요일 오후"]})
    out = build_candidate(current, PolicyInputs(drain_point_window="수요일 오후"))
    assert out.changes == []


def test_multiple_signals_accumulate() -> None:
    out = build_candidate(
        _current(),
        PolicyInputs(
            adherence_rate=0.3,
            resilience_rate=0.2,
            avg_delay_minutes=40.0,
            drain_point_window="목요일 저녁",
        ),
    )
    fields = {c.field_name for c in out.changes}
    assert fields == {
        "daily_max_load",
        "min_recovery_step_minutes",
        "success_buffer",
        "no_touch_zones",
    }
    assert out.reason_for_update is not None
    assert len(out.reason_for_update) <= 200, "reason_for_update 는 VARCHAR(200)"


def test_build_candidate_does_not_mutate_the_input() -> None:
    """순수 함수 — 현재 정책 dict 를 제자리에서 고치면 호출자의 활성 스냅샷이 오염된다."""
    current = _current()
    build_candidate(current, PolicyInputs(adherence_rate=0.1, drain_point_window="월요일 아침"))
    assert current.execution_constraints["daily_max_load"] == 180
    assert current.execution_constraints["no_touch_zones"] == []


def test_baseline_uses_profile_values_when_present() -> None:
    """v1 은 인터뷰가 채운 값에서 출발한다 — 낯선 기본값이 아니라."""

    class _Behavioral:
        attention_span = 45
        energy_cycle = "evening"
        time_chunk_preference = "60"
        success_buffer = 0.15

    class _Interaction:
        suggestion_style = "firm"
        recovery_tone = "gentle"
        explanation_depth = "detailed"
        reminder_frequency = "minimal"

    out = baseline_policy(behavioral=_Behavioral(), interaction=_Interaction())
    assert out.behavioral_profile["attention_span"] == 45
    assert out.behavioral_profile["energy_cycle"] == "evening"
    assert out.interaction_style["recovery_tone"] == "gentle"
    assert out.interaction_style["reminder_frequency"] == "minimal"


def test_baseline_falls_back_when_profiles_are_missing() -> None:
    """프로필 행이 아직 없어도(온보딩 중) 후보를 만들 수 있어야 한다."""
    out = baseline_policy(behavioral=None, interaction=None)
    assert out.behavioral_profile["attention_span"] == 30
    assert out.execution_constraints["daily_max_load"] == 180
    assert out.recovery_policy["min_recovery_step_minutes"] == 10
    assert out.changes == []
