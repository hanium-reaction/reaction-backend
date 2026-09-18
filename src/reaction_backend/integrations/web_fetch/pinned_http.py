"""검사를 통과한 IP 로만 접속하는 `requests` 세션 (inbox-1).

`url_guard.pin` 이 이름을 해석해 전부 공인 IP 임을 확인해도, URL 문자열만 `requests` 에
넘기면 urllib3 가 그 이름을 **다시** 해석해 접속한다. 두 조회 사이에 답이 바뀌면(TTL 0
DNS rebinding) 검사는 공인 IP 를 보고 접속은 사설 IP(EC2 메타데이터 등)로 간다.

그래서 소켓을 여는 한 지점(`HTTPConnection._new_conn`)만 바꿔 `pin` 이 돌려준 IP 로 직접
다이얼한다. URL·Host 헤더·TLS SNI·인증서 검사는 원래 이름 그대로라 사이트 입장에선 평범한
요청과 같다 — IP 를 URL 에 박고 Host 헤더를 손으로 넣는 방식은 HTTPS 인증서 검사가
IP 기준으로 바뀌어 쓰지 않았다.
"""

from __future__ import annotations

import socket
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.poolmanager import PoolManager
from urllib3.util.connection import create_connection


class _Dialer:
    """정해진 IP 들로만 TCP 연결을 연다 — 이름 해석은 하지 않는다."""

    def __init__(self, addresses: tuple[str, ...]) -> None:
        self._addresses = addresses

    def dial(self, conn: HTTPConnection) -> socket.socket:
        """urllib3 의 `_new_conn` 과 같은 규약 — 실패는 urllib3 예외로 바꿔 던진다.

        그래야 `requests` 가 평소처럼 `ConnectTimeout`/`ConnectionError` 로 옮겨 주고,
        호출자의 사유 분류(timeout/unavailable)가 그대로 유지된다. IP 가 여럿이면 urllib3 와
        같이 순서대로 시도한다(IPv6 경로가 없는 EC2 에서 AAAA 가 먼저 와도 v4 로 넘어간다).
        """
        error: ConnectTimeoutError | NewConnectionError | None = None
        for ip in self._addresses:
            try:
                return create_connection(
                    (ip, conn.port),
                    conn.timeout,
                    source_address=conn.source_address,
                    socket_options=conn.socket_options,
                )
            except TimeoutError:
                error = ConnectTimeoutError(
                    conn, f"Connection to {conn.host} timed out. (connect timeout={conn.timeout})"
                )
            except OSError as e:
                error = NewConnectionError(conn, f"Failed to establish a new connection: {e}")
        raise error or NewConnectionError(conn, "no address to connect to")


class _PinnedHTTPConnection(HTTPConnection):
    def __init__(self, *args: Any, dialer: _Dialer, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._dialer = dialer

    def _new_conn(self) -> socket.socket:
        return self._dialer.dial(self)


class _PinnedHTTPSConnection(HTTPSConnection):
    """다이얼만 고정 — TLS 는 부모가 `self.host`(원래 이름)로 SNI·인증서 검사를 한다."""

    def __init__(self, *args: Any, dialer: _Dialer, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._dialer = dialer

    def _new_conn(self) -> socket.socket:
        return self._dialer.dial(self)


class _PinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedPoolManager(PoolManager):
    def __init__(self, dialer: _Dialer, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._dialer = dialer
        self.pool_classes_by_scheme = {
            "http": _PinnedHTTPConnectionPool,
            "https": _PinnedHTTPSConnectionPool,
        }

    def _new_pool(
        self,
        scheme: str,
        host: str,
        port: int,
        request_context: dict[str, Any] | None = None,
    ) -> HTTPConnectionPool:
        # 풀 키(`connection_pool_kw`)에 넣으면 urllib3 의 PoolKey 가 모르는 키라 터진다 —
        # 풀을 만든 뒤 연결 생성 인자에만 얹는다.
        pool = super()._new_pool(scheme, host, port, request_context)
        pool.conn_kw["dialer"] = self._dialer
        return pool


class _PinnedAdapter(HTTPAdapter):
    def __init__(self, dialer: _Dialer) -> None:
        self._dialer = dialer  # 부모 __init__ 이 init_poolmanager 를 부르므로 먼저 둔다
        super().__init__()

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block
        self.poolmanager = _PinnedPoolManager(
            self._dialer, num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs
        )


def session(addresses: tuple[str, ...]) -> requests.Session:
    """`addresses` 로만 접속하는 1회용 세션. `with` 로 닫는다(홉마다 새로 만든다).

    프록시 환경변수는 끈다(`trust_env=False`) — 프록시를 타면 이름 해석을 프록시가
    **다시** 하므로 고정이 무의미해진다. `.netrc` 자격증명이 붙는 것도 같이 막힌다.
    """
    s = requests.Session()
    s.trust_env = False
    adapter = _PinnedAdapter(_Dialer(addresses))
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s
