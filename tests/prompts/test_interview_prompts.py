"""Interview 프롬프트 렌더 회귀 (AGENTS.md §6 — prompt 변경은 tests/prompts/ 로 보호).

누락 변수는 `PromptRenderError` → tool_executor 가 조용히 룰 fallback 으로 빠진다
(사용자에겐 정상처럼 보임). 그 은폐를 막기 위해:
1. 각 프롬프트의 `{{var}}` 집합이 **코드가 실제로 넘기는 변수 집합과 정확히 일치**하는지
   (템플릿에 코드가 안 주는 변수가 생기면 = 런타임 fallback → 여기서 잡는다).
2. 그 변수 집합으로 렌더하면 예외 없이 모든 치환이 끝나는지.
3. 변수를 빼먹으면 실제로 `PromptRenderError` 가 나는지 (안전망 자체가 살아있는지).

`CODE_VARS` 는 `orchestrator/interview.py` 의 `ask_question`/`validate_answer`/
`summarize_interview` 가 넘기는 variables 와 동기화한다 (바뀌면 여기도 갱신).

⚠️ `interview/summary` 만은 **하드코딩하지 않고 코드에서 뽑는다**(`_summary_variables`).
나머지는 호출부에 변수가 인라인이라 목록으로 둘 수밖에 없지만, 요약은 빌더 함수가 단일
진실 소스라 뽑아 쓸 수 있다 — 하드코딩하면 빌더에 키를 더할 때 이 테스트가 따라오지 않아
계약이 조용히 표류한다(`plan_quality` 가 정확히 그렇게 표류했다).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import reaction_backend
from reaction_backend.orchestrator import interview, ultimate_adapter
from reaction_backend.prompts import registry
from reaction_backend.prompts.registry import PromptRenderError

_PROMPTS_DIR = Path(reaction_backend.__file__).parent / "prompts" / "interview"

# 코드가 각 프롬프트에 실제로 넘기는 변수 집합.
CODE_VARS: dict[str, set[str]] = {
    "interview/next_question": {
        "goal_title",
        "answered_context",
        "ambiguous_slot",
        "slot_label",
        "answer_type",
        "options",
        "last_answer",
        "retry",
    },
    "interview/ambiguity_score": {"slot_key", "answer", "answer_type", "options", "today"},
    # 채점 + 수확을 한 번에 하는 프롬프트(#431). 채점 전용(`ambiguity_score`)의 변수에
    # `open_slots` 하나가 더 붙는다 — 수확할 게 있을 때만 이쪽으로 간다.
    "interview/answer_intake": {
        "slot_key",
        "answer",
        "answer_type",
        "options",
        "today",
        "open_slots",
    },
    # 아래 집합은 _summary_var_keys() 로 대체된다 (파일 하단에서 갱신) — 참고용 원본.
    "interview/summary": {
        "identity",
        "goals",
        "heaviest",
        "deadlines",
        "success_image",
        "time_window",
        "peak_window",
        "tone",
        "rest_ok",
        "downscope_unit",
    },
    # kind="ultimate" — goal_title(heaviest 개념)이 없는 대신 statement(궁극 목표 선언)를
    # 싣는다. ambiguity_score 는 plan 과 **변수 집합이 완전히 동일**(validate_answer 는
    # kind 로 갈리지 않는 공용 dict) — prompt_id 만 카탈로그로 갈린다.
    "interview/ultimate_next_question": {
        "ambiguous_slot",
        "slot_label",
        "answer_type",
        "options",
        "answered_context",
        "last_answer",
        "retry",
        "statement",
    },
    "interview/ultimate_ambiguity_score": {"slot_key", "answer", "answer_type", "options", "today"},
    # 아래 집합은 _ultimate_summary_var_keys() 로 대체된다 — 참고용 원본.
    "interview/ultimate_summary": {
        "statement",
        "measure",
        "horizon",
        "identity",
        "current_position",
        "constraints",
    },
}


def _summary_var_keys() -> set[str]:
    """요약 프롬프트에 실제로 넘어가는 키 — 빌더에서 직접 뽑는다(계약의 단일 진실 소스)."""
    from uuid import uuid4

    state = interview.initial_state(session_id=uuid4(), user_id=uuid4())
    return set(interview._summary_variables(state))


def _ultimate_summary_var_keys() -> set[str]:
    """`interview/ultimate_summary` 에 실제로 넘어가는 키 — `ultimate_adapter.summary_variables`

    가 단일 진실 소스(agents/ultimate_summary_agent.run 과 룰 폴백이 둘 다 이 함수를 쓴다).
    """
    return set(ultimate_adapter.summary_variables({}))


CODE_VARS["interview/summary"] = _summary_var_keys()
CODE_VARS["interview/ultimate_summary"] = _ultimate_summary_var_keys()

_FILES = {
    "interview/next_question": "next_question.v1.md",
    "interview/ambiguity_score": "ambiguity_score.v1.md",
    "interview/answer_intake": "answer_intake.v1.md",
    "interview/summary": "summary.v1.md",
    "interview/ultimate_next_question": "ultimate_next_question.v1.md",
    "interview/ultimate_ambiguity_score": "ultimate_ambiguity_score.v1.md",
    "interview/ultimate_summary": "ultimate_summary.v1.md",
}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _placeholders(prompt_id: str) -> set[str]:
    text = (_PROMPTS_DIR / _FILES[prompt_id]).read_text(encoding="utf-8")
    return set(_PLACEHOLDER_RE.findall(text))


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_placeholders_match_code_variables(prompt_id: str) -> None:
    """템플릿 {{var}} 집합 == 코드가 넘기는 변수 집합 (드리프트 = 런타임 fallback 방지)."""
    assert _placeholders(prompt_id) == CODE_VARS[prompt_id]


@pytest.mark.parametrize("prompt_id", list(CODE_VARS))
def test_renders_without_missing_variables(prompt_id: str) -> None:
    """코드 변수 집합으로 렌더하면 예외 없이 모든 {{}} 가 치환된다."""
    text, _tmpl = registry.render(prompt_id, dict.fromkeys(CODE_VARS[prompt_id], "x"))
    assert text.strip()
    assert "{{" not in text  # 남은 미치환 플레이스홀더 없음


def test_missing_variable_raises() -> None:
    """변수 누락 시 PromptRenderError — 안전망(그리고 이 회귀 테스트의 전제)이 살아있는지."""
    with pytest.raises(PromptRenderError):
        registry.render("interview/next_question", {})


def test_next_question_prompt_requires_naming_the_goal() -> None:
    """목표별 슬롯 질문이 '이 목표' 대신 실제 목표 이름을 쓰게 하는 규칙이 살아있는지 (#187).

    회귀 배경: 목표를 3개 말해도 계획은 heaviest 하나만 다루는데, 목표별 슬롯 6종은
    "이 목표는 한 번에 어느 정도…" 라고만 물어 **사용자가 무엇에 답하는지 알 수 없었다.**
    `goal_title` 변수는 예전부터 넘어갔지만 '이름을 넣어라' 는 지시가 없어 LLM 이 붙일
    때도 안 붙일 때도 있었다(확률적).
    """
    body = registry.get("interview/next_question").body
    assert "목표별 슬롯이면 어느 목표를 묻는지 질문에 드러내라" in body
    assert "지시어 대신 실제" in body
    # 과교정 방지 — 첫 시도에서 실측으로 회귀가 잡혔다(실 LLM 3회: 과교정 0건 → 8건).
    # recovery.*/time.* 는 **전역 설정**인데 목표 이름이 붙어 "그 목표 전용" 으로 읽혔다.
    # 기계적으로 판정 가능한 규칙 + 실제로 틀렸던 슬롯의 반례를 함께 박아 둔다.
    assert "슬롯키가 `goals.` 로 시작하지 않으면 목표 이름을 문장에 절대 넣지 마라" in body
    assert "모든 목표에 공통으로 적용되는 전역 설정" in body
    assert "회복 톤은 전역이다" in body
    assert "활동 시간대는 전역이다" in body
    # goals.heaviest 는 '보기 중 하나를 지목하지 말라'는 기존 규칙과 충돌하므로 반드시 제외.
    assert "`goals.list` · `goals.heaviest` 가 **아니면**" in body
    assert "이 중에서 지금 가장 무겁게 느껴지는 건 어떤 거예요?" in body  # 기존 규칙 생존


def test_approach_suggestions_ask_for_a_concrete_target() -> None:
    """`goals.approach` 추천 답변이 **방식 선호**가 아니라 **따라갈 대상**을 묻게 한다.

    회귀 배경(실측, 통제 실험): 같은 인터뷰에서 `approachNote` 하나만 바꿔 마일스톤을
    3회씩 뽑았다. "인프런 강의 커리큘럼대로" 는 **3/3** 모두 뼈대에 '강의 수강' 단계를
    만들었지만, "작은 거부터 직접 만들면서" 는 **미입력과 구별되지 않았다** — 둘 다
    `기초 → 학습 → 구현 → 배포`. 순서 취향은 LLM 이 이미 그렇게 짜므로 계획이 안 바뀐다.

    그런데 추천 답변이 정확히 그 안 먹는 쪽으로 유도하고 있었다(실측 3회 × 3개 = **9/9**
    가 "디자인부터 먼저", "작은 기능부터", "튜토리얼 따라하며" — 이름 있는 대상 0건).
    수정 후 10/10 이 대상형으로 바뀌었고 도메인도 따라간다(달리기 → "훈련 플랜",
    "러닝 코스").

    빠져나갈 길("정해둔 건 없어요")과 베끼기 금지도 함께 박는다 — 첫 시도에서 LLM 이
    예시를 **글자 그대로** 복사해 목표 영역과 무관한 카드를 내놨다(3회 중 2회).
    """
    body = registry.get("interview/next_question").body
    assert '`goals.approach` 는 "무엇을 따라갈지"를 묻는 자리다' in body
    assert "따라갈 대상이 있는지/무엇인지" in body
    # 왜 방식 선호가 안 되는지까지 남긴다 — 이유가 없으면 다음 사람이 되돌린다.
    assert "순서 취향은" in body and "계획이 달라지지 않는다" in body
    # 없는 걸 지어내게 만들면 안 된다.
    assert "정해둔 건 없어요" in body
    # 예시 베끼기 방지(실측 회귀).
    assert "그대로 베끼지 말고" in body
    assert "목표 영역에 맞는 말로 바꿔라" in body


def test_ambiguity_prompt_forbids_promoting_glosses_to_goals() -> None:
    """goals.list 정규화가 부연 설명을 별개 목표로 올리지 못하게 하는 규칙이 살아있는지 (#232).

    회귀(코너 배터리 실측, 실 LLM): "전공책 3권을 완독하고 싶어요. 각 권당 10챕터 정도예요."
    가 목표 2개로 쪼개져 '각 권당 10챕터 학습' 이라는 유령 목표가 생겼다. 규칙이 조용히
    빠지면 결정적 백스톱(`_prune_goal_glosses`)이 좁아서 코드는 초록인 채 회귀가 돌아온다.
    """
    body = registry.get("interview/ambiguity_score").body
    assert "goals.list 는 '하고 싶은 일' 만 센다" in body
    assert "별개 목표가 아니라 직전 목표의 속성" in body
    # 진짜 목표 여러 개는 그대로 나눠야 한다 — 규칙이 과교정으로 기울지 않게 하는 반례.
    assert "이건 진짜 목표 2개다" in body
    # 상태·완료형 답에서 null 로 빠지면 룰 폴백이 원문을 쉼표로 쪼개 조각이 목표가 된다.
    # (코너 재점검에서 실제로 겪은 회귀 — 이 문구가 그 구멍을 막는다.)
    assert "goals.list 에서 normalized_value 를 null 이나 빈 값으로 두지 마라" in body
    assert "대학원 합격" in body


def test_ultimate_next_question_does_not_reference_goal_title() -> None:
    """ultimate 프롬프트는 `{{goal_title}}`(heaviest 목표 개념) 을 참조하지 않는다.

    `_heaviest_goal_hint` 는 궁극목표 세션에서 항상 "당신의 목표" 라는 무의미한 문자열을
    낸다(goals.list/goals.heaviest 를 안 묻는 카탈로그라서) — 프롬프트가 그 값을 쓰면
    안 되고, 대신 `{{statement}}`(궁극 목표 선언)로 그라운딩해야 한다.
    """
    body = registry.get("interview/ultimate_next_question").body
    assert "{{goal_title}}" not in body
    assert "{{statement}}" in body


def test_ultimate_ambiguity_prompt_keeps_statement_singular() -> None:
    """`ultimate.statement` 는 목록이 아니라 단수 선언문이라 쉼표로 쪼개면 안 된다는 규칙 (#232 재발 방지).

    plan 의 goals.list 배열 분해 규칙을 그대로 옮기면 "메이저리그에서 뛰고, 세계 최고 투수가
    되고 싶어요" 가 목표 2개로 쪼개진다 — 이 프롬프트는 그 규칙 대신 반대 방향(쪼개지 말 것)을
    명시해야 한다.
    """
    body = registry.get("interview/ultimate_ambiguity_score").body
    assert "단수 선언문" in body
    assert "쉼표가 있어도 절대 배열로 쪼개지 마라" in body
    # constraints/pillars_hint 는 반대로 여러 항목을 허용해야 한다(§2.5 필수 슬롯 성격 차이).
    assert "여러 항목이 섞일 수 있다" in body


def test_ultimate_summary_prompt_uses_year_horizon_language() -> None:
    """궁극목표 요약이 '이번 학기/이번 주' 같은 짧은 지평 표현을 쓰지 말라는 규칙이 살아있는지.

    계획 인터뷰 요약(`interview/summary`)과 같은 톤 지시를 복붙하면 3~10년 지평의 궁극
    목표가 학기 단위로 읽힌다 — 이 인터뷰의 정체성과 직결되는 규칙이라 별도로 지킨다.
    """
    body = registry.get("interview/ultimate_summary").body
    assert "이번 주" in body or "이번 학기" in body  # 금지 대상 표현 자체가 문서에 명시돼야 함
    assert "짧은 지평의 표현을 쓰지 마라" in body
