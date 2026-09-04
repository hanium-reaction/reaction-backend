"""알라딘 Open API — 도서 검색·상세 (ADR-0010 §1 ②③).

L0 스파이크(`docs/experiments/l0-materials-source-results.md`)가 확인한 사실: 이 API 는
목차를 주지 않는다(`OptResult=Toc` 는 존재하지 않는 파라미터 — 10권 전량 0자). 그래서
검색(`search_books`)은 **후보 나열까지만** 한다 — 전 후보의 상세를 미리 당겨오면 후보
하나당 API 호출이 하나씩 늘어난다. 페이지 수는 후보를 고른 뒤 그 한 권만 `ItemLookUp`
으로 조회한다(`lookup_book`, ADR-0010 §1 ③) — 목차는 여기서도 안 온다(`subInfo` 가
`itemPage`·`subTitle`·`originalTitle` 뿐, 2026-09-03 라이브 확인). 목차는
`integrations/nl_seoji` 의 best-effort 몫이다.

응답 필드는 문서가 아니라 실측으로 확정했다(2026-09-03, `ItemSearch`/`ItemLookUp` 라이브
호출) — `title`·`author`·`isbn13`·`cover`·`link`·`publisher`·`subInfo.itemPage`. `link`
는 `&amp;` 로 HTML 이스케이프되어 온다.

`web_fetch/fetcher.py` 와 같은 패턴 — 동기 `requests` 를 `asyncio.to_thread` 로 감싼다.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final

import requests

logger = logging.getLogger(__name__)

_API_BASE: Final = "https://www.aladin.co.kr/ttb/api"
_CONNECT_TIMEOUT: Final = 3.0
_READ_TIMEOUT: Final = 5.0
# to_thread 자체가 복귀하지 않는 최악까지 대비한 코루틴 상한 (선례: web_fetch/fetcher.py).
_HARD_TIMEOUT: Final = 8.0

REASON_NO_KEY: Final = "no_key"
REASON_TIMEOUT: Final = "timeout"
REASON_UNAVAILABLE: Final = "unavailable"
REASON_EMPTY: Final = "empty"
REASON_NOT_FOUND: Final = "not_found"


@dataclass(frozen=True, slots=True)
class BookResult:
    """검색 후보 1건 — 목차·페이지 없음(이 단계에서는 조회하지 않는다)."""

    title: str
    author: str
    publisher: str
    isbn13: str
    cover_url: str
    link_url: str


@dataclass(slots=True)
class SearchResult:
    """가져온 후보 목록, 또는 못 가져온 이유. `books` 가 비어 있으면 `reason` 이 채워진다."""

    books: list[BookResult] = field(default_factory=list)
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.books)


def _search_sync(query: str, key: str, limit: int) -> SearchResult:
    try:
        response = requests.get(
            f"{_API_BASE}/ItemSearch.aspx",
            params={
                "ttbkey": key,
                "Query": query,
                "QueryType": "Keyword",
                "SearchTarget": "Book",
                "MaxResults": str(limit),
                "start": "1",
                "Sort": "SalesPoint",
                "output": "js",
                "Version": "20131101",
            },
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        response.raise_for_status()
    except requests.Timeout:
        return SearchResult(reason=REASON_TIMEOUT)
    except requests.RequestException:
        logger.warning("aladin search failed", exc_info=True)
        return SearchResult(reason=REASON_UNAVAILABLE)

    try:
        # Content-Type 이 text/html 이어도 본문은 JSON — 끝에 `;` 가 붙는 경우가 있어
        # (l0_materials_probe.py 실측) 그대로 json() 하면 깨진다.
        body: dict[str, Any] = json.loads(response.text.strip().rstrip(";"))
    except json.JSONDecodeError:
        logger.warning("aladin search returned non-JSON body")
        return SearchResult(reason=REASON_UNAVAILABLE)

    items = body.get("item") or []
    books = [
        BookResult(
            title=str(item.get("title", "")),
            author=str(item.get("author", "")),
            publisher=str(item.get("publisher", "")),
            isbn13=str(item.get("isbn13") or item.get("isbn") or ""),
            cover_url=str(item.get("cover", "")),
            link_url=html.unescape(str(item.get("link", ""))),
        )
        for item in items
        if item.get("title")
    ]
    if not books:
        return SearchResult(reason=REASON_EMPTY)
    return SearchResult(books=books)


async def search_books(query: str, *, key: str, limit: int = 5) -> SearchResult:
    """도서 검색 — **후보만**, 목차/페이지 없음(ADR-0010 §4 — 후보 선택 이후 단계 몫)."""
    if not key:
        return SearchResult(reason=REASON_NO_KEY)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_search_sync, query, key, limit), timeout=_HARD_TIMEOUT
        )
    except TimeoutError:
        return SearchResult(reason=REASON_TIMEOUT)
    except Exception:  # noqa: BLE001 — 자료 검색 실패가 계획 생성을 막으면 안 된다
        logger.warning("aladin search_books failed", exc_info=True)
        return SearchResult(reason=REASON_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class BookLookup:
    """`lookup_book` 성공 결과 — 페이지 수. 목차는 없다(위 모듈 독스트링)."""

    title: str
    author: str
    page_count: int


@dataclass(slots=True)
class LookupResult:
    """가져온 상세, 또는 못 가져온 이유."""

    lookup: BookLookup | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.lookup is not None


def _lookup_sync(isbn13: str, key: str) -> LookupResult:
    try:
        response = requests.get(
            f"{_API_BASE}/ItemLookUp.aspx",
            params={
                "ttbkey": key,
                "itemIdType": "ISBN13",
                "ItemId": isbn13,
                "output": "js",
                "Version": "20131101",
            },
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        response.raise_for_status()
    except requests.Timeout:
        return LookupResult(reason=REASON_TIMEOUT)
    except requests.RequestException:
        logger.warning("aladin lookup failed", exc_info=True)
        return LookupResult(reason=REASON_UNAVAILABLE)

    try:
        body: dict[str, Any] = json.loads(response.text.strip().rstrip(";"))
    except json.JSONDecodeError:
        logger.warning("aladin lookup returned non-JSON body")
        return LookupResult(reason=REASON_UNAVAILABLE)

    items = body.get("item") or []
    if not items:
        return LookupResult(reason=REASON_NOT_FOUND)

    item = items[0]
    sub = item.get("subInfo") or {}
    page_count = int(sub.get("itemPage") or 0)
    return LookupResult(
        lookup=BookLookup(
            title=str(item.get("title", "")),
            author=str(item.get("author", "")),
            page_count=page_count,
        )
    )


async def lookup_book(isbn13: str, *, key: str) -> LookupResult:
    """ISBN13 → 페이지 수. 후보를 고른 뒤 **그 한 권만** 부른다(ADR-0010 §1 ③)."""
    if not key:
        return LookupResult(reason=REASON_NO_KEY)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_lookup_sync, isbn13, key), timeout=_HARD_TIMEOUT
        )
    except TimeoutError:
        return LookupResult(reason=REASON_TIMEOUT)
    except Exception:  # noqa: BLE001 — 자료 검색 실패가 계획 생성을 막으면 안 된다
        logger.warning("aladin lookup_book failed", exc_info=True)
        return LookupResult(reason=REASON_UNAVAILABLE)
