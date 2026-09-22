"""사용자가 준 링크의 본문을 가져온다 (BE #226 1단계).

`requests` 는 동기라서 스레드에서 돌리고 **이중 timeout**(소켓 timeout + 코루틴 상한)을
건다. 계획 생성은 이미 LLM 타임아웃에 빠듯하므로(#179) 여기서 오래 붙잡으면 안 된다.

스레드는 공용 기본 풀(`asyncio.to_thread`)이 아니라 **이 모듈 전용 풀**에서 돈다 (inbox-8).
사용자가 준 링크는 상대가 누구든 될 수 있다 — 일부러 느리게 흘리는 서버에 공용 풀이
붙잡히면 web push·캘린더·도서 검색까지 같이 멈춘다. 전용 풀이면 최악에도 링크 수집만
느려진다. 코루틴 상한이 지나면 `SocketWatch` 로 소켓을 끊어 스레드도 돌려받는다.

**실패는 정상 경로다.** 로그인 필요·응답 없음·너무 큼 — 어느 쪽이든 `reason` 만 남기고
호출자는 기존 동작(`(없음)` + 되묻기)으로 폴백한다. 예외를 위로 던지지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
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
# 스레드가 복귀하지 않는 최악까지 대비한 코루틴 상한 (선례: web_push/sender.py). 스레드
# 안에서도 같은 시한을 본다 — 본문을 조금씩 계속 흘리는 서버를 끝없이 읽지 않게.
_HARD_TIMEOUT: Final = 8.0
# 링크 수집 전용 풀 크기. 계획 생성 한 번에 링크는 하나만 연다(`materials_resolver`) —
# 동시 계획 생성 몇 건을 받기엔 충분하고, 전부 붙잡혀도 다른 기능은 영향이 없다.
_MAX_WORKERS: Final = 4
_EXECUTOR: Final = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="web_fetch")

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


def _read_capped(response: requests.Response, deadline: float) -> str | None:
    """본문을 상한까지만 읽는다 — 무한 스트림에 메모리를 내주지 않게.

    시한(`deadline`, `time.monotonic` 기준)을 넘기면 `None` — 조금씩 끝없이 흘리는 서버를
    붙들고 있지 않는다. 청크 하나가 끝나야 검사할 수 있으므로, 청크 하나를 채우지도 않고
    버티는 경우는 `fetch_text` 가 소켓을 끊어 끝낸다.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=8192):
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_BYTES:
            break
        if time.monotonic() >= deadline:
            return None
    raw = b"".join(chunks)[:_MAX_BYTES]
    encoding = response.encoding or response.apparent_encoding or "utf-8"
    return raw.decode(encoding, errors="replace")


def _fetch_sync(url: str, watch: pinned_http.SocketWatch) -> FetchResult:
    """리다이렉트를 **직접** 따라간다 — 매 홉마다 목적지를 다시 검사하기 위해서다.

    `requests` 의 자동 추적(`allow_redirects=True`)을 쓰면 공개 URL 이 `127.0.0.1` 로
    302 하는 순간 우리 가드를 그냥 통과한다. 그래서 302 를 우리가 받아 `url_guard.pin`
    을 다시 태운다. 접속은 매 홉 `pin` 이 확인한 IP 로만 한다(`pinned_http`, inbox-1).

    `watch` 는 이 수집이 연 연결을 붙들어 둔다 — 시한이 지나면 `fetch_text` 가 끊는다.
    """
    try:
        return _follow(url, watch, deadline=time.monotonic() + _HARD_TIMEOUT)
    finally:
        watch.close()


def _follow(url: str, watch: pinned_http.SocketWatch, *, deadline: float) -> FetchResult:
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if time.monotonic() >= deadline:
            return FetchResult(None, REASON_TIMEOUT)
        try:
            target = url_guard.pin(current)
        except url_guard.UnsafeUrl as e:
            return FetchResult(None, e.reason)
        with _pinned_session(target.addresses, watch) as session:
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
                try:
                    body = _read_capped(response, deadline)
                except requests.RequestException:  # 본문 도중 끊김(시한 초과로 끊은 경우 포함)
                    return FetchResult(None, REASON_UNAVAILABLE)
                if body is None:
                    return FetchResult(None, REASON_TIMEOUT)

        text = extract.html_to_text(body) if "html" in content_type else body.strip()
        return FetchResult(text, None) if text else FetchResult(None, REASON_EMPTY)
    return FetchResult(None, REASON_TOO_MANY_REDIRECTS)


async def fetch_text(url: str) -> FetchResult:
    """`_fetch_sync` 를 전용 풀에서 돌리고 상한을 건다. 예외는 여기서 끝난다."""
    watch = pinned_http.SocketWatch()
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_EXECUTOR, _fetch_sync, url, watch), timeout=_HARD_TIMEOUT
        )
    except TimeoutError:
        return FetchResult(None, REASON_TIMEOUT)
    except Exception:  # noqa: BLE001 — 자료 수집 실패가 계획 생성을 막으면 안 된다
        logger.warning("web_fetch failed", exc_info=True)
        return FetchResult(None, REASON_UNAVAILABLE)
    finally:
        # `wait_for` 는 코루틴만 멈춘다 — 스레드는 소켓을 끊어야 돌아온다. 시한 초과·취소
        # 모두 여기로 온다. 정상 종료였다면 이미 다 닫혀 있어 할 일이 없다.
        watch.abort()
