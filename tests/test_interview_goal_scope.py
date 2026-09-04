"""목표 이름을 **누구에게 보여줄지**가 두 경로에서 같은가 (#427).

`identity.*` · `time.*` · `recovery.*` 는 **모든 목표에 공통으로 적용되는 전역 설정**이다.
그 질문에 목표 이름이 붙으면 사용자는 그 설정이 **그 목표에만 적용된다고 오해한다**
(#187 과교정 실측: 실 LLM 3회에 8건).

경로가 둘이라 갈릴 수 있다:

    LLM 경로   `ask_question()` → `{{goal_title}}` 변수
    룰 폴백    `_rule_next_question()` → `default_questions` 의 `{goal}` 자리

**폴백은 처음부터 데이터로 올바르게 분기하고 있었고, LLM 경로만 늘 이름을 넘긴 채
프롬프트가 산문으로 가르쳤다.** 산문 규칙은 측정된 회귀의 가드라 살아 있지만,
규칙은 **어길 수 있고 어겨도 조용하다** — 이 파일은 그 위에 "어길 이름을 주지 않는다"
는 층을 못 박는다.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from reaction_backend.orchestrator import interview
from reaction_backend.orchestrator.interview_catalog import (
    CATALOGS,
    GLOBAL_SCOPE_HINT,
    is_goal_scoped,
)

_GOAL = "토익 900점"


# ── 1. 두 경로가 같은 집합을 본다 ───────────────────────────────────────────


@pytest.mark.parametrize("kind", sorted(CATALOGS))
def test_predicate_matches_the_fallback_question_placeholders(kind: str) -> None:
    """`is_goal_scoped` 와 `default_questions` 의 `{goal}` 자리가 **정확히 같은 집합**이다.

    갈리면 LLM 질문과 폴백 질문이 **다른 대상을 가리킨다** — 사용자는 같은 슬롯을
    두 번 보면서 한 번은 목표 이름을, 한 번은 지시어를 본다.
    """
    catalog = CATALOGS[kind]
    with_placeholder = {k for k, v in catalog.default_questions.items() if "{goal}" in v}
    predicate_says = {k for k in catalog.default_questions if is_goal_scoped(k)}
    assert with_placeholder == predicate_says, (
        f"[{kind}] 두 경로가 갈렸다 — "
        f"폴백에만 있음 {sorted(with_placeholder - predicate_says)} / "
        f"판정에만 있음 {sorted(predicate_says - with_placeholder)}"
    )


def test_the_two_collector_slots_are_excluded() -> None:
    """`goals.list` · `goals.heaviest` 는 **여러 목표를 다루는 자리**라 제외다.

    보기 중 하나를 질문의 대상 명사로 지목하면 나머지를 배제하게 된다 —
    프롬프트의 "이 중에서 지금 가장 무겁게 느껴지는 건 어떤 거예요?" 규칙과 충돌한다.
    """
    assert not is_goal_scoped("goals.list")
    assert not is_goal_scoped("goals.heaviest")
    assert is_goal_scoped("goals.session_length")


@pytest.mark.parametrize(
    "slot_key",
    [
        "identity.role",
        "identity.season",
        "time.activity_window",
        "time.peak_window",
        "recovery.tone",
        "recovery.rest_ok",
        "recovery.downscope_unit",
    ],
)
def test_global_slots_are_never_goal_scoped(slot_key: str) -> None:
    """실제로 과교정이 관측된 슬롯들 — 회귀 반례로 박아 둔다."""
    assert not is_goal_scoped(slot_key)


# ── 2. LLM 경로가 이름을 넘기지 않는다 ──────────────────────────────────────


def _goal_title_sent_for(slot_key: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """`ask_question()` 이 그 슬롯에서 실제로 넘긴 `goal_title` 값."""
    sent: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> Any:
        sent.update(kwargs["variables"])
        raise AssertionError("변수만 본다")

    monkeypatch.setattr(interview.aiClient, "run", _capture)
    # 슬롯 **선택**은 이 테스트의 관심사가 아니다(FSM 이 한다). 대상 슬롯만 고정한다.
    monkeypatch.setattr(interview, "_next_required_slot", lambda state: slot_key)

    state = interview.initial_state(session_id=uuid.uuid4(), user_id=uuid.uuid4(), kind="plan")
    state["slot_answers"]["goals.heaviest"] = {"type": "text", "raw": _GOAL}
    with pytest.raises(AssertionError):
        asyncio.run(interview.ask_question(state, {}))
    return str(sent["goal_title"])


def test_goal_scoped_slot_receives_the_real_name(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _goal_title_sent_for("goals.session_length", monkeypatch) == _GOAL


@pytest.mark.parametrize(
    "slot_key", ["time.activity_window", "recovery.tone", "identity.role", "goals.heaviest"]
)
def test_non_goal_scoped_slot_never_receives_the_name(
    slot_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ 이것이 이 PR 의 핵심이다 — **어길 이름을 주지 않는다.**"""
    sent = _goal_title_sent_for(slot_key, monkeypatch)
    assert _GOAL not in sent, f"[{slot_key}] 전역 설정 질문에 목표 이름이 넘어갔다"
    assert sent == GLOBAL_SCOPE_HINT


def test_the_hint_carries_the_instruction_not_a_name() -> None:
    """자리표시자 자체가 무엇을 하지 말지 말한다 — 프롬프트를 안 봐도 읽힌다."""
    assert "전역 설정" in GLOBAL_SCOPE_HINT
    assert "목표 이름을 문장에 넣지 마라" in GLOBAL_SCOPE_HINT


# ── 3. 프롬프트의 산문 규칙은 **지우지 않았다** ─────────────────────────────


def test_the_measured_prose_guards_are_still_in_the_prompt() -> None:
    """산문 규칙은 **측정된 회귀의 가드**다 — 변수를 막았다고 지우지 않는다.

    #187 은 두 번 측정됐다: (1) 지시어만 써서 사용자가 대상을 몰랐던 회귀,
    (2) 그걸 고치자 전역 슬롯에 이름이 붙은 **과교정**(실 LLM 3회에 8건).
    변수를 막는 것은 (2)만 막는다 — (1)을 지키는 것은 여전히 프롬프트다.
    """
    from reaction_backend.prompts import registry

    body = registry.get("interview/next_question").body
    assert "목표별 슬롯이면 어느 목표를 묻는지 질문에 드러내라" in body
    assert "슬롯키가 `goals.` 로 시작하지 않으면 목표 이름을 문장에 절대 넣지 마라" in body
    assert "{{goal_title}}" in body, "변수를 넘기는데 프롬프트가 안 쓰면 렌더가 터진다"
