"""자료 상세 확정 — 목차(best-effort)/분량 (ADR-0010 §1 ③).

후보 목록(`materials_catalog`)에서 사용자가 고른 **한 건 또는 두 건(책+영상)**의 상세를
조회하고, 확인 후 `goals.materials` 슬롯에 저장한다. LLM 을 부르지 않는다 — API 호출만이다.
몇 건이 적절한지는 Method Agent 의 `materialMix`(`schemas/study_method.py`)가 권장하지만
강제하지 않는다.

**계획 생성에 반영된다(ADR-0010 §5).** `interview_adapter._materials_note` 가
`{"type": "spec", ...}` 를 텍스트로 풀어 `materials_note` 에 싣고, 그 뒤로는
`first_plan_adapter.materials_for_prompt`/`goal_decompose` 가 붙여넣기 텍스트와
**완전히 같은 경로**로 처리한다 — 새 프롬프트 변수도 새 방어 로직도 만들지 않았다.
영상 커리큘럼처럼 항목마다 분량(분)이 붙어 있으면, `goal_decompose` 의 기존 규칙
("참고 자료 원문에 목차·주차·챕터가 있으면 그대로 뼈대로 삼아라")이 세션 길이까지
정확하게 잡아준다(L0 핵심 발견).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from reaction_backend.schemas.common import CamelModel

MAX_TOC_ENTRIES = 100
MAX_CURRICULUM_ITEMS = 200


# ─────────────────────────────────────────────────────────────────────────────
# 도서
# ─────────────────────────────────────────────────────────────────────────────


class BookDetailRequest(CamelModel):
    """POST /plans/materials/book-detail — 후보 목록의 `isbn13` 을 그대로 넘긴다."""

    isbn13: str = Field(min_length=1, max_length=20)


class BookChapter(CamelModel):
    """목차 항목 1개(best-effort, L0 1/10). `endPage` 는 이 챕터가 끝나는 페이지(추정) —
    소단원 점선 뒤 페이지 번호 중 챕터 경계마다 마지막 것만 취한다. 단조증가가 깨지면
    파싱이 틀렸다는 뜻이라 **전부 `None`** 이 된다(`nl_seoji.client._chapter_end_pages`
    참고) — 챕터 제목 목록 자체는 그래도 남는다."""

    title: str
    end_page: int | None = None


class BookSpecDetail(CamelModel):
    """도서 상세 — 페이지 수는 안정적으로 온다(L0 10/10). 목차·페이지 체크포인트는
    best-effort(L0 1/10)."""

    kind: Literal["book"] = "book"
    title: str
    author: str
    isbn13: str
    page_count: int
    """0 이면 페이지 수를 못 받았다는 뜻(진도 계산에 못 쓴다)."""
    chapters: list[BookChapter] = Field(default_factory=list, max_length=MAX_TOC_ENTRIES)
    """비어 있으면 이 판본은 목차가 없다(10권 중 9권이 정상)."""
    toc_source: Literal["seoji"] | None = None
    """`None` 이면 `chapters` 도 비어 있다 — 실패가 아니라 이 소스의 기본 동작."""


class ChapterPace(CamelModel):
    """챕터 1개의 세션 배정 — `sessions` 개로 나누면 이 챕터가 정확히 세션 경계에서 끝난다
    (세션이 챕터 중간에서 끊기지 않는다). 목차 커버리지 밖의 나머지 분량(`endPage` 가
    `pageCount` 에 못 미치는 차이)도 이름 없는 항목 하나로 여기 포함된다."""

    title: str
    end_page: int | None = None
    sessions: int


class BookPace(CamelModel):
    """책 전체를 마감까지 다 보려면 세션당 몇 쪽 — `page_count` 만으로 항상 계산 가능하다
    (L0 10/10, 목차 유무와 무관). 목표의 시간 예산(주당 시간·마감)과 결합한 **파생값**이라
    `spec-confirm` 이 그때그때 서버에서 계산한다 — 클라이언트가 보낸 값을 믿지 않는다.

    `chapters` 가 채워지면(목차 전 챕터에 `endPage` 가 있을 때만, best-effort) `totalSessions`
    는 그 챕터별 배정의 합 — 균등 분할이 아니라 **챕터 경계를 존중한** 세션 수다. 비어 있으면
    (목차가 없거나 일부 챕터만 페이지를 알 때) `pageCount` 균등 분할로 폴백한다."""

    pages_per_session: int
    """`chapters` 가 있어도 이 값은 `page_count / total_sessions` 의 **평균**이다 — 챕터마다
    실제 쪽수는 `chapters[].sessions` 를 봐야 한다."""
    total_sessions: int
    days_until_deadline: int
    summary: str
    """"세션당 약 21쪽씩 20번이면 마감(2026-12-01)까지 420쪽을 다 봐요." 류 — density
    프리셋(standard) 가정의 **추정치**임을 문구에 담는다."""
    chapters: list[ChapterPace] = Field(default_factory=list, max_length=MAX_TOC_ENTRIES + 1)


class BookDetailResponse(CamelModel):
    """조회 결과 — 저장하지 않는다. `detail` 이 있으면 `spec-confirm` 으로 넘길 수 있다."""

    detail: BookSpecDetail | None = None
    notice: str | None = None
    """`detail` 이 없을 때(또는 목차만 없을 때) 사유·안내."""


# ─────────────────────────────────────────────────────────────────────────────
# 영상
# ─────────────────────────────────────────────────────────────────────────────


class VideoDetailRequest(CamelModel):
    """POST /plans/materials/video-detail — 후보 목록의 `playlistId` 를 그대로 넘긴다."""

    playlist_id: str = Field(min_length=1, max_length=100)


class VideoSpecItem(CamelModel):
    """재생목록의 영상 1편 — 제목이 곧 단원명, `minutes` 가 곧 분량(L0 핵심 발견)."""

    title: str
    minutes: int


class VideoSpecDetail(CamelModel):
    """영상 상세 — 커리큘럼(영상 제목)과 분량(재생시간)이 함께 온다(L0 4/4)."""

    kind: Literal["video"] = "video"
    title: str
    channel_title: str
    playlist_id: str
    playlist_url: str
    video_count: int
    """재생목록의 **실제** 총 영상 수 — `curriculum` 이 상한에 잘려도 이 값은 정확하다."""
    total_minutes: int
    curriculum: list[VideoSpecItem] = Field(default_factory=list, max_length=MAX_CURRICULUM_ITEMS)
    truncated: bool = False
    """`MAX_CURRICULUM_ITEMS` 상한에 걸려 `curriculum` 이 일부만 담겼다는 뜻."""


class VideoDetailResponse(CamelModel):
    """조회 결과 — 저장하지 않는다."""

    detail: VideoSpecDetail | None = None
    notice: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# 확정 — goals.materials 슬롯에 저장 (HITL 게이트)
# ─────────────────────────────────────────────────────────────────────────────

MaterialsSpecDetail = Annotated[BookSpecDetail | VideoSpecDetail, Field(discriminator="kind")]


class MaterialsSpecConfirmRequest(CamelModel):
    """POST /plans/materials/spec-confirm — "이 자료 맞아요".

    `details` 는 위 조회 응답에서 받은 것을 **그대로** 되돌려 보낸다(② HITL — `search`→
    `confirm` 과 같은 왕복. 재조회하지 않는 이유는 `orchestrator/materials_spec.py` 참고).

    1~2건 — 책 1개, 영상 1개까지, 같은 종류를 두 번 보낼 수 없다. `study-method` 의
    `materialMix` 가 몇 건이 좋을지 권장하지만 강제하지 않는다 — 최종 선택은 사용자 몫이다.
    """

    details: list[MaterialsSpecDetail] = Field(min_length=1, max_length=2)
    interview_session_id: str | None = None

    @model_validator(mode="after")
    def _distinct_kinds(self) -> MaterialsSpecConfirmRequest:
        kinds = [d.kind for d in self.details]
        if len(kinds) != len(set(kinds)):
            raise ValueError("같은 종류(kind)를 두 번 보낼 수 없어요 — 책 1개, 영상 1개까지만.")
        return self


class MaterialsSpecConfirmResponse(CamelModel):
    goal_title: str
    kinds: list[Literal["book", "video"]]
    notice: str
    book_pace: BookPace | None = None
    """책을 확정했고 목표에 마감이 있으면 채워진다. 영상만 확정했거나 마감이 없으면 `None`."""
