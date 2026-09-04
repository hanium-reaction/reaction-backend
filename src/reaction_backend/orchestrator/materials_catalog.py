"""자료 검색 카탈로그 — 알라딘/YouTube 후보 병행 조회 (ADR-0010 §1 ②).

LLM 을 호출하지 않는다 — 사용자가 확인한 검색어로 실제 API 를 두드려 후보를 모을 뿐이다.
두 소스는 **독립적으로** 실패할 수 있다(키 없음·쿼터 초과·네트워크) — 한쪽이 죽어도 다른
쪽 결과는 그대로 낸다. 부분 성공을 실패로 뭉개지 않는다.
"""

from __future__ import annotations

from reaction_backend.config import get_settings
from reaction_backend.integrations.aladin import client as aladin_client
from reaction_backend.integrations.youtube import client as youtube_client
from reaction_backend.schemas.materials_catalog import (
    MAX_CANDIDATES,
    BookCandidate,
    MaterialsCatalogResponse,
    VideoCandidate,
)

_ALADIN_NOTICE: dict[str, str] = {
    aladin_client.REASON_NO_KEY: "지금은 도서 검색을 쓸 수 없어요.",
    aladin_client.REASON_TIMEOUT: "도서 검색이 제때 응답하지 않았어요. 다시 시도해 주세요.",
    aladin_client.REASON_UNAVAILABLE: "지금은 도서 검색이 잘 되지 않네요. 잠시 후 다시 시도해 주세요.",
    aladin_client.REASON_EMPTY: "이 검색어로는 도서를 찾지 못했어요. 검색어를 바꿔 보세요.",
}
_YOUTUBE_NOTICE: dict[str, str] = {
    youtube_client.REASON_NO_KEY: "지금은 영상 검색을 쓸 수 없어요.",
    youtube_client.REASON_TIMEOUT: "영상 검색이 제때 응답하지 않았어요. 다시 시도해 주세요.",
    youtube_client.REASON_QUOTA: "오늘 쓸 수 있는 영상 검색을 다 썼어요. 내일 다시 시도해 주세요.",
    youtube_client.REASON_UNAVAILABLE: "지금은 영상 검색이 잘 되지 않네요. 잠시 후 다시 시도해 주세요.",
    youtube_client.REASON_EMPTY: "이 검색어로는 영상 강의를 찾지 못했어요. 검색어를 바꿔 보세요.",
}
_FALLBACK_NOTICE = "지금은 검색이 잘 되지 않네요. 잠시 후 다시 시도해 주세요."


async def search(*, book_query: str | None, video_query: str | None) -> MaterialsCatalogResponse:
    """검색어가 있는 소스만 두드린다. 둘 다 있으면 병행 실행하지 않고 순차 실행한다 —
    각각 8s 하드 타임아웃이 있는 단발 API 호출이라 동시성을 더할 값이 크지 않다."""
    settings = get_settings()

    books: list[BookCandidate] = []
    book_notice: str | None = None
    if book_query:
        result = await aladin_client.search_books(
            book_query, key=settings.aladin_ttb_key, limit=MAX_CANDIDATES
        )
        if result.ok:
            books = [
                BookCandidate(
                    title=b.title,
                    author=b.author,
                    publisher=b.publisher,
                    isbn13=b.isbn13,
                    cover_url=b.cover_url,
                    link_url=b.link_url,
                )
                for b in result.books
            ]
        else:
            book_notice = _ALADIN_NOTICE.get(result.reason or "", _FALLBACK_NOTICE)

    videos: list[VideoCandidate] = []
    video_notice: str | None = None
    if video_query:
        vresult = await youtube_client.search_playlists(
            video_query, key=settings.youtube_api_key, limit=MAX_CANDIDATES
        )
        if vresult.ok:
            videos = [
                VideoCandidate(
                    playlist_id=p.playlist_id,
                    title=p.title,
                    channel_title=p.channel_title,
                    thumbnail_url=p.thumbnail_url,
                    playlist_url=p.playlist_url,
                )
                for p in vresult.playlists
            ]
        else:
            video_notice = _YOUTUBE_NOTICE.get(vresult.reason or "", _FALLBACK_NOTICE)

    return MaterialsCatalogResponse(
        books=books,
        book_notice=book_notice,
        videos=videos,
        video_notice=video_notice,
    )


__all__ = ["search"]
