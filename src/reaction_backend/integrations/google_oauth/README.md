# `integrations/google_oauth/` — Google OAuth

Issue #1 follow-up 또는 별도 이슈에서 구현.

책임:
- Google id_token 검증 (issuer, audience, expiry)
- 신규/기존 사용자 분기 → users 테이블 INSERT/UPDATE (last_active_at 갱신)
- 자체 JWT (access + refresh) 발급
- refresh token 회전 (rotation) 정책
