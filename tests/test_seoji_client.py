"""국중 seoji 목차 클라이언트 (ADR-0010 §1 ③) — best-effort. 실패가 아니라 이 소스의
기본 동작(L0 실측: 도서 10권 중 9권이 `REASON_NO_TOC`).

네트워크는 타지 않는다 — `requests.get` 을 대체한다.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from reaction_backend.integrations.nl_seoji import client

# 실측(2026-09-03, "Java의 정석 : 기초편") 형식 그대로 — 개행 없이 점선(···)+페이지번호로
# 항목을 이어붙인 한 덩어리 문자열.
_REAL_TOC = (
    "Chapter 1. 자바를 시작하기 전에01 자바(Java)란? ····20"
    "02 자바의 역사 ····30"
    "Chapter 2. 변수01 변수란? ····52"
    "Chapter 3. 연산자01 연산자와 피연산자 ····98"
)


class _FakeResponse:
    def __init__(self, *, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self._body = body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._body


def _docs_body(**fields: str) -> dict[str, Any]:
    return {"docs": [fields]}


async def test_no_key_fails_without_a_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*a: Any, **k: Any) -> Any:
        raise AssertionError("key 가 없으면 네트워크를 타면 안 된다")

    monkeypatch.setattr(client.requests, "get", _explode)
    result = await client.lookup_toc("9788994492049", key="")
    assert not result.ok
    assert result.reason == client.REASON_NO_KEY


async def test_parses_toc_into_chapters_with_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _docs_body(TITLE="Java의 정석 : 기초편", BOOK_TB_CNT=_REAL_TOC)
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.lookup_toc("9788994492049", key="testkey")

    assert result.ok
    assert result.lookup is not None
    assert result.lookup.chapter_count == 3
    titles = [c.title for c in result.lookup.chapters]
    assert titles == ["Chapter 1. 자바를 시작하기 전에", "Chapter 2. 변수", "Chapter 3. 연산자"]
    # 소단원 나열(점선·페이지 번호)은 제목에서 잘려나가야 한다.
    assert "····" not in titles[0]


async def test_parses_monotonic_page_checkpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """실측(2026-09-04, "Java의 정석 : 기초편" 15챕터 전수)으로 검증한 휴리스틱 — 챕터
    경계마다 마지막 페이지 마커만 취하면 단조증가한다."""
    body = _docs_body(TITLE="Java의 정석 : 기초편", BOOK_TB_CNT=_REAL_TOC)
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.lookup_toc("9788994492049", key="testkey")

    assert result.ok
    assert result.lookup is not None
    assert result.lookup.has_page_checkpoints is True
    end_pages = [c.end_page for c in result.lookup.chapters]
    assert end_pages == [30, 52, 98]


async def test_non_monotonic_page_markers_are_dropped_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """페이지 번호가 거꾸로 가면 파싱이 실제로는 틀렸다는 뜻이다 — 잘못된 숫자를 사실인
    척 내보내는 것보다 전부 버리는 게 낫다(챕터 제목 목록 자체는 그래도 남는다)."""
    broken_toc = (
        "Chapter 1. 서론 부분입니다01 시작하며 배경을 설명합니다 ····50"
        "Chapter 2. 본론 부분입니다01 전개를 자세히 다룹니다 ····10"  # 역행 — 파싱 오류를 흉내
        "Chapter 3. 결론 부분입니다01 마무리를 정리합니다 ····80"
    )
    assert len(broken_toc) >= 80  # _TOC_MIN_CHARS 게이트를 통과해야 챕터 파싱까지 간다
    body = _docs_body(TITLE="어떤 책", BOOK_TB_CNT=broken_toc)
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.lookup_toc("9780000000001", key="testkey")

    assert result.ok
    assert result.lookup is not None
    assert result.lookup.has_page_checkpoints is False
    assert all(c.end_page is None for c in result.lookup.chapters)
    assert result.lookup.chapter_count == 3  # 챕터 목록 자체는 살아남는다


async def test_does_not_guess_the_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """목차가 `BOOK_TB_CNT` 가 아닌 다른 필드에 와도 챕터 패턴으로 찾아낸다."""
    body = _docs_body(TITLE="어떤 책", SOME_OTHER_FIELD=_REAL_TOC)
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.lookup_toc("9780000000000", key="testkey")

    assert result.ok
    assert result.lookup is not None
    assert result.lookup.chapter_count == 3


async def test_short_fields_are_not_mistaken_for_a_toc(monkeypatch: pytest.MonkeyPatch) -> None:
    """제목처럼 짧은 필드가 우연히 챕터 키워드를 담고 있어도 목차로 오인하면 안 된다 —
    `_TOC_MIN_CHARS` 길이 게이트가 실제로 걸러내는지 확인한다."""
    body = _docs_body(TITLE="Chapter 1 이 들어간 책 제목", BOOK_TB_CNT="")
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.lookup_toc("9788965424765", key="testkey")

    assert not result.ok
    assert result.reason == client.REASON_NO_TOC


async def test_empty_toc_field_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """L0 실측: 10권 중 9권이 이 경로다 — 정상, 오류 아님."""
    body = _docs_body(TITLE="해커스 토익 RC", BOOK_TB_CNT="")
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.lookup_toc("9788965424765", key="testkey")

    assert not result.ok
    assert result.reason == client.REASON_NO_TOC


async def test_not_found_when_docs_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body={"docs": []}))
    result = await client.lookup_toc("0000000000000", key="testkey")
    assert not result.ok
    assert result.reason == client.REASON_NOT_FOUND


async def test_result_error_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"RESULT": "ERROR", "ERR_CODE": "010", "ERR_MESSAGE": "인증키 정보가 없습니다."}
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))
    result = await client.lookup_toc("9788965424765", key="badkey")
    assert not result.ok
    assert result.reason == client.REASON_UNAVAILABLE


async def test_timeout_is_reported_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*a: Any, **k: Any) -> Any:
        raise requests.Timeout()

    monkeypatch.setattr(client.requests, "get", _timeout)
    result = await client.lookup_toc("9788994492049", key="testkey")
    assert not result.ok
    assert result.reason == client.REASON_TIMEOUT
