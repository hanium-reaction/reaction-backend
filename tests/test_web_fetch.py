"""사용자가 준 링크 열기 — SSRF 가드·수집·추출 (BE #226 1단계).

**이 파일의 절반은 보안 테스트다.** 서버가 임의 URL 을 여는 기능이라, 가드가 있는지가
아니라 **위반 입력을 실제로 만들어** 거절되는지를 본다([[guard-tests-need-violating-input]]
계열). 특히 EC2 메타데이터(`169.254.169.254`)와 우리 앱 자신(`127.0.0.1:8000`)은
이름으로만 막으면 DNS 로 우회되므로 **해석된 IP** 로 검사하는지까지 고정한다.

네트워크는 타지 않는다 — DNS(`resolved_addresses`)와 `requests.get` 을 대체한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from reaction_backend.integrations.web_fetch import extract, fetcher, url_guard

PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본은 '모든 호스트가 공인 IP 로 해석된다' — 개별 테스트가 필요할 때 덮어쓴다."""
    monkeypatch.setattr(url_guard, "resolved_addresses", lambda host: [PUBLIC_IP])


def _dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    monkeypatch.setattr(
        url_guard, "resolved_addresses", lambda host: mapping.get(host, [PUBLIC_IP])
    )


# ── URL 가드: 거절해야 하는 것들 ─────────────────────────


@pytest.mark.parametrize(
    ("url", "resolved", "reason"),
    [
        # EC2 인스턴스 메타데이터 — 여기에 IAM 자격증명이 있다. 이 한 줄이 이 기능의
        # 가장 큰 위험이다.
        ("http://169.254.169.254/latest/meta-data/", ["169.254.169.254"], "private_address"),
        # 우리 앱 자신 — 인증 미들웨어를 우회해 내부 라우트를 두드릴 수 있다.
        ("http://127.0.0.1/health", ["127.0.0.1"], "private_address"),
        ("http://10.0.0.5/", ["10.0.0.5"], "private_address"),
        ("http://172.16.0.9/", ["172.16.0.9"], "private_address"),
        ("http://192.168.1.1/", ["192.168.1.1"], "private_address"),
        ("http://[::1]/", ["::1"], "private_address"),
        # IPv4-mapped IPv6 — v6 로 왔다고 v4 사설 판정을 건너뛰면 뚫린다.
        ("http://sneaky.example/", ["::ffff:127.0.0.1"], "private_address"),
        # 이름은 평범한데 사설로 해석되는 경우(DNS rebinding 류) — 이름 검사로는 못 막는다.
        ("https://totally-normal.example/plan", ["10.1.2.3"], "private_address"),
        # A 레코드가 여럿이면 **하나라도** 사설이면 거절 — 어느 쪽으로 갈지 우리가 못 고른다.
        ("https://mixed.example/", [PUBLIC_IP, "127.0.0.1"], "private_address"),
    ],
)
def test_guard_rejects_internal_targets(
    monkeypatch: pytest.MonkeyPatch, url: str, resolved: list[str], reason: str
) -> None:
    monkeypatch.setattr(url_guard, "resolved_addresses", lambda host: resolved)
    with pytest.raises(url_guard.UnsafeUrl) as e:
        url_guard.validate_url(url)
    assert e.value.reason == reason


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", url_guard.REASON_SCHEME),
        ("ftp://example.com/x", url_guard.REASON_SCHEME),
        ("gopher://example.com/", url_guard.REASON_SCHEME),
        # `@` 앞은 사람 눈에 도메인처럼 보이지만 실제 접속은 뒤쪽으로 간다.
        ("https://real-site.com@127.0.0.1/", url_guard.REASON_MALFORMED),
        ("https:///path-only", url_guard.REASON_MALFORMED),
        # 표준 웹 포트가 아니면 대부분 내부 서비스다(우리 앱 8000, PG 5432 …).
        ("http://example.com:8000/", url_guard.REASON_PORT),
        ("http://example.com:5432/", url_guard.REASON_PORT),
    ],
)
def test_guard_rejects_malformed_or_nonweb_urls(url: str, reason: str) -> None:
    with pytest.raises(url_guard.UnsafeUrl) as e:
        url_guard.validate_url(url)
    assert e.value.reason == reason


def test_guard_rejects_when_dns_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(host: str) -> list[str]:
        raise OSError("no such host")

    monkeypatch.setattr(url_guard, "resolved_addresses", _boom)
    with pytest.raises(url_guard.UnsafeUrl) as e:
        url_guard.validate_url("https://nope.example/")
    assert e.value.reason == url_guard.REASON_DNS


@pytest.mark.parametrize("url", ["https://example.com/syllabus", "http://example.com:80/a?b=1"])
def test_guard_allows_public_web_urls(url: str) -> None:
    assert url_guard.validate_url(url) == url


