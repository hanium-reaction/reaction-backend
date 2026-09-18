"""사용자가 준 링크의 본문을 가져온다 (BE #226 1단계).

`requests` 는 동기라서 `web_push/sender.py` 와 같은 방식으로 감싼다 — `to_thread` +
**이중 timeout**(스레드가 영영 안 돌아오는 최악까지 대비). 계획 생성은 이미 LLM
타임아웃에 빠듯하므로(#179) 여기서 오래 붙잡으면 안 된다.

**실패는 정상 경로다.** 로그인 필요·응답 없음·너무 큼 — 어느 쪽이든 `reason` 만 남기고
호출자는 기존 동작(`(없음)` + 되묻기)으로 폴백한다. 예외를 위로 던지지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin

import requests

from reaction_backend.integrations.web_fetch import extract, pinned_http, url_guard

logger = logging.getLogger(__name__)

# 계획 생성 안에서 도는 왕복이라 짧게 잡는다. 자료를 못 가져오는 것보다 계획이 통째로
# 늦어지는 게 나쁘다 (#226 설계 코멘트).
_CONNECT_TIMEOUT: Final = 3.0
_READ_TIMEOUT: Final = 5.0
# to_thread 자체가 복귀하지 않는 최악까지 대비한 코루틴 상한 (선례: web_push/sender.py).
_HARD_TIMEOUT: Final = 8.0

_MAX_BYTES: Final = 512 * 1024
_MAX_REDIRECTS: Final = 3
_TEXT_TYPES: Final[tuple[str, ...]] = ("text/html", "text/plain", "application/xhtml+xml")

# 브라우저인 척하지 않는다 — 우리가 봇임을 밝히는 쪽이 정직하고, robots 정책을 쓰는
# 사이트가 우리를 식별할 수 있다.
_USER_AGENT: Final = "reaction-backend/1.0 (+https://github.com/hanium-reaction)"

REASON_LOGIN_REQUIRED: Final = "login_required"
REASON_NOT_FOUND: Final = "not_found"
REASON_TOO_MANY_REDIRECTS: Final = "too_many_redirects"
REASON_UNSUPPORTED_TYPE: Final = "unsupported_type"
REASON_EMPTY: Final = "empty_body"
REASON_TIMEOUT: Final = "timeout"
REASON_UNAVAILABLE: Final = "unavailable"

# 테스트가 네트워크 없이 갈아 끼우는 지점 — 실제로는 `pin` 이 확인한 IP 로만 접속하는 세션.
_pinned_session = pinned_http.session


@dataclass(frozen=True)
class FetchResult:
    """가져온 텍스트, 또는 못 가져온 이유. 둘 중 하나만 채워진다."""

    text: str | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.text is not None


def _read_capped(response: requests.Response) -> str:
    """본문을 상한까지만 읽는다 — 무한 스트림에 메모리를 내주지 않게."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=8192):
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_BYTES:
            break
    raw = b"".join(chunks)[:_MAX_BYTES]
    encoding = response.encoding or response.apparent_encoding or "utf-8"
    return raw.decode(encoding, errors="replace")


def _fetch_sync(url: str) -> FetchResult:
    """리다이렉트를 **직접** 따라간다 — 매 홉마다 목적지를 다시 검사하기 위해서다.

    `requests` 의 자동 추적(`allow_redirects=True`)을 쓰면 공개 URL 이 `127.0.0.1` 로
    302 하는 순간 우리 가드를 그냥 통과한다. 그래서 302 를 우리가 받아 `url_guard.pin`
    을 다시 태운다. 접속은 매 홉 `pin` 이 확인한 IP 로만 한다(`pinned_http`, inbox-1).
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            target = url_guard.pin(current)
        except url_guard.UnsafeUrl as e:
            return FetchResult(None, e.reason)
        with _pinned_session(target.addresses) as session:
            try:
                response = session.get(
                    target.url,
                    headers={"User-Agent": _USER_AGENT, "Accept-Language": "ko,en;q=0.8"},
                    timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                    allow_redirects=False,
                    stream=True,
                )
            except requests.Timeout:
                return FetchResult(None, REASON_TIMEOUT)
            except requests.RequestException:
                return FetchResult(None, REASON_UNAVAILABLE)

            with response:
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        return FetchResult(None, REASON_UNAVAILABLE)
                    current = urljoin(target.url, location)
                    continue
                if response.status_code in (401, 403):
                    return FetchResult(None, REASON_LOGIN_REQUIRED)
                if response.status_code == 404:
                    return FetchResult(None, REASON_NOT_FOUND)
                if response.status_code >= 400:
                    return FetchResult(None, REASON_UNAVAILABLE)

                content_type = (
                    response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                )
                if content_type and not content_type.startswith(_TEXT_TYPES):
                    return FetchResult(None, REASON_UNSUPPORTED_TYPE)
                body = _read_capped(response)

        text = extract.html_to_text(body) if "html" in content_type else body.strip()
        return FetchResult(text, None) if text else FetchResult(None, REASON_EMPTY)
    return FetchResult(None, REASON_TOO_MANY_REDIRECTS)


async def fetch_text(url: str) -> FetchResult:
    """`_fetch_sync` 를 스레드로 돌리고 상한을 건다. 예외는 여기서 끝난다."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_fetch_sync, url), timeout=_HARD_TIMEOUT)
    except TimeoutError:
        return FetchResult(None, REASON_TIMEOUT)
    except Exception:  # noqa: BLE001 — 자료 수집 실패가 계획 생성을 막으면 안 된다
        logger.warning("web_fetch failed", exc_info=True)
        return FetchResult(None, REASON_UNAVAILABLE)
