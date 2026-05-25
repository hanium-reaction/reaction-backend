"""Refresh token revoke store — Issue #16 MVP.

logout 시 refresh token 의 `jti` 를 등록한다. 동일 `jti` 가 등록되어 있으면 refresh 거부.

저장소: in-memory + 만료시각 기준 자동 정리.
- 다중 프로세스 / 재기동에 취약 (Issue #3 의 `IdempotencyStore` 와 동일 한계).
- 후속: DB 테이블(`refresh_token_revocations`)로 교체 예정.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


class RevokeStore(Protocol):
    """logout 시 등록되는 refresh jti 저장소 인터페이스."""

    def revoke(self, jti: str, expires_at: datetime) -> None: ...

    def is_revoked(self, jti: str) -> bool: ...


@dataclass
class InMemoryRevokeStore:
    """Thread-safe in-memory store. 만료된 `jti` 는 조회 시점에 정리."""

    _store: dict[str, datetime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def revoke(self, jti: str, expires_at: datetime) -> None:
        with self._lock:
            self._store[jti] = expires_at

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            self._cleanup_locked()
            return jti in self._store

    def _cleanup_locked(self) -> None:
        now = datetime.now(UTC)
        expired = [k for k, v in self._store.items() if v <= now]
        for k in expired:
            self._store.pop(k, None)

    def clear(self) -> None:
        """테스트용. 운영에서는 호출 X."""
        with self._lock:
            self._store.clear()


_default_store: InMemoryRevokeStore = InMemoryRevokeStore()


def get_revoke_store() -> RevokeStore:
    """단일 프로세스 default store. FastAPI dependency 로도 사용 가능."""
    return _default_store