@pytest.mark.parametrize("resolved", [["8.8.8.8"], ["::ffff:8.8.8.8"], ["2001:4860:4860::8888"]])
def test_guard_does_not_over_block_public_addresses(
    monkeypatch: pytest.MonkeyPatch, resolved: list[str]
) -> None:
    """공인이면 형태가 달라도 통과해야 한다 — 여기서 막으면 기능이 조용히 죽는다.

    `::ffff:8.8.8.8`(IPv4-mapped IPv6)를 넣은 이유: 위 차단 테스트의 짝이다. 두 방향을
    같이 고정해야 "매핑 주소는 무조건 차단" 같은 과잉 차단으로 기울지 않는다.
    """
    monkeypatch.setattr(url_guard, "resolved_addresses", lambda host: resolved)
    assert url_guard.validate_url("https://public.example/x")


# ── 수집 ──────────────────────────────────────────────────


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status
        self.headers = headers if headers is not None else {"Content-Type": "text/html"}
        self._body = body
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self.served = 0  # 실제로 소비된 바이트 — '읽기를 멈췄는가' 검사용

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    @property
    def is_permanent_redirect(self) -> bool:
        return self.status_code in (301, 308)

    def iter_content(self, chunk_size: int = 8192):  # noqa: ANN201
        for i in range(0, len(self._body), chunk_size):
            chunk = self._body[i : i + chunk_size]
            self.served += len(chunk)
            yield chunk

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _serve(monkeypatch: pytest.MonkeyPatch, *responses: _FakeResponse) -> list[str]:
    """요청 순서대로 응답을 돌려주고, **실제로 요청된 URL 목록**을 반환한다."""
    seen: list[str] = []
    queue = list(responses)

    def _get(url: str, **kwargs: Any) -> _FakeResponse:
        seen.append(url)
        return queue.pop(0)

    monkeypatch.setattr(fetcher.requests, "get", _get)
    return seen


