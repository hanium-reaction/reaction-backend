"""공통 pytest fixture.

Issue #16 이후 모든 도메인 라우터(health 제외)에 `Depends(get_current_user)` 적용.
- `client`         : 인증 override 적용 (demo user). 일반 도메인 라우터 테스트에 사용.
- `unauthed_client`: override 없는 fresh client. 인증 동작 자체 검증용.
- `auth_client`    : repo/session 만 override, 인증은 실제 JWT 흐름 — `/auth/*` 테스트 전용.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.api.deps import get_current_user
from reaction_backend.auth.revoke import get_revoke_store
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db
from reaction_backend.main import create_app
from reaction_backend.repositories.user_repo import GoogleProfile, UserRepo, get_user_repo

DEMO_USER_UUID = UUID("11111111-1111-4111-8111-111111111111")


def make_demo_user() -> User:
    """ORM 상태 없이 만든 demo User 인스턴스 (mock 응답 일관성용).

    `api/mock/demo.py` 의 DEMO_USER 와 핵심 필드(email/onboarding_state)를 맞춘다.
    """
    u = User()
    u.id = DEMO_USER_UUID
    u.email = "demo@reaction.local"
    u.name = "김민수"
    u.timezone = "Asia/Seoul"
    u.onboarding_state = "ACTIVE"
    u.tone_mode = "gentle"
    return u


def _reset_process_singletons() -> None:
    """프로세스 단위 in-memory store 들을 테스트 간 격리."""
    store = get_revoke_store()
    clear = getattr(store, "clear", None)
    if callable(clear):
        clear()


@pytest.fixture(autouse=True)
def _ensure_test_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """테스트 환경 settings — JWT_SECRET / AUTH_STUB_MODE 자동 적용.

    `get_settings` 의 lru_cache 를 매 테스트마다 clear → env 가 신선하게 반영.
    """
    monkeypatch.setenv(
        "JWT_SECRET",
        "test-jwt-secret-which-is-long-enough-for-hs256-aaaaaaaa",
    )
    monkeypatch.setenv("AUTH_STUB_MODE", "true")
    from reaction_backend.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """매 테스트마다 새 앱 + 기본 인증된 demo user (dependency override).

    Idempotency in-memory 저장소 + revoke set 도 테스트마다 초기화된다.
    """
    _reset_process_singletons()
    app = create_app()
    app.dependency_overrides[get_current_user] = make_demo_user
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unauthed_client() -> Iterator[TestClient]:
    """override 없는 fresh client — 401 분기 / Authorization 헤더 테스트용.

    `get_db` 만 fake 로 묶는다 — 401 던지기 전 의존성 chain 에서 실제 DB session 이 열리는 걸 막기 위함.
    """
    _reset_process_singletons()
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[get_db] = _fake_session_gen
    with TestClient(app) as test_client:
        yield test_client


# ───── /auth/* 테스트 전용 fixture (실제 JWT 흐름) ─────


class _FakeSession:
    """`/auth/google` 의 `await session.commit()` 만 충족하는 가짜 세션."""

    async def commit(self) -> None:  # noqa: D401
        return None

    async def rollback(self) -> None:
        return None


class FakeUserRepo:
    """in-memory User store. 실제 `UserRepo` 와 동일한 메서드 시그니처."""

    def __init__(self) -> None:
        self._by_email: dict[str, User] = {}
        self._by_id: dict[UUID, User] = {}

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._by_id.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        return self._by_email.get(email)

    async def upsert_from_google(self, profile: GoogleProfile) -> User:
        existing = self._by_email.get(profile.email)
        if existing is not None:
            existing.name = profile.name
            return existing
        u = User()
        u.id = uuid4()
        u.email = profile.email
        u.name = profile.name
        u.timezone = "Asia/Seoul"
        u.onboarding_state = "WELCOME"
        u.tone_mode = None
        self._by_email[profile.email] = u
        self._by_id[u.id] = u
        return u


@pytest.fixture
def fake_user_repo() -> FakeUserRepo:
    return FakeUserRepo()


@pytest.fixture
def auth_client(fake_user_repo: FakeUserRepo) -> Iterator[TestClient]:
    """`/auth/*` 테스트 — repo/session 만 override, 인증 override 는 하지 않는다.

    `/auth/google` 응답으로 받은 토큰을 그대로 `/auth/me` 검증에 사용한다.
    """
    _reset_process_singletons()
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    # FastAPI 는 `Annotated[UserRepo, Depends(get_user_repo)]` 를 override 가능.
    # 타입 시그니처가 다른 fake 라도 dependency_overrides 는 callable 만 본다.
    overrides: dict[Callable[..., Any], Callable[..., Any] | Callable[..., Awaitable[Any]]] = {
        get_db: _fake_session_gen,
        get_user_repo: lambda: fake_user_repo,
    }
    app.dependency_overrides.update(overrides)
    with TestClient(app) as c:
        yield c


def issue_helper_token(
    *,
    user_id: UUID,
    token_type: str,
    expired: bool = False,
) -> str:
    """테스트 보조 — JWT 직접 발급 (만료 강제 포함).

    `auth.jwt._issue` 를 거치지 않고 만료된 토큰을 만들기 위해 pyjwt 직접 사용.
    """
    from datetime import UTC, datetime, timedelta

    import jwt as pyjwt

    from reaction_backend.config import get_settings

    cfg = get_settings()
    now = datetime.now(UTC)
    if expired:
        iat = now - timedelta(hours=2)
        exp = now - timedelta(hours=1)
    else:
        iat = now
        exp = now + timedelta(hours=1)
    payload = {
        "sub": str(user_id),
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "type": token_type,
        "jti": "test-jti",
    }
    return pyjwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


# `UserRepo` re-export 로 mypy/IDE 가 fixture 사용처에서 잡을 수 있게.
__all__ = [
    "DEMO_USER_UUID",
    "FakeUserRepo",
    "UserRepo",
    "auth_client",
    "client",
    "fake_user_repo",
    "issue_helper_token",
    "make_demo_user",
    "unauthed_client",
]
