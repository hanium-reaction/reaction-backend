"""사용자가 쓴 말은 금지어 치환·톤 게이트가 고치거나 버리지 않는다 (llm-1·llm-2·planB-2).

실 경로(`aiClient.run`)를 태운다 — provider 만 가짜로 바꾼다. 두 방향을 **같이** 고정한다:

- 사용자 원문(프롬프트 변수)을 LLM·룰 폴백이 그대로 옮겨 쓴 자리 → 그대로 둔다.
- AI 가 스스로 쓴 금지어·톤 마커 → 전과 똑같이 치환·reject 된다(AGENTS §2 — 필터를 끄지 않는다).
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel

from reaction_backend.llm import aiClient
from reaction_backend.llm.provider import ProviderResponse
from reaction_backend.prompts import registry
from reaction_backend.schemas.interview import AnswerIntake
from reaction_backend.schemas.planning import ActionItemDraft, GoalDecomposition, GoalNodeDraft

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _vars(prompt_id: str, **values: str) -> dict[str, str]:
    """프롬프트가 요구하는 변수를 전부 채운다(렌더 실패로 no_prompt 폴백이 나지 않게)."""
    names = set(_VAR.findall(registry.get(prompt_id).body))
    out = dict.fromkeys(names, "(없음)")
    out.update(values)
    return out


def _fake_provider(monkeypatch: pytest.MonkeyPatch, value: BaseModel) -> None:
    async def fake_generate_structured(**kwargs: object) -> tuple[BaseModel, ProviderResponse]:
        return value, ProviderResponse(raw_text="{}", tokens_in=10, tokens_out=5, model="fake")

    monkeypatch.setattr(
        "reaction_backend.llm.tool_executor.generate_structured", fake_generate_structured
    )


def _decomposition(root: str, leaf: str, item_title: str, first_step: str) -> GoalDecomposition:
    return GoalDecomposition(
        goal_nodes=[
            GoalNodeDraft(
                node_id="n0",
                parent_id=None,
                title=root,
                node_type="root",
                order_index=0,
                is_leaf=False,
            ),
            GoalNodeDraft(
                node_id="n1",
                parent_id="n0",
                title=leaf,
                node_type="leaf",
                order_index=0,
                is_leaf=True,
            ),
        ],
        action_items=[
            ActionItemDraft(
                node_id="n1",
                title=item_title,
                estimated_minutes=30,
                category="study",
                first_step=first_step,
            )
        ],
    )


# ── llm-2: 금지어 치환이 사용자 답을 바꿔 저장하지 않는다 ─────────────────


async def test_answer_intake_keeps_the_users_own_answer_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """미러 실측: 이 답이 '두 번의 창업 한 번 멈춤를 딛고 잠깐 쉬어가는하지 않는…' 로 저장됐다."""
    answer = "두 번의 창업 실패를 딛고 포기하지 않는 창업가가 되기"
    _fake_provider(
        monkeypatch,
        AnswerIntake(
            slot_key="ultimate.statement",
            clarity_score=0.9,
            new_ambiguity=0.1,
            normalized_value=answer,
        ),
    )

    result = await aiClient.run(
        module="interview",
        schema=AnswerIntake,
        prompt_id="interview/answer_intake",
        fallback=lambda: AnswerIntake(slot_key="x", clarity_score=0, new_ambiguity=1),
        variables=_vars("interview/answer_intake", answer=answer),
        timeout=1.0,
    )

    assert result.fell_back is False
    assert result.value.normalized_value == answer
    assert result.banned_hits == ()


async def test_ai_authored_banned_word_is_still_replaced_next_to_user_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """같은 호출 안에서 AI 가 스스로 쓴 '포기하지 마세요' 는 사용자 답에 '포기하지'가 있어도 치환."""
    goal = "포기하지 않고 영어 회화 끝내기"
    _fake_provider(
        monkeypatch,
        _decomposition(
            root=goal,
            leaf=f"{goal} 1단계",
            item_title=f"{goal} 1회차",
            first_step="포기하지 마세요, 단어 5개만 외워요",
        ),
    )

    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        prompt_id="planning/goal_decompose@v3",
        fallback=lambda: _decomposition("x", "x", "x", "x"),
        variables=_vars("planning/goal_decompose@v3", goal_title=goal),
        timeout=1.0,
    )

    assert result.fell_back is False
    value = result.value
    assert value.goal_nodes[0].title == goal
    assert value.goal_nodes[1].title == f"{goal} 1단계"
    assert value.action_items[0].title == f"{goal} 1회차"
    assert value.action_items[0].first_step == "잠깐 쉬어가는하지 마세요, 단어 5개만 외워요"
    assert result.banned_hits == ("포기",)


async def test_rule_fallback_keeps_the_users_goal_title() -> None:
    """LLM 이 죽어 룰 폴백('{제목} 1회차')이 나가도 사용자 목표 제목은 깨지지 않는다."""

    class _Card(BaseModel):
        title: str
        hint: str

    goal = "포기하지 않고 영어 회화 끝내기"
    result = await aiClient.run(
        module="inbox",
        schema=_Card,
        prompt_id="inbox/classify",
        fallback=lambda: _Card(title=f"{goal} 1회차", hint="실패해도 괜찮아요"),
        variables=_vars("inbox/classify", raw_text=goal),
        timeout=0.01,  # provider 없음 → 폴백
    )

    assert result.fell_back is True
    assert result.value.title == f"{goal} 1회차"
    # 폴백 템플릿이 스스로 쓴 금지어는 전과 같이 치환된다.
    assert result.value.hint == "한 번 멈춤해도 괜찮아요"


# ── llm-1 · planB-2: 톤 게이트가 사용자 목표 제목 때문에 AI 계획을 버리지 않는다 ──


@pytest.mark.parametrize(
    "goal",
    [
        "UX 디자이너가 되기 위한 포트폴리오 3개 완성",  # '너가'
        "자격증 네가지 따기",  # '네가'
        "똑똑하게 돈 관리하기",  # '똑똑하'
        "영어 능력이 있는 사람 되기",  # '능력이 있'
    ],
)
async def test_decomposition_echoing_the_goal_title_is_not_tone_gated(
    monkeypatch: pytest.MonkeyPatch, goal: str
) -> None:
    """미러 실측: 이 제목들이면 분해가 매번 tone_gate 로 버려지고 'N회차' 자리표시자만 나왔다."""
    _fake_provider(
        monkeypatch,
        _decomposition(
            root=goal,
            leaf=f"{goal} 1주차",
            item_title=f"{goal} 1주차 준비",
            first_step="책상에 앉아 오늘 할 일 한 줄 적기",
        ),
    )

    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        prompt_id="planning/goal_decompose@v3",
        fallback=lambda: _decomposition("rule", "rule", "rule", "rule"),
        variables=_vars("planning/goal_decompose@v3", goal_title=goal),
        timeout=1.0,
    )

    assert result.fell_back is False, result.reason
    assert result.value.goal_nodes[0].title == goal


async def test_ai_authored_tone_marker_is_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """사용자 제목이 '똑똑하게 돈 관리하기' 여도 AI 가 쓴 '똑똑하게 복습해요' 는 전과 같이 걸린다."""
    goal = "똑똑하게 돈 관리하기"
    _fake_provider(
        monkeypatch,
        _decomposition(
            root=goal,
            leaf=f"{goal} 1주차",
            item_title="가계부 정리",
            first_step="똑똑하게 복습해요",
        ),
    )

    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        prompt_id="planning/goal_decompose@v3",
        fallback=lambda: _decomposition("rule", "rule", "rule", "rule"),
        variables=_vars("planning/goal_decompose@v3", goal_title=goal),
        timeout=1.0,
    )

    assert result.fell_back is True
    assert result.reason == "tone_gate"
    assert result.banned_hits == ("똑똑하",)


async def test_goal_title_without_matching_variable_is_still_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """변수에 없는 문구는 사용자 원문이 아니다 — 기존 동작 그대로 reject."""
    _fake_provider(
        monkeypatch,
        _decomposition(
            root="UX 디자이너가 되기 위한 포트폴리오 3개 완성",
            leaf="1주차",
            item_title="포트폴리오 주제 고르기",
            first_step="책상에 앉기",
        ),
    )

    result = await aiClient.run(
        module="planning",
        schema=GoalDecomposition,
        prompt_id="planning/goal_decompose@v3",
        fallback=lambda: _decomposition("rule", "rule", "rule", "rule"),
        variables=_vars("planning/goal_decompose@v3", goal_title="토익 900"),
        timeout=1.0,
    )

    assert result.fell_back is True
    assert result.reason == "tone_gate"
