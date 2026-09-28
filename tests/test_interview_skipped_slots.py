"""건너뛴 슬롯은 "사용자가 답한 값" 이 아니다 — `unresolved_slots` 에 남아야 한다 (#499).

사용자가 '없어요/모르겠어요/건너뛰기' 로 닫은 슬롯에는 `interview._SKIP_MARKER`(빈 text)가
저장된다. `is_filled_answer` 는 이를 **충족**으로 읽는데, 그건 옳다 — 미충족으로 두면 FSM 이
같은 질문을 영원히 반복한다(스킵 마커가 생긴 이유 자체가 그 무한 루프다, #79).

문제는 `unresolved_slots` 가 그 술어 하나로 계산됐다는 것이다. 스킵 슬롯이 정의상 목록에서
빠지고, `profile_memory.persist_profile_from_outcome` 의 가드가 그 목록을 "안 답한 칸" 으로
믿는다 — 그래서 `build_outcome` 이 채운 **안전 기본값이 사용자의 답으로 영속됐다**:

    활동창 09:00~23:00 · 톤 '담백' · 최소 단위 10분 · 휴식 수용 '네' · 피크 파생

그리고 재인터뷰의 프로필 오버레이가 그 값을 시드로 넣어 **그 칸을 다시 묻지 않는다.**
`profile_memory.py` 의 가드 주석이 막겠다고 선언한 사고가 스킵 경로로 그대로 났다.

이 파일은 네 가지를 함께 고정한다 — 고친 것과, **고치면서 깨면 안 되는 것**:

1. 스킵 슬롯이 `unresolved_slots` 에 남는다 (plan · ultimate).
2. 그 슬롯의 기본값이 프로필 어디에도 저장되지 않는다.
3. FSM 은 스킵 슬롯을 다시 묻지 않는다 — `open_required_keys` 는 종전 그대로다.
4. FE 명료성 지표도 스킵 슬롯을 세지 않는다 — 진행바가 100% 에서 되돌아가지 않는다.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from reaction_backend.orchestrator import interview, interview_adapter, ultimate_adapter
from reaction_backend.orchestrator import profile_memory as pm
from reaction_backend.orchestrator.interview_catalog import PLAN_CATALOG

# 사용자가 실제로 답한 슬롯 — 대조군. 이 값들은 종전대로 프로필에 남아야 한다.
_ANSWERED = {
    "identity.role": {"type": "chip", "values": ["3학년"]},
    "time.peak_window": {"type": "chip", "values": ["저녁"]},
}


def _skipped(*keys: str) -> dict[str, Any]:
    return {k: dict(interview._SKIP_MARKER) for k in keys}


# ── 1. 스킵 슬롯이 `unresolved_slots` 에 남는다 ──────────────────────────────


def test_skipped_slot_is_reported_as_defaulted() -> None:
    """스킵으로 닫힌 필수 슬롯은 `unresolved_slots` 에 남는다 — 값은 서버 기본값이다."""
    outcome = interview_adapter.build_outcome(
        session_id="s1",
        slot_answers={**_ANSWERED, **_skipped("time.activity_window", "recovery.tone")},
        ambiguity_final=0.0,
        end_reason=cast(Any, "completed"),
        analysis_source="llm",
    )

    assert "time.activity_window" in outcome.unresolved_slots
    assert "recovery.tone" in outcome.unresolved_slots
    # 실제로 답한 슬롯은 안 들어간다 — 이 목록은 "기본값으로 채워진 칸" 이다.
    assert "time.peak_window" not in outcome.unresolved_slots
    # 그리고 outcome 자체는 기본값을 들고 있다(그래서 이 목록이 필요하다).
    assert outcome.availability.activity_window.start == "09:00"
    assert outcome.availability.activity_window.end == "23:00"


def test_core_goal_slot_skipped_three_times_still_opens_the_follow_up() -> None:
    """핵심 슬롯도 같다 — 목표를 못 정한 사용자가 '완료 100%' 로 조용히 넘어가면 안 된다.

    `goals.list` 에 '모르겠어요' 를 상한까지 답하면 스킵 마커가 찍힌다(`interview.py`).
    그 키가 남아야 First Plan 의 보완 분기(`first_plan_adapter` 가 `unresolved_slots` 를
    프롬프트로 싣는다)가 열린다.
    """
    outcome = interview_adapter.build_outcome(
        session_id="s1",
        slot_answers={**_ANSWERED, **_skipped("goals.list")},
        ambiguity_final=0.0,
        end_reason=cast(Any, "completed"),
        analysis_source="llm",
    )

    assert "goals.list" in outcome.unresolved_slots


def test_ultimate_outcome_uses_the_same_rule() -> None:
    outcome = ultimate_adapter.build_ultimate_outcome(
        session_id="s1",
        slot_answers=_skipped("ultimate.statement", "ultimate.domain"),
        ambiguity_final=0.0,
        end_reason=cast(Any, "completed"),
        analysis_source="llm",
    )

    assert "ultimate.statement" in outcome.unresolved_slots
    assert "ultimate.domain" in outcome.unresolved_slots


def test_skip_marker_shape_is_pinned_to_the_predicate() -> None:
    """`_SKIP_MARKER` 의 모양이 바뀌면 판정이 같이 깨지도록 못 박는다.

    둘이 다른 파일에 있어서, 마커만 바꾸면 이 결함이 조용히 되살아난다.
    """
    assert interview_adapter.is_skipped_answer(interview._SKIP_MARKER)
    assert interview_adapter.is_filled_answer(interview._SKIP_MARKER)  # FSM 쪽 계약
    assert not interview_adapter.is_skipped_answer({"type": "chip", "values": ["저녁"]})
    assert not interview_adapter.is_skipped_answer({"type": "text", "raw": "2026-05-01"})
    assert not interview_adapter.is_skipped_answer(None)


# ── 2. 스킵 슬롯의 기본값이 프로필에 안 들어간다 ────────────────────────────


class _RecordingProfileRepo:
    calls: dict[str, list[dict[str, Any]]] = {}

    def __init__(self, session: Any) -> None:  # noqa: D107
        pass

    async def upsert_behavioral(self, user_id: Any, *, fields: dict[str, Any]) -> None:
        type(self).calls.setdefault("behavioral", []).append(fields)

    async def upsert_interaction(self, user_id: Any, *, fields: dict[str, Any]) -> None:
        type(self).calls.setdefault("interaction", []).append(fields)


def _persist(monkeypatch: Any, user: Any, slot_answers: dict[str, Any]) -> dict[str, Any]:
    _RecordingProfileRepo.calls = {}
    monkeypatch.setattr(pm, "ProfileRepo", _RecordingProfileRepo)
    outcome = interview_adapter.build_outcome(
        session_id="s1",
        slot_answers=slot_answers,
        ambiguity_final=0.0,
        end_reason=cast(Any, "completed"),
        analysis_source="llm",
    )
    asyncio.run(pm.persist_profile_from_outcome(cast(Any, None), user=user, outcome=outcome))
    return _RecordingProfileRepo.calls


def test_skipped_slots_do_not_become_the_users_profile(monkeypatch: Any) -> None:
    """⚠️ 이 테스트가 이 PR 의 본론이다 — 묻지 않은 값이 사용자의 답으로 굳지 않는다.

    기존 `test_early_finish_does_not_write_defaults_into_the_profile` 는 슬롯 **행 자체가
    없는** 경우다. 여기서는 행이 있고 값이 스킵 마커다 — 그 차이가 구멍이었다.
    """
    user = cast(Any, SimpleNamespace(id="u1", tone_mode=None, focus_mode_preferences=None))

    calls = _persist(
        monkeypatch,
        user,
        _skipped(
            "time.activity_window",
            "time.peak_window",
            "recovery.tone",
            "recovery.rest_ok",
            "recovery.downscope_unit",
        ),
    )

    assert calls.get("behavioral") is None, "스킵한 칸으로 behavioral 행을 만들면 안 된다"
    assert calls.get("interaction") is None
    assert not (user.focus_mode_preferences or {})
    assert user.tone_mode is None, "톤을 건너뛴 사용자의 말투를 서버가 정하면 안 된다"


def test_answered_slots_are_still_persisted_alongside_skipped_ones(monkeypatch: Any) -> None:
    """가드는 스킵한 칸만 거른다 — 같은 세션에서 답한 칸은 그대로 저장된다."""
    user = cast(Any, SimpleNamespace(id="u1", tone_mode=None, focus_mode_preferences={}))

    calls = _persist(
        monkeypatch,
        user,
        {
            "time.peak_window": {"type": "chip", "values": ["저녁"]},
            "recovery.tone": {"type": "chip", "values": ["따뜻"]},
            **_skipped("time.activity_window", "recovery.rest_ok", "recovery.downscope_unit"),
        },
    )

    behavioral = calls.get("behavioral", [{}])[0]
    assert behavioral.get("energy_cycle") == "evening"
    assert "preferred_start_time" not in behavioral, "건너뛴 활동창이 같이 실렸다"
    assert "recovery_speed_type" not in behavioral
    assert calls.get("interaction", [{}])[0].get("recovery_tone") == "gentle"
    assert not (user.focus_mode_preferences or {})


# ── 3·4. 고치면서 깨면 안 되는 것 ───────────────────────────────────────────


def test_fsm_does_not_ask_a_skipped_slot_again() -> None:
    """스킵 슬롯은 FSM 에게 '끝난 칸' 이다 — 아니면 같은 질문이 무한 반복된다(#79).

    `unresolved_slots` 를 고치려고 `open_required_keys` 를 같이 고치면 여기서 잡힌다.
    """
    answers = {k: dict(interview._SKIP_MARKER) for k in PLAN_CATALOG.required_keys}

    assert interview_adapter.open_required_keys(PLAN_CATALOG.required_keys, answers) == []


def test_clarity_metric_does_not_count_skipped_slots() -> None:
    """FE 명료성 지표(남은 질문 수)도 스킵 칸을 세지 않는다 — 진행바가 되돌아가면 안 된다."""
    answers = {
        **_ANSWERED,
        **_skipped("time.activity_window"),
    }

    remaining = interview_adapter.open_required_keys(PLAN_CATALOG.required_keys, answers)

    assert "time.activity_window" not in remaining
