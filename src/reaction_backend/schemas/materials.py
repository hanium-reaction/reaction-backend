"""자료 검색 3단계 HITL 흐름 스키마 (#259 §4.1 ①②③ 결정).

세 단계로 나눈 이유는 UI 편의가 아니라 **결정 사항 세 개가 각각 다른 단계에 걸려 있기**
때문이다:

  ① 프라이버시 — 목표 텍스트가 Google 검색으로 나간다. 그래서 **무엇이 나가는지 사용자가
     보고 고칠 수 있어야** 한다 → 1단계(제안)와 2단계(실행)가 갈라져 있고, 1단계는
     **아무것도 외부로 보내지 않는다**.
  ② HITL — 가져온 자료가 계획을 바꾸기 전에 "이 자료 맞아요" 를 받는다 → 3단계(확정)가
     따로 있고, 2단계 응답은 `is_draft=True` 로 아무 데도 저장되지 않는다.
  ③ 트리거 — 자동이 아니라 **사용자가 버튼을 누를 때**만 돈다 → 세 단계 모두 POST 다.

3단계가 자료를 `goals.materials` 슬롯에 쓰면, 그 뒤는 **붙여넣기와 완전히 같은 경로**로
흐른다(`build_outcome` → `materials_for_prompt` → 분해). 검색 전용 배선이 따로 없다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel, DraftMixin

# 사용자가 편집한 검색어의 상한 — 질의 하나로는 충분히 길고, 목표 원문을 통째로 붙여
# 넣는 것(=① 프라이버시 결정을 우회하는 형태)은 막는다.
MAX_QUERY_CHARS = 200
# 확정 자료 원문 상한. 프롬프트에는 앞 2,000자만 실리지만(`_MATERIALS_MAX_CHARS`),
# 사용자가 편집해 보낼 여지를 두고 넉넉히 받는다.
MAX_MATERIAL_CHARS = 20_000


# ─────────────────────────────────────────────────────────────────────────────
# 1단계 — 검색어 제안 (외부 호출 0회 · LLM 0회)
# ─────────────────────────────────────────────────────────────────────────────


class MaterialsQueryRequest(CamelModel):
    """POST /plans/materials/search-query."""

    interview_session_id: str | None = None
    """미지정이면 가장 최근 '정상 종료' 인터뷰에서 가장 무거운 목표를 쓴다."""


class MaterialsQueryResponse(CamelModel):
    """제안된 검색어 — **아직 아무것도 검색하지 않았다.**

    `is_draft` 를 쓰지 않는 이유: 이건 AI 산출물이 아니라 목표 제목에서 규칙으로 만든
    문자열이다. LLM 도 외부 호출도 없으므로 Draft 표기 대상이 아니다.
    """

    suggested_query: str
    goal_title: str
    notice: str
    """이 검색어가 외부로 나간다는 사실을 사용자에게 알리는 문구 (① 결정)."""


# ─────────────────────────────────────────────────────────────────────────────
# 2단계 — 검색 실행 (그라운딩 1건 과금)
# ─────────────────────────────────────────────────────────────────────────────

MaterialsSearchStatus = Literal[
    "found",
    "not_found",
    "blocked_copyright",
    "quota_exceeded",
    "unavailable",
]
"""왜 상태를 다섯으로 가르는가 — **사용자가 다음에 할 행동이 각각 다르기 때문**이다.

`blocked_copyright` 는 재시도해도 영영 안 된다(provider 가 막는다). 이걸 `unavailable`
과 묶으면 사용자는 되지 않을 버튼을 계속 누른다. `quota_exceeded` 는 내일이면 되고,
`not_found` 는 검색어를 고치면 될 수 있다.
"""


class MaterialsSearchRequest(CamelModel):
    """POST /plans/materials/search — **사용자가 확인·편집한 검색어만** 받는다 (① 결정).

    목표 슬롯을 서버가 알아서 질의로 만들지 않는다. 무엇이 외부로 나가는지가 이 필드
    하나로 결정되고, 사용자가 1단계에서 그걸 봤다.
    """

    query: str = Field(min_length=2, max_length=MAX_QUERY_CHARS)


class MaterialSource(CamelModel):
    """검색이 실제로 참조한 출처 — 사용자에게 그대로 보여준다 (#259 ⑩).

    출처를 숨기고 자료를 쓰면 다른 판·다른 강의를 가져왔을 때 사용자가 알아챌 방법이 없다.
    """

    title: str
    uri: str


class MaterialsSearchResponse(DraftMixin):
    """검색 결과 — **저장되지 않았다.** 3단계 확정 전까지는 아무 데도 안 남는다.

    `is_draft=True` 가 그 계약이다 (AGENTS §1.4).
    """

    status: MaterialsSearchStatus
    text: str | None = None
    """`status="found"` 일 때만 채워진다. 출처가 0건이면 버려진다(#259 §2)."""
    sources: list[MaterialSource] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    """모델이 실제로 던진 검색어 — 사용자가 "뭘 보고 온 건지" 확인할 수 있게."""
    notice: str
    remaining_today: int | None = None
    """오늘 남은 검색 횟수. 무제한 설정이면 None."""


# ─────────────────────────────────────────────────────────────────────────────
# 3단계 — 사용자 확정 (HITL 게이트 · ② 결정)
# ─────────────────────────────────────────────────────────────────────────────


class MaterialsConfirmRequest(CamelModel):
    """POST /plans/materials/confirm — "이 자료 맞아요".

    `text` 를 그대로 받는 이유: 사용자가 **고쳐서** 보낼 수 있어야 한다. 검색이 다른 판을
    가져왔거나 일부만 맞을 때 지우고 붙이는 게 HITL 의 핵심이다.
    """

    text: str = Field(min_length=1, max_length=MAX_MATERIAL_CHARS)
    interview_session_id: str | None = None


class MaterialsConfirmResponse(CamelModel):
    """확정 결과 — 명시 승인이므로 Draft 가 아니다 (ADR-0005 §7.2)."""

    goal_title: str
    saved_chars: int
    notice: str
