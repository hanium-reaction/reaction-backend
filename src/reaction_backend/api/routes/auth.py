"""Auth — Google OAuth 로그인 + JWT 세션 (S01, api-contract §2).

Issue #16 실구현:
- `/auth/google`  : Google id_token 검증 → users upsert → access(60m) + refresh(14d) 발급
- `/auth/refresh` : refresh → 새 access. 회전 X (MVP). revoke 된 jti · 삭제된 계정(#321) 은 401.
- `/auth/logout`  : refresh 의 jti 를 revoke set 에 등록. 잘못된 토큰이어도 멱등하게 204.
- `/auth/me`      : `CurrentUser` 의존성 (api/deps.py) — Bearer JWT 검증.

Issue #323 — refresh token httpOnly 쿠키 (웹 새로고침 시 재로그인 문제):
- `/auth/google` 이 `refreshToken` 을 응답 본문(그대로 유지, 네이티브·이행기간용)과
  `reaction_refresh` httpOnly 쿠키(`Path=/auth`, `SameSite=Lax`) 로 **둘 다** 내려준다.
- `/auth/refresh`·`/auth/logout` 은 본문에 토큰이 없으면 쿠키로 폴백 — 어느 쪽이든
  하나만 있으면 동작한다.
- 네이티브(capacitor://localhost)는 크로스오리진이라 쿠키를 안 쓰고 지금처럼 본문만
  본다 — 이미 Keystore 로 안전해 `SameSite=None` 의 CSRF 노출을 감수할 이유가 없다.

Issue #324 — 신규 가입 게이트 (기존 사용자 로그인은 완전히 영향받지 않는다):
- `SIGNUPS_ENABLED=false` — 긴급 차단, 재배포 없이 끌 수 있다(`toggle-signups.yml`).
- 가입 인원 상한(`SIGNUP_CAPACITY`, 기본 30) 도달 시 차단.
- 유효·미사용 초대코드 필수(`scripts/manage_invite_codes.py` 로 발급).
- 세 검사 + user 생성 + 코드 소진을 **전역 advisory lock** 으로 감싸 동시 가입 경합을
  막는다(`_signup_lock`) — 30명 상한과 "코드 1회용"을 둘 다 지키려면 "카운트 확인 →
  insert" 사이에 다른 요청이 끼어들지 못해야 한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.auth.jwt import (
    JwtError,
    JwtErrorReason,
    decode_token,
    issue_access_token,
    issue_refresh_token,
)
from reaction_backend.auth.revoke import RevokeStore, get_revoke_store
from reaction_backend.config import get_settings
from reaction_backend.db.models.invite_code import InviteCode
from reaction_backend.db.models.user import User
from reaction_backend.db.session import get_db
from reaction_backend.integrations.google_oauth.verifier import verify_google_id_token
from reaction_backend.repositories.invite_code_repo import (
    InviteCodeRepo,
    get_invite_code_repo,
)
from reaction_backend.repositories.user_repo import GoogleProfile, UserRepo, get_user_repo
from reaction_backend.schemas.auth import (
    AccessToken,
    AuthSession,
    GoogleLoginRequest,
    LogoutRequest,
    RefreshRequest,
    UserProfile,
)
from reaction_backend.schemas.errors import ApiError, ErrorCode

router = APIRouter(prefix="/auth", tags=["auth"])

# pg_advisory_xact_lock 키 — 신규 가입 전용, `user_id × agent` 스킴(orchestrator/_common.py)
# 과 겹치지 않게 그 범위 밖의 고정값을 쓴다. 신규 가입은 순간적인 동작(LLM 호출 없음)이라
# `_common.user_agent_lock` 의 "5s 대기 후 409" 복잡도가 필요 없다 — 그냥 블로킹 대기.
_SIGNUP_LOCK_KEY = -(2**62)

# refresh token httpOnly 쿠키 (#323, FE #246 후속) — `Path=/auth` 로 스코프해
# `/auth/refresh`·`/auth/logout` 호출에만 실린다. 네이티브 앱(capacitor://localhost)은
# 크로스오리진이라 쿠키를 안 쓰고 지금처럼 본문으로만 받는다 — 이미 Keystore 로
# 안전해서 굳이 SameSite=None 의 CSRF 노출을 감수할 이유가 없다(이슈가 명시한 "단순한
# 쪽" 선택). 그래서 로그인 응답은 본문 `refreshToken` 도 그대로 유지한다(이행 기간 겸
# 네이티브용) — 웹은 앞으로 쿠키만 읽어도 되고, `/auth/refresh`·`/auth/logout` 은 본문이
# 없으면 쿠키로 폴백한다.
_REFRESH_COOKIE_NAME = "reaction_refresh"
_REFRESH_COOKIE_PATH = "/auth"


@asynccontextmanager
async def _signup_lock(session: AsyncSession) -> AsyncIterator[None]:
    """가입 인원 상한 + 초대코드 1회성을 지키는 트랜잭션 스코프 lock.

    `pg_advisory_xact_lock` 은 commit/rollback 시 자동 해제 — 수동 unlock 없음
    (`orchestrator/_common.user_agent_lock` 과 같은 이유로 session-level lock 은 쓰지 않는다).
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SIGNUP_LOCK_KEY})
    yield


