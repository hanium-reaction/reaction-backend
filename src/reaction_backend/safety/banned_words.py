"""금지어 후처리 필터 (DevBaseline §4.2 — 잠금).

- "Be on your side, not on your case" 톤을 강제하기 위한 **단일 사전**.
- LLM Tool Executor 의 **마지막 단계** 에서 `enforce()` 가 모든 사용자
  노출 문자열을 통과시킨다 — 우회 금지 (AGENTS.md §2).
- 발견 시 PRD §4.3 권장 표현으로 치환. 치환 후에도 남아있으면 fallback 신호.

사전:
    {금지어: 권장 치환어}
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# DevBaseline §4.2 / PRD §4.3 — 잠금 사전. 수정은 PR + 사람 합의 필요.
BANNED_REPLACEMENTS: Mapping[str, str] = {
    "실패율": "회복률",
    "실패": "한 번 멈춤",
    "또 못": "이번엔 어렵네요",
    "안 됐": "다음에 다시 해봐요",
    "못했": "아직 못한",
    "왜 안": "어떻게 하면",
    "다시 실수": "다시 시도",
    # 추가 톤 가드 — "Be on your side" 보강
    "게으르": "지금 힘든",
    "한심": "충분히 노력 중",
    "한심해": "충분히 노력 중이에요",
    "패배": "잠깐 멈춤",
    "포기": "잠깐 쉬어가는",
}

# 치환 후에도 잡히면 안 되는 strict 차단어 (치환 사전에 없거나 치환 후에도 남는 경우).
# 빈 사전이지만 향후 보강용으로 노출.
HARD_BLOCK_TERMS: frozenset[str] = frozenset()

# 컴파일된 우선순위 패턴 — 긴 키부터 매칭해야 부분문자열 충돌을 피한다 ("실패율" > "실패").
_PATTERN = re.compile(
    "|".join(re.escape(k) for k in sorted(BANNED_REPLACEMENTS, key=len, reverse=True))
)


@dataclass(frozen=True, slots=True)
class FilterResult:
    """`enforce()` 의 결과."""

    text: str
    """치환이 끝난 최종 문자열."""
    changed: bool
    """치환이 1회 이상 발생했는지."""
    hits: tuple[str, ...]
    """매칭된 금지어 목록 (중복 제거, 입력 순서)."""
    blocked: bool
    """`HARD_BLOCK_TERMS` 가 잔존하거나 치환 실패 — Tool Executor 가 fallback 분기."""


def scan(text: str) -> tuple[str, ...]:
    """치환 없이 매칭만 — 회귀 테스트/로그 용."""
    seen: list[str] = []
    for match in _PATTERN.finditer(text):
        token = match.group(0)
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def enforce(text: str) -> FilterResult:
    """금지어를 권장 표현으로 치환. Tool Executor 마지막 단계.

    - 사용자 노출 문자열에 **무조건** 통과시켜야 함.
    - `blocked=True` 면 호출자가 fallback 메시지로 대체.
    """
    hits = scan(text)
    new_text = _PATTERN.sub(lambda m: BANNED_REPLACEMENTS[m.group(0)], text)

    leftover = any(term in new_text for term in HARD_BLOCK_TERMS)
    # 1회 치환 후 다시 매칭되면 (드물지만 치환 결과가 금지어를 만든 경우) 차단.
    re_match_after = _PATTERN.search(new_text) is not None

    return FilterResult(
        text=new_text,
        changed=bool(hits),
        hits=hits,
        blocked=leftover or re_match_after,
    )


def enforce_many(texts: Iterable[str]) -> list[FilterResult]:
    """여러 문자열을 일괄 통과 — 응답에 여러 필드가 있을 때."""
    return [enforce(t) for t in texts]


def enforce_structured(payload: Any) -> tuple[Any, bool, tuple[str, ...]]:
    """dict/list/scalar 트리를 재귀적으로 치환.

    - 문자열만 통과시키고 다른 타입은 그대로 둔다.
    - 반환: (치환된 트리, blocked 플래그, 누적 hits)
    """
    hits: list[str] = []
    blocked = False

    def _walk(node: Any) -> Any:
        nonlocal blocked
        if isinstance(node, str):
            r = enforce(node)
            for h in r.hits:
                if h not in hits:
                    hits.append(h)
            if r.blocked:
                blocked = True
            return r.text
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, tuple):
            return tuple(_walk(v) for v in node)
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        return node

    return _walk(payload), blocked, tuple(hits)