async def test_fetches_and_strips_html(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<html><head><style>p{color:red}</style></head><body><h1>3\xec\xa3\xbc\xec\xb0\xa8</h1><p>OT</p></body></html>"
    _serve(monkeypatch, _FakeResponse(body=html))
    result = await fetcher.fetch_text("https://lecture.example/syllabus")
    assert result.ok
    assert "OT" in (result.text or "")
    assert "color:red" not in (result.text or ""), "style 안쪽이 본문으로 새어 나왔다"


async def test_redirect_to_internal_address_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """공개 URL 이 사설 대역으로 302 하는 것이 전형적인 SSRF 우회다.

    `requests` 의 자동 추적을 썼다면 여기서 그냥 뚫린다 — 그래서 우리가 직접 따라가며
    매 홉을 다시 검사한다.
    """
    _dns(monkeypatch, {"evil.example": [PUBLIC_IP], "169.254.169.254": ["169.254.169.254"]})
    seen = _serve(
        monkeypatch,
        _FakeResponse(
            status=302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        ),
    )
    result = await fetcher.fetch_text("https://evil.example/start")
    assert not result.ok
    assert result.reason == url_guard.REASON_PRIVATE
    assert len(seen) == 1, "차단된 목적지에 요청을 보냈다"


async def test_first_hop_is_validated_at_the_public_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fetch_text` 에 사설 주소를 **바로** 넣어도 요청 없이 막힌다.

    기존 내부 대역 테스트는 `url_guard.validate_url` 을 직접 부르고, `fetch_text` 를 타는
    테스트는 전부 공개 URL 에서 출발한다. 그래서 검사를 리다이렉트 홉에만 남기는 뮤턴트가
    전 스위트를 초록으로 통과하면서 **첫 홉은 그대로 나가는** 구멍이 생긴다 —
    `fetcher.py` 스스로 "이 한 줄이 이 기능의 가장 큰 위험" 이라 적어둔 그 경로다.
    사용자가 붙여넣는 값이 곧 첫 홉이므로 진입점에서 직접 고정한다.
    """
    _dns(monkeypatch, {"169.254.169.254": ["169.254.169.254"]})
    seen = _serve(monkeypatch)
    result = await fetcher.fetch_text("http://169.254.169.254/latest/meta-data/")
    assert not result.ok
    assert result.reason == url_guard.REASON_PRIVATE
    assert seen == [], "사설 주소에 요청이 나갔다"


async def test_redirect_chain_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    hops = [
        _FakeResponse(status=302, headers={"Location": f"https://example.com/{i}"})
        for i in range(fetcher._MAX_REDIRECTS + 1)
    ]
    _serve(monkeypatch, *hops)
    result = await fetcher.fetch_text("https://example.com/start")
    assert result.reason == fetcher.REASON_TOO_MANY_REDIRECTS


async def test_relative_redirect_is_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(
        monkeypatch,
        _FakeResponse(status=302, headers={"Location": "/real"}),
        _FakeResponse(body=b"<p>syllabus</p>"),
    )
    result = await fetcher.fetch_text("https://example.com/start")
    assert result.ok
    assert seen[1] == "https://example.com/real"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, fetcher.REASON_LOGIN_REQUIRED),
        (403, fetcher.REASON_LOGIN_REQUIRED),
        (404, fetcher.REASON_NOT_FOUND),
        (500, fetcher.REASON_UNAVAILABLE),
    ],
)
async def test_error_statuses_map_to_reasons(
    monkeypatch: pytest.MonkeyPatch, status: int, reason: str
) -> None:
    """사유를 구분해야 사용자에게 왜 안 됐는지 말할 수 있다(로그인 필요 vs 페이지 없음)."""
    _serve(monkeypatch, _FakeResponse(status=status))
    result = await fetcher.fetch_text("https://example.com/x")
    assert result.reason == reason


async def test_non_text_content_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _FakeResponse(headers={"Content-Type": "application/pdf"}, body=b"%PDF"))
    result = await fetcher.fetch_text("https://example.com/a.pdf")
    assert result.reason == fetcher.REASON_UNSUPPORTED_TYPE


async def test_body_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """무한 스트림에 메모리를 내주지 않는다 — **읽기를 멈추는지**를 본다.

    결과 길이만 보면 부족하다: 상한 없이 끝까지 읽고 마지막에 잘라도 길이는 똑같이
    작아진다(뮤테이션에서 실제로 그 구멍이 드러났다). 방어의 본질은 '다 읽지 않는 것'
    이므로 **소비된 바이트**를 단언한다.
    """
    huge = b"<p>" + b"a" * (fetcher._MAX_BYTES * 3)
    response = _FakeResponse(body=huge)
    _serve(monkeypatch, response)
    result = await fetcher.fetch_text("https://example.com/huge")

    assert result.ok
    assert len(result.text or "") <= fetcher._MAX_BYTES
    # 상한 + 마지막 청크 하나까지가 허용치. 그 이상 읽었으면 스트림을 다 삼킨 것이다.
    assert response.served <= fetcher._MAX_BYTES + 8192, (
        f"상한을 넘겨 {response.served} 바이트를 읽었다 — 무한 스트림이면 메모리가 터진다"
    )


async def test_timeout_becomes_a_reason_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """계획 생성 중이라 예외가 위로 새면 계획 전체가 실패한다."""

    def _timeout(url: str, **kwargs: Any) -> _FakeResponse:
        raise fetcher.requests.Timeout("too slow")

    monkeypatch.setattr(fetcher.requests, "get", _timeout)
    result = await fetcher.fetch_text("https://slow.example/")
    assert result.reason == fetcher.REASON_TIMEOUT


async def test_unexpected_exception_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, **kwargs: Any) -> _FakeResponse:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(fetcher.requests, "get", _boom)
    result = await fetcher.fetch_text("https://example.com/")
    assert result.reason == fetcher.REASON_UNAVAILABLE


async def test_empty_page_is_not_treated_as_material(monkeypatch: pytest.MonkeyPatch) -> None:
    """빈 본문을 '자료 있음' 으로 넘기면 프롬프트의 지어내기 방지 가드가 무력화된다."""
    _serve(monkeypatch, _FakeResponse(body=b"<html><body>  </body></html>"))
    result = await fetcher.fetch_text("https://example.com/blank")
    assert result.reason == fetcher.REASON_EMPTY


async def test_request_identifies_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """브라우저인 척하지 않는다 — 사이트가 우리를 식별할 수 있어야 한다."""
    captured: dict[str, Any] = {}

    def _get(url: str, **kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse(body=b"<p>ok</p>")

    monkeypatch.setattr(fetcher.requests, "get", _get)
    await fetcher.fetch_text("https://example.com/")
    assert "reaction-backend" in captured["headers"]["User-Agent"]
    assert captured["allow_redirects"] is False, "자동 리다이렉트를 켜면 가드가 우회된다"
    assert captured["timeout"] == (fetcher._CONNECT_TIMEOUT, fetcher._READ_TIMEOUT)


# ── HTML → 텍스트 ─────────────────────────────────────────


def test_extract_drops_script_and_style() -> None:
    text = extract.html_to_text("<div>본문<script>alert(1)</script><style>a{}</style>끝</div>")
    assert "alert" not in text and "a{}" not in text
    assert "본문" in text and "끝" in text


def test_extract_breaks_blocks_so_words_do_not_merge() -> None:
    text = extract.html_to_text("<li>1주차</li><li>2주차</li>")
    assert "1주차2주차" not in text


def test_extract_decodes_entities() -> None:
    assert "&amp;" not in extract.html_to_text("<p>A &amp; B</p>")


def test_extract_survives_broken_markup() -> None:
    """닫는 태그만 있는 문서에서 터지면 계획 생성이 통째로 실패한다."""
    assert "내용" in extract.html_to_text("</div></span><p>내용")
