"""사용자 원문 에코 판정 — 금지어 필터·톤 게이트가 **사용자가 쓴 말**을 고치지 않게 (llm-1·llm-2).

왜 필요한가
-----------
금지어 필터(`banned_words`)와 톤 게이트(`tone_gate`)는 LLM 출력 **전체 문자열**에 걸린다.
그런데 LLM 은 사용자 목표 제목·답을 출력에 그대로 옮겨 쓴다(목표 트리 root, 인터뷰
`normalized_value`, "{제목} N회차" 룰 폴백). 그 결과:

- '포기하지 않는 창업가가 되기' → '잠깐 쉬어가는하지 않는 창업가가 되기' 로 **사용자 답이
  바뀌어 저장**됐다(미러 실측, llm-2).
- 'UX 디자이너가 되기' 의 '디자이**너가**' 가 사람 귀인 마커로 걸려 AI 계획이 **매번**
  버려졌다(미러 실측, llm-1). 트리거가 사용자 자신의 목표 제목이라 다시 만들어도 같다.

계약은 이미 정해져 있다 — "사용자 문구는 금지어 필터를 거치지 않는다(톤 잠금은 AI 출력
대상)" (api-contract §7 edited 결정). 이 모듈은 그 계약을 **LLM 이 사용자 문구를 옮겨 쓴
경우**까지 넓힌다.

무엇을 '사용자 원문'으로 보나 — 보수적으로
------------------------------------------
필터를 끄는 게 아니다(AGENTS §2). **AI 가 스스로 쓴 말은 전과 똑같이** 걸려야 하므로,
"사용자 입력과 우연히 같은 어절 하나" 로는 면제하지 않는다. 예: 사용자가 '포기하지 않는
창업가' 라고 썼어도 AI 의 '포기하지 마세요' 는 여전히 치환된다(공유 구간 '포기하지' 한
어절뿐). 면제는 둘 중 하나일 때만이다.

1. 매칭을 품은 어절을 포함해 **연속 두 어절 이상**이 사용자 입력에 그대로 있고, 그
   구간이 금지어·마커 외에 글자를 `_MIN_CONTEXT_CHARS` 자 이상 더 품는다
   ('포기하지 않는 창업가' ⊂ 사용자 답).
2. 매칭을 품은 연속 어절이 **사용자 입력 하나 전체**와 같다(한 어절짜리 축 제목
   '실패노트' 를 그대로 옮긴 경우). 단 금지어만 달랑 있는 입력('실패')은 제외 —
   그건 AI 문장에서 흔히 우연히 겹친다.

'사용자 입력'은 호출자가 넘긴 프롬프트 변수 값이다(`tool_executor.run`). 변수에는
사용자 답·제목 말고도 이전 AI 출력(분해 결과 JSON, 리뷰 피드백)이 실리지만, 그 문장들은
이미 이 필터를 **거쳐 저장된** 것이라 새로 빠져나갈 금지어가 없다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# 어절 경계 — 공백과 문장부호. 따옴표로 감싼 인용("'포기하지 않는 창업가'라는 목표")도
# 어절로 보게 문장부호를 경계에 넣는다.
_TOKEN_RE = re.compile(r"[^\s\"'“”‘’`()\[\]{}<>「」『』《》〈〉【】,.!?…·~:;/|]+")
_EDGE_CHARS = " \t\r\n\"'“”‘’`()[]{}<>「」『』《》〈〉【】,.!?…·~:;/|"

# 규칙 1 — 금지어·마커 밖으로 더 겹쳐야 하는 글자 수(공백 제외). '포기할 수'(+2) 같은
# 흔한 조합은 우연히 겹치므로 면제하지 않고, '포기하지 않는'(+4)부터 사용자 문구로 본다.
_MIN_CONTEXT_CHARS = 4
# 규칙 2 — 입력 전체를 옮겨 쓴 경우의 하한. '실패'(+0) 는 제외, '실패노트'(+2) 는 면제.
_MIN_WHOLE_EXTRA_CHARS = 2


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _visible_len(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


@dataclass(frozen=True, slots=True)
class UserText:
    """프롬프트 변수에서 모은 사용자 입력 — `covers()` 로 매칭 구간이 사용자 원문인지 본다."""

    texts: tuple[str, ...]
    """공백 정규화된 입력들(빈 값 제외)."""
    wholes: tuple[str, ...]
    """`texts` 각각에서 앞뒤 문장부호까지 걷어낸 모양 — 규칙 2 비교용(같은 순서)."""

    @classmethod
    def of(cls, texts: Iterable[object]) -> UserText:
        normalized = [_normalize(str(t)) for t in texts if t is not None]
        kept = tuple(dict.fromkeys(t for t in normalized if t))
        return cls(texts=kept, wholes=tuple(t.strip(_EDGE_CHARS) for t in kept))

    def __bool__(self) -> bool:
        return bool(self.texts)

    def covers(self, text: str, start: int, end: int) -> bool:
        """`text[start:end]`(금지어·마커 매칭)가 사용자 원문을 옮겨 쓴 자리인가."""
        if not self.texts:
            return False
        tokens = [(m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]
        first = next((i for i, (_, te) in enumerate(tokens) if te > start), None)
        last = next((i for i in range(len(tokens) - 1, -1, -1) if tokens[i][0] < end), None)
        if first is None or last is None or first > last:
            return False
        word_len = _visible_len(text[start:end])

        def window(a: int, b: int) -> str:
            return _normalize(text[tokens[a][0] : tokens[b][1]])

        for source, whole in zip(self.texts, self.wholes, strict=True):
            if window(first, last) not in source:
                continue
            for order in ((-1, 1), (1, -1)):
                a, b = first, last
                for step in order:
                    while True:
                        na, nb = (a - 1, b) if step < 0 else (a, b + 1)
                        if na < 0 or nb >= len(tokens) or window(na, nb) not in source:
                            break
                        a, b = na, nb
                run = window(a, b)
                extra = _visible_len(run) - word_len
                if b > a and extra >= _MIN_CONTEXT_CHARS:
                    return True
                if run.strip(_EDGE_CHARS) == whole and extra >= _MIN_WHOLE_EXTRA_CHARS:
                    return True
        return False
