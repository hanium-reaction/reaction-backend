# `integrations/google_oauth/` — Google ID token 검증 경계

이 패키지는 클라이언트가 보낸 Google `id_token`을 검증해 백엔드가 신뢰할 수 있는 최소 claim으로 바꾸는 역할만 맡는다. 사용자 upsert와 re:action 자체 JWT 발급·폐기는 [`../../api/routes/auth.py`](../../api/routes/auth.py), [`../../auth/`](../../auth/)와 repository의 책임이다.

## 현재 구성

- [`verifier.py`](verifier.py): production/staging의 Google token 검증, local demo stub, `GoogleClaims` 정규화를 구현한다.
- [`__init__.py`](__init__.py): 패키지 표식이다.

## 핵심 계약과 불변조건

1. `verify_google_id_token(token)`의 성공 결과는 immutable `GoogleClaims(sub, email, name)`이다.
2. `AUTH_STUB_MODE=false`에서는 `GOOGLE_OAUTH_CLIENT_ID`가 반드시 필요하다. 없으면 인증 실패로 숨기지 않고 서버 설정 오류 `RuntimeError`로 fail fast한다.
3. 실제 token 검증은 `google.oauth2.id_token.verify_oauth2_token`에 client ID를 audience로 전달한다. 이 라이브러리가 서명, issuer, audience, expiry를 검증한다.
4. token 형식·서명·만료·audience 오류 또는 필수 `sub`/`email` claim 누락은 `401 AUTH_INVALID_ID_TOKEN`으로 정규화한다.
5. `name`은 `name`, `given_name`, 빈 문자열 순으로 정규화하되, `sub`와 `email`은 비어 있으면 성공시키지 않는다.
6. local stub은 명시적으로 켠 환경에서만 동작한다. `demo:<id>`는 소문자 영숫자·`_`·`-`만 남기고 32자로 제한해 브라우저별 격리 계정을 만든다. 유효 slug가 없거나 다른 token이면 고정 demo 계정을 사용한다.
7. production code가 stub token을 해석하거나 Google 검증을 우회하는 별도 경로를 만들지 않는다.

## 대표 흐름

```text
POST /auth/google { idToken }
  → verify_google_id_token()
      ├─ local + AUTH_STUB_MODE: 고정/브라우저별 demo claims
      └─ 그 외: google-auth 서명·iss·aud·exp 검증
  → auth route가 email/name으로 users upsert
  → auth.jwt가 access token + refresh token 발급
  → AuthSession 반환
```

refresh와 logout은 Google token을 다시 다루지 않는다. `/auth/refresh`는 re:action refresh JWT를 검증해 access token만 새로 발급하고, `/auth/logout`은 refresh JWT의 jti를 revoke store에 기록한다.

## 검증

- [`test_auth.py`](../../../../tests/test_auth.py): stub login, invalid/expired token 오류, user upsert, access/refresh, logout·revoke, `/auth/me` 경계

전체 회귀는 저장소 루트에서 `uv run pytest`로 실행한다.

## 확장 방법

- 추가 Google claim이 필요하면 `GoogleClaims`에 최소 필드만 추가하고 누락·타입 오류를 verifier에서 정규화한다. raw token payload를 route로 흘리지 않는다.
- authorization code flow나 다른 provider를 추가할 때는 검증/교환 adapter를 별도 모듈로 두고, 내부 `AuthSession` 발급은 기존 auth 경계에 유지한다.
- stub 시나리오를 늘릴 때는 `AUTH_STUB_MODE` 밖에서 절대 활성화되지 않는 테스트와 slug 정규화·계정 격리 회귀를 함께 추가한다.
- 네트워크·인증서 캐시 정책을 바꿀 때는 `google-auth` 검증을 직접 재구현하지 말고 공식 verifier 경계를 유지한다.

## 알려진 제약

- 이 패키지는 OAuth authorization code 교환, consent URL 생성, access/refresh token 저장을 구현하지 않는다. 현재 입력은 이미 클라이언트가 획득한 `id_token`이다.
- Google `sub`는 검증 결과에 포함되지만 현재 [`../../api/routes/auth.py`](../../api/routes/auth.py)의 user upsert는 email/name을 전달한다.
- re:action refresh token은 현재 회전하지 않는다. access 재발급과 revoke 정책은 Google OAuth가 아니라 내부 auth 모듈의 책임이다.
- stub mode에서 `demo:<id>`가 아닌 문자열은 고정 demo 계정으로 매핑되므로 shared test 환경에서는 브라우저별 `demo:<id>` 사용이 필요하다.