def _to_profile(user: User) -> UserProfile:
    """User ORM → API UserProfile (ADR-0001 §3.1: API 식별자에 `user_` prefix).

    tone_mode 는 신규 user 에서 None 가능 — 빈 문자열로 fallback (FE 는 기본 톤).
    """
    return UserProfile(
        user_id=f"user_{user.id}",
        email=user.email,
        name=user.name,
        timezone=user.timezone,
        onboarding_state=user.onboarding_state,
        tone_mode=user.tone_mode or "",
    )


async def _validate_new_signup(
    body: GoogleLoginRequest,
    *,
    user_repo: UserRepo,
    invite_repo: InviteCodeRepo,
) -> InviteCode:
    """신규 가입 전 3중 검사 — 순수 검증, 부수효과 없음(코드를 아직 소비하지 않는다).

    순서: 긴급 스위치 → 인원 상한 → 초대코드. "지금 가입을 받고 있는가"가 코드 유효성
    보다 근본적인 조건이라 앞에 둔다. 통과 시 미소진 상태의 코드 행을 반환한다 — 호출자가
    `_signup_lock` 을 쥔 채 user 를 만든 뒤 그 id 로 `mark_used` 를 마저 부른다(코드
    소비는 user 존재가 전제라 여기서 끝낼 수 없다).
    """
    settings = get_settings()
    if not settings.signups_enabled:
        raise ApiError(
            ErrorCode.AUTH_SIGNUPS_DISABLED,
            "지금은 신규 가입을 받지 않고 있어요. 잠시 후 다시 시도해 주세요.",
            http_status=HTTPStatus.FORBIDDEN,
        )

    signed_up = await user_repo.count_signed_up()
    if signed_up >= settings.signup_capacity:
        raise ApiError(
            ErrorCode.AUTH_SIGNUP_CAPACITY_REACHED,
            "지금은 자리가 다 찼어요.",
            http_status=HTTPStatus.FORBIDDEN,
        )

    if not body.invite_code:
        raise ApiError(
            ErrorCode.AUTH_INVALID_INVITE_CODE,
            "초대코드를 입력해 주세요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="inviteCode",
        )
    code_row = await invite_repo.get_by_code(body.invite_code)
    if code_row is None:
        raise ApiError(
            ErrorCode.AUTH_INVALID_INVITE_CODE,
            "초대코드가 올바르지 않아요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="inviteCode",
        )
    if code_row.used_at is not None:
        raise ApiError(
            ErrorCode.AUTH_INVITE_CODE_ALREADY_USED,
            "이미 사용된 초대코드예요.",
            http_status=HTTPStatus.CONFLICT,
            field="inviteCode",
        )
    return code_row


def _set_refresh_cookie(response: Response, token: str, expires_at: datetime) -> None:
    max_age = max(int((expires_at - datetime.now(UTC)).total_seconds()), 0)
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        path=_REFRESH_COOKIE_PATH,
        httponly=True,
        secure=get_settings().app_env != "local",
        samesite="lax",
    )


