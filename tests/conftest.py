"""공통 pytest fixture.

Issue #16 이후 모든 도메인 라우터(health 제외)에 `Depends(get_current_user)` 적용.
Issue #17 이후 4 도메인(time_policies / fixed_schedules / notifications) 실 DB 의존.

테스트 격리를 위해:
- `client`         : 인증 override + 4 도메인 fake repo + fake session. 일반 도메인 테스트용.
- `unauthed_client`: 인증 override 없음 (DB 의존성만 fake) — 401 분기 검증.
- `auth_client`    : 인증 override 없음 + user_repo 만 fake — `/auth/*` 흐름 (실 JWT 발급).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, time
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from reaction_backend.api.deps import get_current_user
from reaction_backend.auth.revoke import get_revoke_store
from reaction_backend.db.models.fixed_schedule import FixedSchedule
from reaction_backend.db.models.notification_setting import NotificationSetting
from reaction_backend.db.models.time_policy import TimePolicy
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db
from reaction_backend.main import create_app
from reaction_backend.repositories.fixed_schedule_repo import get_fixed_schedule_repo
from reaction_backend.repositories.notification_repo import get_notification_repo
from reaction_backend.repositories.time_policy_repo import get_time_policy_repo
from reaction_backend.repositories.user_repo import GoogleProfile, get_user_repo

DEMO_USER_UUID = UUID("11111111-1111-4111-8111-111111111111")


def make_demo_user(*, onboarding_state: str = "ACTIVE") -> User:
    """ORM 상태 없이 만든 demo User 인스턴스.

    `onboarding_state` 는 default ACTIVE — 상태 전이 테스트는 인자로 override.
    """
    u = User()
    u.id = DEMO_USER_UUID
    u.email = "demo@reaction.local"
    u.name = "김민수"
    u.timezone = "Asia/Seoul"
    u.onboarding_state = onboarding_state
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
    """테스트 환경 settings — JWT_SECRET / AUTH_STUB_MODE 자동 적용."""
    monkeypatch.setenv(
        "JWT_SECRET",
        "test-jwt-secret-which-is-long-enough-for-hs256-aaaaaaaa",
    )
    monkeypatch.setenv("AUTH_STUB_MODE", "true")
    from reaction_backend.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ───── 가짜 세션 + 결과 ─────


class _FakeResult:
    """SQLAlchemy `Result` 의 부분 stub — `.all()` / `.scalars()` / `.scalar_one*` 만."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> _FakeResult:
        return self

    def scalar_one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        return self._rows[0]


class _FakeSession:
    """라우터가 호출하는 session 인터페이스의 stub.

    repo 들이 모두 dependency_override 로 fake 로 교체되므로 session 은 직접 query 수행 X.
    `time_policies` prefill 의 inline select 만 fake — 항상 빈 결과 (interview 답 없음).
    """

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, stmt: Any) -> _FakeResult:  # noqa: ARG002
        # prefill 의 inline select 만 도달 — interview 답이 없는 default 시나리오 반환.
        return _FakeResult([])

    async def flush(self) -> None:
        return None

    async def refresh(self, obj: Any) -> None:  # noqa: ARG002
        return None

    def add(self, obj: Any) -> None:  # noqa: ARG002
        return None


# ───── 가짜 repository ─────


class FakeTimePolicyRepo:
    def __init__(self) -> None:
        self._items: dict[UUID, TimePolicy] = {}

    async def list_active(self, user_id: UUID) -> list[TimePolicy]:
        return [p for p in self._items.values() if p.user_id == user_id and p.archived_at is None]

    async def get_by_id(self, user_id: UUID, policy_id: UUID) -> TimePolicy | None:
        p = self._items.get(policy_id)
        if p is None or p.user_id != user_id or p.archived_at is not None:
            return None
        return p

    async def create(self, user_id: UUID, policy_type: str, payload: dict[str, Any]) -> TimePolicy:
        p = TimePolicy()
        p.id = uuid4()
        p.user_id = user_id
        p.policy_type = policy_type
        p.payload = payload
        p.is_active = True
        p.archived_at = None
        self._items[p.id] = p
        return p

    async def update(
        self,
        policy: TimePolicy,
        *,
        payload: dict[str, Any] | None = None,
        is_active: bool | None = None,
    ) -> TimePolicy:
        if payload is not None:
            policy.payload = payload
        if is_active is not None:
            policy.is_active = is_active
        return policy

    async def soft_delete(self, policy: TimePolicy) -> None:
        policy.archived_at = datetime.now(UTC)
        policy.is_active = False

    async def count_active(self, user_id: UUID) -> int:
        return len(await self.list_active(user_id))


class FakeFixedScheduleRepo:
    def __init__(self) -> None:
        self._items: dict[UUID, FixedSchedule] = {}

    async def list_active(self, user_id: UUID) -> list[FixedSchedule]:
        items = [s for s in self._items.values() if s.user_id == user_id and s.archived_at is None]
        return sorted(items, key=lambda s: s.start_time)

    async def get_by_id(self, user_id: UUID, schedule_id: UUID) -> FixedSchedule | None:
        s = self._items.get(schedule_id)
        if s is None or s.user_id != user_id or s.archived_at is not None:
            return None
        return s

    async def create(
        self,
        user_id: UUID,
        title: str,
        days_of_week: list[str],
        start_time: time,
        end_time: time,
    ) -> FixedSchedule:
        s = FixedSchedule()
        s.id = uuid4()
        s.user_id = user_id
        s.title = title
        s.days_of_week = days_of_week
        s.start_time = start_time
        s.end_time = end_time
        s.archived_at = None
        self._items[s.id] = s
        return s

    async def update(
        self,
        schedule: FixedSchedule,
        *,
        title: str | None = None,
        days_of_week: list[str] | None = None,
        start_time: time | None = None,
        end_time: time | None = None,
    ) -> FixedSchedule:
        if title is not None:
            schedule.title = title
        if days_of_week is not None:
            schedule.days_of_week = days_of_week
        if start_time is not None:
            schedule.start_time = start_time
        if end_time is not None:
            schedule.end_time = end_time
        return schedule

    async def soft_delete(self, schedule: FixedSchedule) -> None:
        schedule.archived_at = datetime.now(UTC)

    async def count_active(self, user_id: UUID) -> int:
        return len(await self.list_active(user_id))


