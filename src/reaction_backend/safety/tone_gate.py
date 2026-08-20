"""톤·구조 게이트 (근거 대장 §4 S6) — `banned_words` **뒤에** 붙는 결정적 검증기.

`banned_words.py` 는 명사 1:1 치환("실패"→"한 번 멈춤")이라, **문장 구조** 문제는 못
잡는다 — 안전한 대체 표현이 없는 두 가지를 여기서 대신 본다:

1. **사람 귀인** ("당신이 안 해서" 류) — 근거 A2: 원인 귀인의 주어가 상황이 아니라
   **사람**(2인칭)이면 shame 축을 건드린다. 회복 카드는 애초에 "~해볼까요" 청유형이라
   2인칭 대명사가 문법적으로 필요 없다 — 그래서 이 마커가 나타나면 대부분 실제로
   비난 문장이다(짧은 코칭 카드라는 도메인 특성에 기댄 휴리스틱, 자유 대화엔 안 맞음).
2. **자존감 부양(자아 수준 칭찬)** ("역시 잘하시네요" 류) — 근거 A1(자존감 조건이
   자기자비보다 약함)·E2(자아 수준 피드백이 1/3 에서 성과를 낮춤). `rubric-v1.md` 축④
   5점 상한 조건과 같은 어휘를 쓴다 — 두 곳이 갈라지면 한쪽만 고쳐지므로 여기 것이
   원본이고 루브릭 문서가 이걸 인용한다.

`banned_words.enforce()` 와 달리 **치환하지 않고 reject 만 한다** — "당신이 게을러서"
에서 "당신이"만 지워도 문장이 안 살아난다. 안전하게 고칠 수 없는 문제는 통과시키지
않는 게 낫다(Tool Executor 가 fallback 으로 분기).
"""

from __future__ import annotations

from typing import Any

# 근거 A2 — 회복 카드 문구(if_clause/then_clause/rationale 등)는 전부 "~해볼까요" 청유형
# 이라 2인칭 대명사가 문법적으로 불필요하다. 등장하면 대부분 "당신이 ~해서" 류의 비난이다.
PERSON_ATTRIBUTION_MARKERS: tuple[str, ...] = ("당신이", "네가", "너가")

# 근거 A1/E2, rubric-v1.md 축④ 5점 상한 조건과 동일 어휘 — 두 곳이 갈라지지 않게 유지할 것.
SELF_ESTEEM_BOOST_MARKERS: tuple[str, ...] = (
    "역시 잘하",
    "역시 잘 하",
    "똑똑하",
    "능력있",
    "능력이 있",
    "원래 잘하",
    "잘하시네요",
)

_ALL_MARKERS: tuple[str, ...] = PERSON_ATTRIBUTION_MARKERS + SELF_ESTEEM_BOOST_MARKERS


def scan(text: str) -> tuple[str, ...]:
    """치환 없이 매칭만 — `banned_words.scan` 과 같은 계약(순서 보존, 중복 제거)."""
    seen: list[str] = []
    for marker in _ALL_MARKERS:
        if marker in text and marker not in seen:
            seen.append(marker)
    return tuple(seen)


def check_structured(payload: Any) -> tuple[bool, tuple[str, ...]]:
    """dict/list/scalar 트리를 재귀적으로 스캔. `banned_words.enforce_structured` 와
    달리 치환된 트리는 반환하지 않는다 — 애초에 안전하게 고칠 방법이 없어서 reject 만
    한다.

    반환: (blocked, 누적 hits — 중복 제거, 최초 발견 순서).
    """
    hits: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            for h in scan(node):
                if h not in hits:
                    hits.append(h)
        elif isinstance(node, list | tuple):
            for v in node:
                _walk(v)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)

    _walk(payload)
    return bool(hits), tuple(hits)