@router.post("/google")
async def login_with_google(
    body: GoogleLoginRequest,
    response: Response,
    user_repo: Annotated[UserRepo, Depends(get_user_repo)],
    invite_repo: Annotated[InviteCodeRepo, Depends(get_invite_code_repo)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AuthSession:
    """Google id_token 검증 → user upsert → JWT 발급.

    기존 사용자(email 이미 존재)는 게이트를 전혀 거치지 않는다 — lock 도 신규 가입
    판정 이후에만 잡는다(로그인은 이미 30명 안에 있던 사람이라 경합 대상이 아니다).
    """
    claims = verify_google_id_token(body.id_token)
    existing = await user_repo.get_by_email(claims.email)

    if existing is None:
        async with _signup_lock(session):
            # lock 을 잡은 채로 다시 한 번 확인 — lock 획득 대기 중에 동시 요청이 같은
            # email 로 먼저 가입을 끝냈을 수 있다(레이스의 마지막 틈).
            existing = await user_repo.get_by_email(claims.email)
            if existing is None:
                code_row = await _validate_new_signup(
                    body, user_repo=user_repo, invite_repo=invite_repo
                )
                user = await user_repo.upsert_from_google(
                    GoogleProfile(email=claims.email, name=claims.name),
                )
                await invite_repo.mark_used(code_row, used_by_user_id=user.id)
                await session.commit()
            else:
                user = existing
    else:
        user = await user_repo.upsert_from_google(
            GoogleProfile(email=claims.email, name=claims.name),
        )
        await session.commit()

    access = issue_access_token(user.id)
    refresh = issue_refresh_token(user.id)
    _set_refresh_cookie(response, refresh.token, refresh.expires_at)
    return AuthSession(
        access_token=access.token,
        refresh_token=refresh.token,
        user=_to_profile(user),
    )


@router.post("/refresh")
async def refresh_access_token(
    body: RefreshRequest,
    revoke_store: Annotated[RevokeStore, Depends(get_revoke_store)],
    user_repo: Annotated[UserRepo, Depends(get_user_repo)],
    reaction_refresh: Annotated[str | None, Cookie()] = None,
) -> AccessToken:
    """refresh → 새 access. refresh 회전 X (refresh 자체 재발급 안 함).

    토큰 출처는 본문 우선, 없으면 `reaction_refresh` 쿠키(#323) — 웹이 쿠키로 옮겨가도
    네이티브(본문만 보냄)와 과거 웹 클라이언트(둘 다 안 옮긴 상태)가 계속 동작해야 한다.
    둘 다 없으면 애초에 세션이 없는 것이므로 401.

    사용자 존재 확인(#321) — 이전에는 jti revoke 여부만 보고 `decoded.user_id` 로 바로
    access 를 재발급했다. 계정 삭제(`POST /settings/delete-account`)는 개별 jti 를
    모르므로(다중 기기 발급분을 전부 추적하지 않는다) revoke set 에 등록할 수 없다 —
    대신 `UserRepo.get_by_id` 의 `archived_at IS NULL` 필터로 막는다. `get_current_user`
    가 이미 같은 필터로 access token 을 막고 있으니, 여기도 같은 기준을 적용해야
    "삭제된 계정은 refresh 로도 못 살아난다"가 성립한다.
    """
    token = body.refresh_token or reaction_refresh
    if token is None:
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "refresh token 이 없습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        )

    try:
        decoded = decode_token(token, expected_type="refresh")
    except JwtError as e:
        if e.reason is JwtErrorReason.EXPIRED:
            raise ApiError(
                ErrorCode.AUTH_TOKEN_EXPIRED,
                "refresh token 이 만료됐어요. 다시 로그인해 주세요.",
                http_status=HTTPStatus.UNAUTHORIZED,
            ) from e
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "refresh token 이 유효하지 않습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        ) from e

    if revoke_store.is_revoked(decoded.jti):
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "refresh token 이 더 이상 유효하지 않습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        )

    if await user_repo.get_by_id(decoded.user_id) is None:
        raise ApiError(
            ErrorCode.AUTH_INVALID_TOKEN,
            "사용자를 찾을 수 없습니다.",
            http_status=HTTPStatus.UNAUTHORIZED,
        )

    new_access = issue_access_token(decoded.user_id)
    return AccessToken(access_token=new_access.token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest,
    response: Response,
    revoke_store: Annotated[RevokeStore, Depends(get_revoke_store)],
    reaction_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    """refresh 의 jti 를 revoke set 에 등록 + 쿠키 삭제. 잘못된/없는 토큰이어도 멱등 204.

    쿠키는 토큰 유효성과 무관하게 항상 지운다(#323) — 브라우저에 남은 쿠키를 정리하는
    게 목적이라, revoke 대상 jti 를 못 찾는 경우(토큰 없음/깨짐)에도 해야 할 일이다.
    """
    response.delete_cookie(key=_REFRESH_COOKIE_NAME, path=_REFRESH_COOKIE_PATH)

    token = body.refresh_token or reaction_refresh
    if token is None:
        return None
    try:
        decoded = decode_token(token, expected_type="refresh")
    except JwtError:
        return None
    revoke_store.revoke(decoded.jti, decoded.expires_at)
    return None


@router.get("/me")
async def get_current_user_profile(user: CurrentUser) -> UserProfile:
    """현재 사용자 + onboardingState 반환 (`Depends(get_current_user)`)."""
    return _to_profile(user)
