"""90일 비활성 자동 익명화 cron (#24, DevBaseline §1.4) — 룰 레벨 검증.

job 함수에 Fake repo 주입 (`test_proposed_goal_expiry.py` 와 같은 모양).
실 SQL(WHERE 절)은 `test_anonymize_inactive_sql.py` 가 실 Postgres 로 따로 고정한다.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from reaction_backend.db.models.user import User
from reaction_backend.safety.encryption import ANONYMIZED_SENTINEL
from reaction_backend.scheduler.anonymize_inactive import (
    INACTIVE_ANONYMIZE_TTL_DAYS,
    inactive_anonymize_before,
    run_anonymize_inactive_users,
)
from reaction_backend.schemas.common import now_kst
from tests.conftest import FakePrivacyRepo, FakeUserRepo, _FakeSession


def _seed(repo: FakeUserRepo, *, last_active_days_ago: float, anonymized: bool = False) -> User:
    user = User()
    user.id = uuid.uuid4()
    user.email = f"{user.id}@test.local"
    user.name = "떠난 사용자"
    user.timezone = "Asia/Seoul"
    user.onboarding_state = "ACTIVE"
    user.tone_mode = None
    user.is_anonymized = anonymized
    user.last_active_at = now_kst() - timedelta(days=last_active_days_ago)
    user.anonymized_at = now_kst() if anonymized else None
    repo.register(user)
    return user


async def test_anonymizes_only_users_past_the_ttl() -> None:
    """90일 넘긴 사용자만 익명화. TTL 이내는 그대로."""
    repo = FakeUserRepo()
    stale = _seed(repo, last_active_days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 1)
    fresh = _seed(repo, last_active_days_ago=1)

    result = await run_anonymize_inactive_users(
        _FakeSession(), user_repo=repo, privacy_repo=FakePrivacyRepo(), now=now_kst()
    )

    assert result.anonymized == 1
    assert result.failed == 0
    assert stale.is_anonymized is True
    assert stale.anonymized_at is not None
    assert fresh.is_anonymized is False
    assert fresh.anonymized_at is None


async def test_masks_name_but_never_email() -> None:
    """email 은 로그인 1차 키 — 마스킹하면 익명화가 아니라 사실상 계정 삭제가 된다.

    계정 삭제(#321 `POST /settings/delete-account`)와 갈리는 지점이라 여기서 못 박는다.
    """
    repo = FakeUserRepo()
    user = _seed(repo, last_active_days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 10)
    original_email = user.email

    await run_anonymize_inactive_users(
        _FakeSession(), user_repo=repo, privacy_repo=FakePrivacyRepo(), now=now_kst()
    )

    assert user.name == ANONYMIZED_SENTINEL
    assert user.email == original_email
    assert user.archived_at is None  # soft delete 아님 — 계정은 살아 있다


async def test_calls_privacy_repo_masking() -> None:
    """`*_encrypted` 마스킹은 수동 익명화와 **같은 함수**(PrivacyRepo.anonymize_user)로 한다."""
    repo = FakeUserRepo()
    privacy = FakePrivacyRepo()
    user = _seed(repo, last_active_days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 1)

    await run_anonymize_inactive_users(
        _FakeSession(), user_repo=repo, privacy_repo=privacy, now=now_kst()
    )

    assert privacy.anonymized_user == user.id


async def test_idempotent_skips_already_anonymized() -> None:
    """이미 익명화된 사용자는 재처리 대상이 아니다 — `anonymized_at IS NULL` 가드."""
    repo = FakeUserRepo()
    already = _seed(repo, last_active_days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 50, anonymized=True)
    before = already.anonymized_at

    result = await run_anonymize_inactive_users(
        _FakeSession(), user_repo=repo, privacy_repo=FakePrivacyRepo(), now=now_kst()
    )

    assert result.total == 0
    assert result.anonymized == 0
    assert already.anonymized_at == before  # 시각도 안 덮어쓴다


async def test_boundary_exactly_at_ttl_is_not_anonymized() -> None:
    """경계값 — 정확히 90일째는 아직 아니다(`<` 이지 `<=` 가 아님)."""
    repo = FakeUserRepo()
    now = now_kst()
    user = User()
    user.id = uuid.uuid4()
    user.email = f"{user.id}@test.local"
    user.name = "경계"
    user.timezone = "Asia/Seoul"
    user.onboarding_state = "ACTIVE"
    user.tone_mode = None
    user.is_anonymized = False
    user.anonymized_at = None
    user.last_active_at = inactive_anonymize_before(now)  # 정확히 경계
    repo.register(user)

    result = await run_anonymize_inactive_users(
        _FakeSession(), user_repo=repo, privacy_repo=FakePrivacyRepo(), now=now
    )

    assert result.anonymized == 0
    assert user.is_anonymized is False


async def test_one_failure_does_not_stop_the_batch() -> None:
    """한 사용자에서 터져도 나머지는 계속 처리된다 — 사용자 단위 commit + except 격리.

    이게 없으면 사고 사용자 1명 때문에 그날 배치 전체가 롤백되고, 다음 04:00 까지
    아무도 익명화되지 않는다.
    """
    repo = FakeUserRepo()
    doomed = _seed(repo, last_active_days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 1)
    _seed(repo, last_active_days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 2)
    _seed(repo, last_active_days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 3)

    class _ExplodingPrivacyRepo(FakePrivacyRepo):
        async def anonymize_user(self, user_id: uuid.UUID) -> int:
            if user_id == doomed.id:
                raise RuntimeError("DB 커넥션이 끊겼다고 치자")
            return await super().anonymize_user(user_id)

    result = await run_anonymize_inactive_users(
        _FakeSession(), user_repo=repo, privacy_repo=_ExplodingPrivacyRepo(), now=now_kst()
    )

    assert result.total == 3
    assert result.anonymized == 2
    assert result.failed == 1
    assert doomed.is_anonymized is False


async def test_onboarding_state_is_not_a_filter() -> None:
    """온보딩 중 이탈한 계정도 대상 — 오히려 90일 뒤에 남아 있으면 안 되는 데이터다.

    `list_active()`(ACTIVE 만)의 필터를 재사용하면 이들이 영원히 남는다.
    """
    repo = FakeUserRepo()
    user = _seed(repo, last_active_days_ago=INACTIVE_ANONYMIZE_TTL_DAYS + 1)
    user.onboarding_state = "ONBOARDING_INTERVIEW"

    result = await run_anonymize_inactive_users(
        _FakeSession(), user_repo=repo, privacy_repo=FakePrivacyRepo(), now=now_kst()
    )

    assert result.anonymized == 1


def test_ttl_is_locked_at_90_days() -> None:
    """DevBaseline §1.4 잠금 결정 — 임의로 못 바꾼다(AGENTS §1)."""
    assert INACTIVE_ANONYMIZE_TTL_DAYS == 90


def test_boundary_helper_matches_ttl() -> None:
    now = now_kst()
    assert inactive_anonymize_before(now) == now - timedelta(days=90)


@pytest.mark.parametrize("days_ago", [91, 120, 365])
async def test_various_stale_ages_all_anonymized(days_ago: int) -> None:
    repo = FakeUserRepo()
    _seed(repo, last_active_days_ago=days_ago)
    result = await run_anonymize_inactive_users(
        _FakeSession(), user_repo=repo, privacy_repo=FakePrivacyRepo(), now=now_kst()
    )
    assert result.anonymized == 1
