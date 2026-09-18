"""알라딘 도서 클라이언트 (ADR-0010 §1 ②③) — 검색(후보만)·상세(페이지 수). 목차는 절대
조회하지 않는다(어느 endpoint 로도 안 온다 — L0 실측).

네트워크는 타지 않는다 — `requests.get` 을 대체한다(`test_web_fetch.py` 와 같은 패턴).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from reaction_backend.integrations.aladin import client


class _FakeResponse:
    def __init__(self, *, status: int = 200, body: str = "") -> None:
        self.status_code = status
        self.text = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _body(*items: dict[str, Any]) -> str:
    return json.dumps({"item": list(items)})


async def test_no_key_fails_without_a_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*a: Any, **k: Any) -> Any:
        raise AssertionError("key 가 없으면 네트워크를 타면 안 된다")

    monkeypatch.setattr(client.requests, "get", _explode)
    result = await client.search_books("토익", key="", limit=3)
    assert not result.ok
    assert result.reason == client.REASON_NO_KEY


async def test_parses_search_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """실측 필드명(2026-09-03 라이브 호출) 그대로 — 문서가 아니라 응답을 믿는다."""
    body = _body(
        {
            "title": "해커스 토익 RC 리딩",
            "author": "David Cho (지은이)",
            "publisher": "해커스어학연구소(Hackers)",
            "isbn13": "9788965422389",
            "cover": "https://image.aladin.co.kr/cover.jpg",
            "link": "https://www.aladin.co.kr/shop/wproduct.aspx?ItemId=1&amp;partner=openAPI",
        }
    )
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.search_books("토익", key="ttbtest", limit=3)

    assert result.ok
    book = result.books[0]
    assert book.title == "해커스 토익 RC 리딩"
    assert book.isbn13 == "9788965422389"
    # `&amp;` 이스케이프가 풀려야 링크가 바로 열린다.
    assert "&amp;" not in book.link_url
    assert "&partner=openAPI" in book.link_url


async def test_trailing_semicolon_in_body_does_not_break_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """알라딘은 가끔 JSON 끝에 `;` 를 붙여 준다(l0_materials_probe.py 실측)."""
    body = _body({"title": "자바의 정석", "isbn13": "9788994492049"}) + ";"
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.search_books("자바", key="ttbtest", limit=3)

    assert result.ok
    assert result.books[0].title == "자바의 정석"


async def test_empty_results_are_reported_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        client.requests, "get", lambda *a, **k: _FakeResponse(body=json.dumps({"item": []}))
    )
    result = await client.search_books("존재하지않는책", key="ttbtest", limit=3)
    assert not result.ok
    assert result.reason == client.REASON_EMPTY


async def test_timeout_is_reported_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*a: Any, **k: Any) -> Any:
        raise requests.Timeout()

    monkeypatch.setattr(client.requests, "get", _timeout)
    result = await client.search_books("토익", key="ttbtest", limit=3)
    assert not result.ok
    assert result.reason == client.REASON_TIMEOUT


# ─────────────────────────── lookup_book (ADR-0010 §1 ③) ───────────────────────────


def _lookup_body(*, title: str = "", author: str = "", page: int = 0) -> str:
    return json.dumps({"item": [{"title": title, "author": author, "subInfo": {"itemPage": page}}]})


async def test_lookup_no_key_fails_without_a_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*a: Any, **k: Any) -> Any:
        raise AssertionError("key 가 없으면 네트워크를 타면 안 된다")

    monkeypatch.setattr(client.requests, "get", _explode)
    result = await client.lookup_book("9788965424765", key="")
    assert not result.ok
    assert result.reason == client.REASON_NO_KEY


async def test_lookup_parses_page_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """실측 필드명(2026-09-03 라이브 호출): `subInfo.itemPage`."""
    body = _lookup_body(title="해커스 토익 RC 리딩", author="David Cho (지은이)", page=816)
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.lookup_book("9788965424765", key="ttbtest")

    assert result.ok
    assert result.lookup is not None
    assert result.lookup.title == "해커스 토익 RC 리딩"
    assert result.lookup.author == "David Cho (지은이)"
    assert result.lookup.page_count == 816


async def test_lookup_not_found_when_item_list_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        client.requests, "get", lambda *a, **k: _FakeResponse(body=json.dumps({"item": []}))
    )
    result = await client.lookup_book("0000000000000", key="ttbtest")
    assert not result.ok
    assert result.reason == client.REASON_NOT_FOUND


async def test_lookup_timeout_is_reported_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*a: Any, **k: Any) -> Any:
        raise requests.Timeout()

    monkeypatch.setattr(client.requests, "get", _timeout)
    result = await client.lookup_book("9788965424765", key="ttbtest")
    assert not result.ok
    assert result.reason == client.REASON_TIMEOUT


# ───────────────── 로그에 API 키가 남지 않는다 (inbox-7) ─────────────────

_SECRET = "ttbSECRET123"


def _leaky_failures() -> list[Any]:
    """`requests` 예외는 문자열에 요청 URL 전체(키 포함)를 담는다 — 실제 형식 그대로."""

    def _connection_error(*a: Any, **k: Any) -> Any:
        raise requests.ConnectionError(
            f"HTTPSConnectionPool: Max retries exceeded with url: /ttb/api/ItemSearch.aspx?ttbkey={_SECRET}&Query=a"
        )

    class _HttpErrorResponse(_FakeResponse):
        def raise_for_status(self) -> None:
            raise requests.HTTPError(
                f"503 Server Error for url: https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx?ttbkey={_SECRET}",
                response=self,
            )

    return [_connection_error, lambda *a, **k: _HttpErrorResponse(status=503)]


@pytest.mark.parametrize("failure", _leaky_failures())
async def test_search_failure_log_does_not_leak_the_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Any
) -> None:
    monkeypatch.setattr(client.requests, "get", failure)
    with caplog.at_level("DEBUG"):
        result = await client.search_books("토익", key=_SECRET, limit=3)
    assert result.reason == client.REASON_UNAVAILABLE
    assert "aladin search failed" in caplog.text, "실패 사실 자체는 남아야 한다"
    assert _SECRET not in caplog.text


@pytest.mark.parametrize("failure", _leaky_failures())
async def test_lookup_failure_log_does_not_leak_the_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Any
) -> None:
    monkeypatch.setattr(client.requests, "get", failure)
    with caplog.at_level("DEBUG"):
        result = await client.lookup_book("9788965422389", key=_SECRET)
    assert result.reason == client.REASON_UNAVAILABLE
    assert "aladin lookup failed" in caplog.text
    assert _SECRET not in caplog.text
