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

'사용자 입력'은 호출자가 넘긴 프롬프트 변수 중 **이름을 명시적으로 적어둔 것만**이다
(`USER_AUTHORED_VARIABLES` / `UserText.from_variables`). 예전엔 변수 **전체**를 사용자
원문으로 봤는데 그건 틀렸다 — 변수에는 사용자가 쓰지 않은 텍스트도 실린다. 가장 위험한
건 `materials` 로, 사용자가 **붙여넣은 링크를 서버가 열어 가져온 제3자 웹페이지 본문**이
들어온다(`first_plan_adapter.materials_for_prompt(fetched=...)` ← `web_fetch.fetcher`).
`goal_decompose` 프롬프트는 그 본문의 실제 내용을 뼈대로 삼으라고 **지시**하므로 LLM 이
거기서 두 어절 이상을 그대로 옮겨 쓰는 건 설계된 동작이고, 그 자리를 면제하면 **남이 쓴
문장**이 금지어 치환과 톤 게이트를 통째로 빠져나간다(AGENTS §2 우회 금지). 공격자도 필요
없다 — 목차에 '실패 사례 분석' 이 있는 평범한 학습 자료 한 장이면 된다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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


# ── 어떤 프롬프트 변수를 '사용자 원문'으로 보는가 — 허용 목록 ────────────────────
# **여기 적힌 이름만** 보호한다. 목록에 없는 변수는 AI·서버가 만든 텍스트로 취급돼 금지어
# 치환·톤 게이트가 **전과 똑같이** 걸린다. 기본값이 '보호 안 함'이라(fail-tight) 나중에
# 새 변수가 생겨도 필터에 구멍이 나지 않는다 — 빠뜨렸을 때의 증상은 "사용자 문구가 고쳐짐"
# (고칠 수 있는 버그)이지 "남의 문장이 필터를 통과함"(잠금 결정 위반)이 아니다.
#
# 들어오는 값의 출처는 전부 확인한 것만 적는다:
#   goal_title/title/success_image/current_level/approach_note — 인터뷰에서 사용자가 쓴 목표 슬롯
#     (`first_plan_adapter.context_from_outcome`, `study_method_agent`)
#   answer/last_answer/raw_text/query/user_hint — 요청 본문에 사용자가 직접 친 글
#     (`interview`, `inbox.classify`, `materials.search`, `mandala` 링 재생성)
#   statement/measure/current_position/constraints/pillars_hint/locked_axes — 궁극목표 인터뷰
#     슬롯 (`mandala_adapter.context_from_ultimate`, "사용자가 인터뷰에서 직접 말한 축")
#   subgoal/sibling_titles/locked_cells — 사용자가 편집·잠근 만다라 축·칸 제목
#     (`mandala_cell_agent.run_branch`, "사용자가 이미 편집한 셀")
#
# 일부러 **뺀** 것:
#   materials — 서버가 링크를 열어 가져온 제3자 본문이 섞인다(모듈 docstring 참고). 붙여넣은
#     메모만 들어오는 경우까지 같이 빠지지만, 남의 문장을 면제하느니 사용자 메모가 예전처럼
#     치환되는 쪽이 낫다.
#   identity — 사용자 답이 아니라 서버가 **조립한 문장**(`_identity_line`).
#   요약·JSON·숫자·라벨 전부 — behavioral_summary, failure_summary, review_feedback,
#     goal_nodes_json, milestones, horizon, total_minutes … 사용자가 쓴 글이 아니다.
USER_AUTHORED_VARIABLES: frozenset[str] = frozenset(
    {
        # 목표 슬롯 (first plan / study method)
        "goal_title",
        "title",
        "success_image",
        "current_level",
        "approach_note",
        # 사용자가 방금 친 글
        "answer",
        "last_answer",
        "raw_text",
        "query",
        "user_hint",
        # 궁극목표 인터뷰 슬롯 (mandala)
        "statement",
        "measure",
        "current_position",
        "constraints",
        "pillars_hint",
        "locked_axes",
        # 사용자가 편집·잠근 만다라 제목
        "subgoal",
        "sibling_titles",
        "locked_cells",
    }
)


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
    def from_variables(cls, variables: Mapping[str, object] | None) -> UserText:
        """프롬프트 변수 중 **사용자가 직접 쓴 값만** 모은다(`USER_AUTHORED_VARIABLES`).

        `tool_executor.run` 이 쓰는 생성자다. 목록에 없는 이름은 조용히 버린다 — 보호하지
        않는다는 뜻이고, 그게 안전한 기본값이다.
        """
        if not variables:
            return cls.of(())
        return cls.of(v for k, v in variables.items() if k in USER_AUTHORED_VARIABLES)

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