class FakeNotificationRepo:
    def __init__(self) -> None:
        self._items: dict[UUID, NotificationSetting] = {}

    async def get_by_user(self, user_id: UUID) -> NotificationSetting | None:
        return self._items.get(user_id)

    async def get_or_create(self, user_id: UUID) -> NotificationSetting:
        existing = self._items.get(user_id)
        if existing is not None:
            return existing
        s = NotificationSetting()
        s.id = uuid4()
        s.user_id = user_id
        s.morning_brief_time = time(8, 0)
        s.evening_reflection_time = time(21, 0)
        s.pre_card_enabled = False
        s.push_subscription = None
        self._items[user_id] = s
        return s

    async def update(
        self,
        setting: NotificationSetting,
        *,
        morning_brief_time: time | None = None,
        evening_reflection_time: time | None = None,
        pre_card_enabled: bool | None = None,
    ) -> NotificationSetting:
        if morning_brief_time is not None:
            setting.morning_brief_time = morning_brief_time
        if evening_reflection_time is not None:
            setting.evening_reflection_time = evening_reflection_time
        if pre_card_enabled is not None:
            setting.pre_card_enabled = pre_card_enabled
        return setting


class FakeUserRepo:
    """in-memory UserRepo. /auth 흐름 + 상태 전이 헬퍼 둘 다 지원."""

    def __init__(self) -> None:
        self._by_email: dict[str, User] = {}
        self._by_id: dict[UUID, User] = {}

    def register(self, user: User) -> None:
        """테스트가 미리 user 를 등록할 때 사용 (`client` fixture 가 demo user 자동 등록)."""
        self._by_email[user.email] = user
        self._by_id[user.id] = user

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

    async def advance_onboarding(
        self,
        user: User,
        expected_from: str | tuple[str, ...],
        to: str,
    ) -> bool:
        expected = (expected_from,) if isinstance(expected_from, str) else expected_from
        if user.onboarding_state in expected:
            user.onboarding_state = to
            return True
        return False


# ───── 일반 도메인 client (인증 + 모든 fake) ─────


@pytest.fixture
def demo_user_orm() -> User:
    """demo user ORM 인스턴스 — 테스트가 `onboarding_state` 직접 변경 가능."""
    return make_demo_user()


@pytest.fixture
def fake_time_policy_repo() -> FakeTimePolicyRepo:
    return FakeTimePolicyRepo()


@pytest.fixture
def fake_fixed_schedule_repo() -> FakeFixedScheduleRepo:
    return FakeFixedScheduleRepo()


@pytest.fixture
def fake_notification_repo() -> FakeNotificationRepo:
    return FakeNotificationRepo()


@pytest.fixture
def fake_user_repo() -> FakeUserRepo:
    return FakeUserRepo()


@pytest.fixture
def client(
    demo_user_orm: User,
    fake_time_policy_repo: FakeTimePolicyRepo,
    fake_fixed_schedule_repo: FakeFixedScheduleRepo,
    fake_notification_repo: FakeNotificationRepo,
    fake_user_repo: FakeUserRepo,
) -> Iterator[TestClient]:
    """기본 client — 인증된 demo user + 4 도메인 fake repo + fake session."""
    _reset_process_singletons()
    fake_user_repo.register(demo_user_orm)
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[get_current_user] = lambda: demo_user_orm
    app.dependency_overrides[get_db] = _fake_session_gen
    app.dependency_overrides[get_time_policy_repo] = lambda: fake_time_policy_repo
    app.dependency_overrides[get_fixed_schedule_repo] = lambda: fake_fixed_schedule_repo
    app.dependency_overrides[get_notification_repo] = lambda: fake_notification_repo
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unauthed_client() -> Iterator[TestClient]:
    """override 없는 fresh client + fake session — 401 분기 / Authorization 헤더 테스트용."""
    _reset_process_singletons()
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[get_db] = _fake_session_gen
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(fake_user_repo: FakeUserRepo) -> Iterator[TestClient]:
    """`/auth/*` 테스트 — repo/session 만 override, 인증은 실제 JWT 흐름."""
    _reset_process_singletons()
    app = create_app()

    async def _fake_session_gen() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[get_db] = _fake_session_gen
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    with TestClient(app) as c:
        yield c


def issue_helper_token(
    *,
    user_id: UUID,
    token_type: str,
    expired: bool = False,
) -> str:
    """테스트 보조 — JWT 직접 발급 (만료 강제 포함)."""
    from datetime import timedelta

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


__all__ = [
    "DEMO_USER_UUID",
    "FakeFixedScheduleRepo",
    "FakeNotificationRepo",
    "FakeTimePolicyRepo",
    "FakeUserRepo",
    "auth_client",
    "client",
    "demo_user_orm",
    "fake_fixed_schedule_repo",
    "fake_notification_repo",
    "fake_time_policy_repo",
    "fake_user_repo",
    "issue_helper_token",
    "make_demo_user",
    "unauthed_client",
]
