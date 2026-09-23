"""사용자가 준 URL 을 서버가 열어도 되는지 판정 (BE #226).

**이 파일이 SSRF 방어의 전부다.** 우리 배포는 EC2 라서 서버가 임의 URL 을 여는 순간
아래가 사정권에 들어온다:

- `http://169.254.169.254/latest/meta-data/iam/security-credentials/` — 인스턴스
  메타데이터. 자격증명이 여기 있다.
- `http://127.0.0.1:8000/...` — 우리 앱 자신. 인증 미들웨어를 우회해 내부 라우트를
  두드릴 수 있다.
- RDS·사설 대역(10./172.16./192.168.).

그래서 **호스트 이름이 아니라 실제로 접속할 IP** 를 본다. `evil.com` 이 `127.0.0.1`
로 해석되는 DNS rebinding 류를 이름 검사로는 못 막는다. 리다이렉트도 매 홉마다 다시
검사해야 한다 — 공개 URL 이 사설 대역으로 302 하는 게 전형적인 우회다(호출자 책임,
`fetcher._fetch_sync` 참고).

**검사한 IP 가 곧 접속할 IP 여야 한다** (inbox-1). 예전엔 여기서 IP 를 본 뒤 URL 문자열만
돌려줬고, `requests` 가 그 이름을 **다시** 해석해 접속했다. 두 조회 사이에 답이 바뀌면
(TTL 0 rebinding) 검사는 공인 IP 를, 접속은 사설 IP 를 본다. 게다가 유니코드 호스트는
두 쪽이 **다른 이름**을 해석했다 — `socket.getaddrinfo` 는 옛 IDNA2003 으로 `ß` 를 `ss` 로
바꾸고, `requests` 는 UTS46 으로 `xn--zca` 로 바꾼다. 그래서 `pin` 은 `requests` 와 같은
방식으로 URL 을 정규화해 **그 ASCII 이름**을 해석하고, 통과한 IP 목록을 함께 돌려준다.
접속은 `pinned_http` 가 그 IP 로만 한다 — 두 번째 DNS 조회는 없다.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import requests

# 이 사유들은 사용자에게 보이는 문구로 번역된다 (`orchestrator/materials_resolver.py`).
REASON_SCHEME: Final = "unsupported_scheme"
REASON_MALFORMED: Final = "malformed"
REASON_PORT: Final = "unsupported_port"
REASON_DNS: Final = "dns_failed"
REASON_PRIVATE: Final = "private_address"

_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
# 표준 웹 포트만. 다른 포트는 대부분 내부 서비스(우리 앱 8000, DB 5432 …)를 가리킨다.
_ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443})


class UnsafeUrl(Exception):
    """이 URL 은 서버가 열면 안 된다. `reason` 은 위 REASON_* 상수."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_public(ip: str) -> bool:
    """공인 주소인가 — 사설·loopback·link-local·multicast·reserved 는 전부 거절.

    `is_global` 한 속성으로 묶는 이유: 개별 대역을 손으로 나열하면 IPv6 unique-local
    (`fc00::/7`)처럼 빠뜨리기 쉬운 구멍이 남는다. IPv4-mapped IPv6(`::ffff:127.0.0.1`)도
    stdlib 이 이미 매핑된 v4 성질로 판정한다 — 직접 언랩하는 코드를 뒀다가 뮤테이션에서
    **죽은 코드**로 드러나 지웠다. 그 동작은 `test_web_fetch.py` 가 양방향으로 고정한다
    (매핑된 사설은 차단, 매핑된 공인은 통과).
    """
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def resolved_addresses(host: str) -> list[str]:
    """호스트가 해석되는 모든 IP. 하나라도 사설이면 통째로 거절할 것(아래 `pin`)."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True)
class PinnedUrl:
    """검사를 통과한 URL 과, 접속해도 되는 IP 들."""

    url: str
    """`requests` 가 그대로 쓸 정규형 — 호스트는 ASCII(punycode)로 바뀌어 있다."""
    addresses: tuple[str, ...]
    """이 이름을 해석해 **전부 공인**임을 확인한 IP. 접속은 이 IP 로만 한다."""


def _canonical(raw: str) -> str:
    """`requests` 가 실제로 쓸 URL — 유니코드 호스트는 UTS46 punycode 로 바뀐다.

    `requests` 의 정규화를 그대로 빌린다. 규칙을 따로 흉내 내면 둘이 어긋나는 순간이 곧
    우회로다(위 모듈 설명의 `ß` 사례).
    """
    prepared = requests.PreparedRequest()
    try:
        prepared.prepare_url(raw, None)
    except (requests.RequestException, ValueError, UnicodeError) as e:
        raise UnsafeUrl(REASON_MALFORMED) from e
    if not prepared.url:
        raise UnsafeUrl(REASON_MALFORMED)
    return prepared.url


def pin(raw: str) -> PinnedUrl:
    """열어도 되면 정규화된 URL 과 접속할 IP 를 돌려주고, 아니면 `UnsafeUrl` 을 던진다.

    A 레코드가 여러 개면 **전부** 공인이어야 한다 — 하나라도 사설이면 요청이 어느 쪽으로
    갈지 우리가 고를 수 없다.
    """
    parts = urlsplit(raw)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrl(REASON_SCHEME)
    # `@` 가 있으면 `https://real.com@127.0.0.1/` 처럼 사람 눈과 파서가 다르게 읽는다.
    if "@" in parts.netloc or not parts.hostname:
        raise UnsafeUrl(REASON_MALFORMED)

    url = _canonical(raw)
    parts = urlsplit(url)
    host = parts.hostname
    if "@" in parts.netloc or not host:
        raise UnsafeUrl(REASON_MALFORMED)
    try:
        port = parts.port
    except ValueError as e:  # 포트가 숫자가 아님
        raise UnsafeUrl(REASON_MALFORMED) from e
    if port is not None and port not in _ALLOWED_PORTS:
        raise UnsafeUrl(REASON_PORT)

    try:
        addresses = resolved_addresses(host)
    except (OSError, UnicodeError) as e:
        raise UnsafeUrl(REASON_DNS) from e
    if not addresses:
        raise UnsafeUrl(REASON_DNS)
    if not all(_is_public(ip) for ip in addresses):
        raise UnsafeUrl(REASON_PRIVATE)
    # 순서를 지킨 채 중복만 걷는다 — getaddrinfo 는 같은 IP 를 소켓 종류별로 여러 번 준다.
    return PinnedUrl(url, tuple(dict.fromkeys(addresses)))


def validate_url(raw: str) -> str:
    """`pin` 의 URL 만 — 판정만 필요한 호출자용. 실제 접속은 반드시 `pin` 의 IP 로 한다."""
    return pin(raw).url
