"""Refresh token revoke store — Issue #16 MVP.

logout 시 refresh token 의 `jti` 를 등록한다. 동일 `jti` 가 등록되어 있으면 refresh 거부.

저장소: in-memory + 만료시각 기준 자동 정리.
- 다중 프로세스 / 재기동에 취약 (Issue #3 의 `IdempotencyStore` 와 동일 한계) — 매 배포마다
  systemd 재시작으로 비워져, 로그아웃한 refresh token(14일)이 다시 통한다.
- access token 은 여기서 막지 않는다(`get_current_user` 는 revoke 를 안 본다) — 로그아웃 뒤에도
  만료(24시간)까지 유효하다.
- 후속(마이그레이션 필요, AGENTS §8 합의 대상): DB 테이블(`refresh_token_revocations`) 또는
  `users.tokens_valid_after` 로 교체해 재기동에도 유지하고 access 도 함께 막는다.
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
