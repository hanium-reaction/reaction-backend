"""LLM 시스템 프롬프트 합성 — 톤 모드 prefix (Issue #23, S23).

DevBaseline §부록 D Q8 잠금: MVP 에선 톤 차이(gentle/strict/encouraging)를
시스템 프롬프트 **prefix 1줄**로만 분기한다. 톤이 없으면(None) prefix 없음 —
기존 LLM 동작을 그대로 유지한다.

톤 정책 (AGENTS.md §1): "Be on your side, not on your case" — 어떤 톤이어도
비난·압박·죄책감 유발 금지. 후처리 금지어 필터(`safety.banned_words`)와 함께 동작.

본 모듈은 **순수 함수**다 (프레임워크/세션 의존 없음 → 단위 테스트 용이).

배선 상태:
- #23-A: 본 헬퍼(prefix 카피)를 잠그고 테스트로 보호.
- #23-C: `aiClient.run(tone_mode=...)` 옵션 kwarg 추가로 **tool_executor 가 렌더 직후 prefix 적용**
  (ADR-0003 addendum, `docs/decisions/0003-llm-tool-executor.md`). 배선 = inbox·recovery
  라우트(`user.tone_mode`) + morning_brief cron.
- #23-D: interview·first_plan(LangGraph) 노드도 `config["configurable"]["tone_mode"]` 채널로
  전달(state 불변). 모든 LLM 호출에 톤 적용 완료.
"""

from __future__ import annotations

# 톤 모드 → 시스템 프롬프트 prefix 1줄. User.TONE_MODE_VALUES(gentle/strict/encouraging)
# 와 키가 일치한다. 카피 변경은 본 dict 한 곳만 수정.
TONE_SYSTEM_PREFIXES: dict[str, str] = {
    "gentle": "사용자를 다그치지 말고, 따뜻하고 부드러운 말투로 응답하세요.",
    "strict": "군더더기 없이 명확하고 단호하게, 다만 비난 없이 응답하세요.",
    "encouraging": "작은 진전도 구체적으로 짚어 격려하는 말투로 응답하세요.",
}


def tone_system_prefix(tone_mode: str | None) -> str:
    """톤 모드 → 시스템 프롬프트 prefix 1줄.

    톤이 없거나(None/빈 문자열) 미지원 값이면 빈 문자열을 돌려준다 (= prefix 없음).
    """
    if not tone_mode:
        return ""
    return TONE_SYSTEM_PREFIXES.get(tone_mode, "")


def compose_system_prompt(prompt_text: str, tone_mode: str | None) -> str:
    """렌더된 프롬프트 앞에 톤 prefix 를 붙인다.

    prefix 가 없으면(톤 없음/미지원) 원문을 그대로 반환 — 멱등하게 호출 가능.
    """
    prefix = tone_system_prefix(tone_mode)
    if not prefix:
        return prompt_text
    return f"{prefix}\n\n{prompt_text}"
