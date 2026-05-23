"""컬럼 양방향 암호화 (AES-256-GCM).

Issue #5 §3. `*_encrypted` 접미사 컬럼에 대한 단일 진실 소스.

키 소스:
- 환경변수 `COLUMN_ENCRYPTION_KEY` (urlsafe base64, 32-byte 디코드).
- `get_cipher()` 가 lru_cache 로 단일 인스턴스 보존.

호출자 (3개 즉시 적용 가능):
- OAuth 토큰         → `encrypt_oauth_token()` / `decrypt_oauth_token()`
                       (`calendar_connections.{access,refresh}_token_encrypted`)
- 회고 메모          → `encrypt_memo()` / `decrypt_memo()`
                       (`execution_failure_tags.memo_encrypted`)
- LLM 입출력 요약     → `encrypt_llm_payload()` / `decrypt_llm_payload()`
                       (`llm_runs.{input,output}_summary_encrypted`)

저장 포맷:
- urlsafe base64(nonce(12B) || ciphertext_with_tag)
- 익명화 cron 은 평문 '[anonymized]' 으로 덮어쓰기 — decrypt 는 그 경우
  `EncryptionError` 가 아닌 원문 그대로 반환.
"""

from __future__ import annotations

import base64
import os
import secrets
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from reaction_backend.config import get_settings

# AES-GCM 권장 nonce 길이 (96-bit).
_NONCE_LEN = 12

# 익명화 cron 이 남기는 sentinel — decrypt 가 그대로 반환.
ANONYMIZED_SENTINEL = "[anonymized]"


class EncryptionError(RuntimeError):
    """암호화/복호화 실패. 키 누락·키 길이 불일치·태그 검증 실패 등."""


def _load_key() -> bytes:
    """`COLUMN_ENCRYPTION_KEY` 환경변수에서 32-byte AES 키 로드.

    형식: urlsafe base64 (`Fernet.generate_key()` 호환 / `secrets.token_urlsafe(32)` 등).
    누락이면 `EncryptionError` — fail fast.
    """
    raw = get_settings().column_encryption_key or os.environ.get("COLUMN_ENCRYPTION_KEY", "")
    if not raw:
        raise EncryptionError(
            "COLUMN_ENCRYPTION_KEY is not set. "
            "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    try:
        key = base64.urlsafe_b64decode(_pad_b64(raw))
    except (ValueError, TypeError) as exc:
        raise EncryptionError("COLUMN_ENCRYPTION_KEY must be urlsafe base64.") from exc
    if len(key) != 32:
        raise EncryptionError(
            f"COLUMN_ENCRYPTION_KEY must decode to exactly 32 bytes (got {len(key)}). Use AES-256."
        )
    return key


def _pad_b64(value: str) -> str:
    """urlsafe base64 패딩 보정. 입력이 padding 생략 형태여도 받는다."""
    padding = (-len(value)) % 4
    return value + ("=" * padding)


@lru_cache(maxsize=1)
def get_cipher() -> AESGCM:
    """단일 AESGCM 인스턴스. 키 로테이션 시 `get_cipher.cache_clear()` 호출."""
    return AESGCM(_load_key())


def encrypt(plaintext: str, *, associated_data: bytes | None = None) -> str:
    """문자열을 AES-256-GCM 으로 암호화 → urlsafe base64 문자열.

    `associated_data` 는 무결성에만 사용 (암호문에 포함되지 않음). 호출자별로
    다른 컨텍스트(예: `b"oauth"`, `b"memo"`, `b"llm_input"`) 를 지정하면
    교차 사용을 막을 수 있다.
    """
    if plaintext == ANONYMIZED_SENTINEL:
        return plaintext

    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = get_cipher().encrypt(nonce, plaintext.encode("utf-8"), associated_data)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt(token: str, *, associated_data: bytes | None = None) -> str:
    """`encrypt()` 의 역함수.

    - sentinel(`[anonymized]`) 은 그대로 반환 — 익명화 cron 호환.
    - tag/nonce 손상 시 `EncryptionError`.
    """
    if token == ANONYMIZED_SENTINEL:
        return token

    try:
        blob = base64.urlsafe_b64decode(_pad_b64(token))
    except (ValueError, TypeError) as exc:
        raise EncryptionError("Encrypted payload is not valid urlsafe base64.") from exc

    if len(blob) <= _NONCE_LEN:
        raise EncryptionError("Encrypted payload too short.")

    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    try:
        plaintext = get_cipher().decrypt(nonce, ciphertext, associated_data)
    except InvalidTag as exc:
        raise EncryptionError("AES-GCM tag verification failed.") from exc
    return plaintext.decode("utf-8")


# ── 도메인별 헬퍼: associated_data 로 교차 사용 차단 ────────────────────────

_AD_OAUTH = b"reaction:oauth-token"
_AD_MEMO = b"reaction:failure-memo"
_AD_LLM = b"reaction:llm-payload"


def encrypt_oauth_token(plaintext: str) -> str:
    """`calendar_connections.{access,refresh}_token_encrypted` 용."""
    return encrypt(plaintext, associated_data=_AD_OAUTH)


def decrypt_oauth_token(token: str) -> str:
    return decrypt(token, associated_data=_AD_OAUTH)


def encrypt_memo(plaintext: str) -> str:
    """`execution_failure_tags.memo_encrypted` 용."""
    return encrypt(plaintext, associated_data=_AD_MEMO)


def decrypt_memo(token: str) -> str:
    return decrypt(token, associated_data=_AD_MEMO)


def encrypt_llm_payload(plaintext: str) -> str:
    """`llm_runs.{input,output}_summary_encrypted` 용."""
    return encrypt(plaintext, associated_data=_AD_LLM)


def decrypt_llm_payload(token: str) -> str:
    return decrypt(token, associated_data=_AD_LLM)


def generate_key() -> str:
    """개발용 키 생성 헬퍼. `python -m reaction_backend.safety.encryption` 에서도 사용."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


if __name__ == "__main__":  # pragma: no cover - manual key generation helper
    print(generate_key())
