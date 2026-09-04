"""YouTube Data API v3 — 영상 강의(재생목록) 검색·상세 (ADR-0010 §1 ②③).

L0 스파이크(`docs/experiments/l0-materials-source-results.md`)의 핵심 발견: 영상 강의는
도서 목차보다 나은 것을 준다 — 재생목록의 영상 제목이 곧 커리큘럼이고, 단원마다 정확한
분량(재생시간)이 붙어 온다. 검색(`search_playlists`)은 `search.list` 한 번으로 후보
나열까지만 한다. 커리큘럼·분량까지 당겨오려면 `playlistItems`+`videos` 를 추가로 불러야
하는데(재생목록 하나당 여러 호출), 그건 후보를 **고른 뒤** 그 한 재생목록만 조회하는
`get_playlist_detail`(ADR-0010 §1 ③)의 몫이다.

⚠️ `search.list` 는 **100유닛** 이다(다른 endpoint 는 대부분 1유닛). 일일 쿼터
10,000유닛 기준 앱 전체에서 하루 ~100회 검색이 상한이다 — `config.youtube_api_key` 주석
참고. `playlistItems`/`videos` 는 1유닛이라 상세 조회 쪽은 훨씬 여유롭다. 초과하면
Google 이 403 + `reason=quotaExceeded` 를 준다(레거시 Google API 에러 포맷, 실측하지
않고 공식 문서 포맷을 그대로 따름 — 다르면 `REASON_UNAVAILABLE` 로 안전하게 폴백된다).

`web_fetch/fetcher.py` 와 같은 패턴 — 동기 `requests` 를 `asyncio.to_thread` 로 감싼다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

import requests

logger = logging.getLogger(__name__)

_SEARCH_URL: Final = "https://www.googleapis.com/youtube/v3/search"
_PLAYLISTS_URL: Final = "https://www.googleapis.com/youtube/v3/playlists"
_PLAYLIST_ITEMS_URL: Final = "https://www.googleapis.com/youtube/v3/playlistItems"
_VIDEOS_URL: Final = "https://www.googleapis.com/youtube/v3/videos"
_CONNECT_TIMEOUT: Final = 3.0
_READ_TIMEOUT: Final = 5.0
_HARD_TIMEOUT: Final = 8.0
# 재생목록 상세 조회 상한 — 이보다 긴 재생목록도 있지만(L0 실측: 시나공 정보처리기사
# 200편+), 계획 분해에 필요한 건 "단원 목록" 이지 무한 스크롤이 아니다. 상한에 걸리면
# `PlaylistDetail.truncated=True` 로 밝힌다(조용히 자르지 않는다).
_MAX_CURRICULUM_ITEMS: Final = 200

REASON_NO_KEY: Final = "no_key"
REASON_TIMEOUT: Final = "timeout"
REASON_QUOTA: Final = "quota_exceeded"
REASON_UNAVAILABLE: Final = "unavailable"
REASON_EMPTY: Final = "empty"
REASON_NOT_FOUND: Final = "not_found"


@dataclass(frozen=True, slots=True)
class PlaylistResult:
    """검색 후보 1건 — 커리큘럼(영상 제목)·분량(재생시간) 없음(이 단계에서는 조회하지 않는다)."""

    playlist_id: str
    title: str
    channel_title: str
    thumbnail_url: str
    playlist_url: str


@dataclass(slots=True)
class SearchResult:
    """가져온 후보 목록, 또는 못 가져온 이유. `playlists` 가 비어 있으면 `reason` 이 채워진다."""

    playlists: list[PlaylistResult] = field(default_factory=list)
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.playlists)


def _error_reason(response: requests.Response) -> str:
    if response.status_code == 403:
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            return REASON_UNAVAILABLE
        errors = (body.get("error") or {}).get("errors") or []
        if errors and errors[0].get("reason") in ("quotaExceeded", "dailyLimitExceeded"):
            return REASON_QUOTA
    return REASON_UNAVAILABLE


def _search_sync(query: str, key: str, limit: int) -> SearchResult:
    try:
        response = requests.get(
            _SEARCH_URL,
            params={
                "key": key,
                "part": "snippet",
                "q": query,
                "type": "playlist",
                "maxResults": str(limit),
                "relevanceLanguage": "ko",
                "regionCode": "KR",
            },
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
    except requests.Timeout:
        return SearchResult(reason=REASON_TIMEOUT)
    except requests.RequestException:
        logger.warning("youtube search failed", exc_info=True)
        return SearchResult(reason=REASON_UNAVAILABLE)

    if not response.ok:
        reason = _error_reason(response)
        if reason != REASON_QUOTA:
            logger.warning("youtube search returned %s", response.status_code)
        return SearchResult(reason=reason)

    try:
        body = response.json()
    except ValueError:
        return SearchResult(reason=REASON_UNAVAILABLE)

    playlists: list[PlaylistResult] = []
    for item in body.get("items") or []:
        playlist_id = (item.get("id") or {}).get("playlistId")
        snippet = item.get("snippet") or {}
        if not playlist_id or not snippet.get("title"):
            continue
        thumb = (snippet.get("thumbnails") or {}).get("medium") or (
            snippet.get("thumbnails") or {}
        ).get("default", {})
        playlists.append(
            PlaylistResult(
                playlist_id=str(playlist_id),
                title=str(snippet["title"]),
                channel_title=str(snippet.get("channelTitle", "")),
                thumbnail_url=str(thumb.get("url", "")),
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
            )
        )
    if not playlists:
        return SearchResult(reason=REASON_EMPTY)
    return SearchResult(playlists=playlists)


async def search_playlists(query: str, *, key: str, limit: int = 5) -> SearchResult:
    """영상 강의(재생목록) 검색 — **후보만**, 커리큘럼/분량 없음(ADR-0010 §4 — 후보 선택
    이후 단계 몫)."""
    if not key:
        return SearchResult(reason=REASON_NO_KEY)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_search_sync, query, key, limit), timeout=_HARD_TIMEOUT
        )
    except TimeoutError:
        return SearchResult(reason=REASON_TIMEOUT)
    except Exception:  # noqa: BLE001 — 자료 검색 실패가 계획 생성을 막으면 안 된다
        logger.warning("youtube search_playlists failed", exc_info=True)
        return SearchResult(reason=REASON_UNAVAILABLE)


_DURATION_RE = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_iso8601_duration(value: str) -> int:
    """`PT1H23M45S` → 초. YouTube `contentDetails.duration` 형식(L0 실측)."""
    m = _DURATION_RE.fullmatch(value)
    if not m:
        return 0
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


@dataclass(frozen=True, slots=True)
class CurriculumItem:
    """재생목록의 영상 1편 — 제목이 곧 단원명, 재생시간이 곧 분량(L0 핵심 발견)."""

    title: str
    seconds: int


@dataclass(frozen=True, slots=True)
class PlaylistDetail:
    """`get_playlist_detail` 성공 결과.

    `title`/`channel_title` 은 `playlists.list` 로 **따로** 받는다 — `playlistItems` 의
    `snippet.title` 은 **영상** 제목이지 재생목록 제목이 아니다(먼저 그렇게 짰다가 첫
    영상 제목이 재생목록 제목으로 새는 걸 잡았다). 호출부가 `playlist_id` 하나만 들고
    이 함수를 부르므로(검색 결과를 다시 들려보내지 않는다) 재생목록 자체의 제목도
    여기서 조회해야 한다 — `playlists.list` 1유닛으로 싸다.
    """

    title: str
    channel_title: str
    video_count: int
    """재생목록의 **실제** 총 영상 수(API `pageInfo.totalResults`) — `curriculum` 이 상한에
    잘려도 이 값은 정확하다."""
    total_seconds: int
    """`curriculum` 에 담긴 영상들의 재생시간 합. 상한에 잘렸으면 실제 총합보다 작다."""
    curriculum: list[CurriculumItem] = field(default_factory=list)
    truncated: bool = False
    """`_MAX_CURRICULUM_ITEMS` 상한에 걸려 일부만 담았다는 뜻 — 조용히 자르지 않는다."""


@dataclass(slots=True)
class DetailResult:
    """가져온 상세, 또는 못 가져온 이유."""

    detail: PlaylistDetail | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.detail is not None


def _playlist_meta_sync(playlist_id: str, key: str) -> tuple[str, str] | None:
    """재생목록 자체의 (title, channelTitle). 실패하면 조용히 빈 값 — 커리큘럼이 이
    응답의 핵심이라, 제목 하나 때문에 전체를 실패시키지 않는다."""
    try:
        response = requests.get(
            _PLAYLISTS_URL,
            params={"key": key, "part": "snippet", "id": playlist_id},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        return None
    items = body.get("items") or []
    if not items:
        return None
    snippet = items[0].get("snippet") or {}
    return str(snippet.get("title", "")), str(snippet.get("channelTitle", ""))


def _playlist_detail_sync(playlist_id: str, key: str) -> DetailResult:
    meta = _playlist_meta_sync(playlist_id, key)
    title, channel_title = meta if meta else ("", "")

    ordered: list[tuple[str, str]] = []  # (videoId, title)
    total_results = 0
    page: str | None = None
    truncated = False
    while True:
        params: dict[str, str] = {
            "key": key,
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": "50",
        }
        if page:
            params["pageToken"] = page
        try:
            response = requests.get(
                _PLAYLIST_ITEMS_URL, params=params, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)
            )
        except requests.Timeout:
            return DetailResult(reason=REASON_TIMEOUT)
        except requests.RequestException:
            logger.warning("youtube playlistItems failed", exc_info=True)
            return DetailResult(reason=REASON_UNAVAILABLE)

        if not response.ok:
            reason = _error_reason(response)
            if reason != REASON_QUOTA:
                logger.warning("youtube playlistItems returned %s", response.status_code)
            return DetailResult(reason=reason)

        try:
            body = response.json()
        except ValueError:
            return DetailResult(reason=REASON_UNAVAILABLE)

        total_results = int((body.get("pageInfo") or {}).get("totalResults") or total_results)
        for item in body.get("items") or []:
            video_id = (item.get("contentDetails") or {}).get("videoId")
            video_title = (item.get("snippet") or {}).get("title")
            if video_id and video_title:
                ordered.append((str(video_id), str(video_title)))

        page = body.get("nextPageToken")
        if not page:
            break
        if len(ordered) >= _MAX_CURRICULUM_ITEMS:
            truncated = True
            break

    if not ordered:
        return DetailResult(reason=REASON_NOT_FOUND)

    # 길이 합산 — videos.list 는 한 번에 50개까지.
    seconds: dict[str, int] = {}
    for i in range(0, len(ordered), 50):
        chunk = [v for v, _ in ordered[i : i + 50]]
        try:
            response = requests.get(
                _VIDEOS_URL,
                params={"key": key, "part": "contentDetails", "id": ",".join(chunk)},
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        except requests.Timeout:
            return DetailResult(reason=REASON_TIMEOUT)
        except requests.RequestException:
            logger.warning("youtube videos failed", exc_info=True)
            return DetailResult(reason=REASON_UNAVAILABLE)
        if not response.ok:
            return DetailResult(reason=_error_reason(response))
        try:
            detail_body = response.json()
        except ValueError:
            return DetailResult(reason=REASON_UNAVAILABLE)
        for item in detail_body.get("items") or []:
            duration = (item.get("contentDetails") or {}).get("duration")
            if duration:
                seconds[item["id"]] = parse_iso8601_duration(duration)

    curriculum = [
        CurriculumItem(title=video_title, seconds=seconds.get(vid, 0))
        for vid, video_title in ordered
    ]
    return DetailResult(
        detail=PlaylistDetail(
            title=title,
            channel_title=channel_title,
            video_count=total_results or len(ordered),
            total_seconds=sum(c.seconds for c in curriculum),
            curriculum=curriculum,
            truncated=truncated,
        )
    )


async def get_playlist_detail(playlist_id: str, *, key: str) -> DetailResult:
    """재생목록 ID → 커리큘럼(영상 제목 목록) + 분량(재생시간). 후보를 고른 뒤 **그 한
    재생목록만** 부른다(ADR-0010 §1 ③)."""
    if not key:
        return DetailResult(reason=REASON_NO_KEY)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_playlist_detail_sync, playlist_id, key), timeout=_HARD_TIMEOUT
        )
    except TimeoutError:
        return DetailResult(reason=REASON_TIMEOUT)
    except Exception:  # noqa: BLE001 — 자료 검색 실패가 계획 생성을 막으면 안 된다
        logger.warning("youtube get_playlist_detail failed", exc_info=True)
        return DetailResult(reason=REASON_UNAVAILABLE)
