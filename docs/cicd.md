# CI/CD — Supabase 마이그레이션 자동화

> PR 검증 (CI) + main 머지 시 staging Supabase 자동 적용 (CD).
> 호스팅 자동 배포는 별도 PR (후속).

## 1. 환경 구성

| 환경 | 용도 | Supabase 프로젝트 | DATABASE_URL 위치 |
|---|---|---|---|
| **local** | 사용자 개인 개발 | dev (이미 있음) | 사용자 `.env` (committed X) |
| **CI 검증용** | PR 마다 격리된 임시 PG | (없음 — 컨테이너) | GitHub Actions `services.postgres` |
| **staging** | main 머지 자동 적용 | **새로 생성 필요** | GitHub Secrets `STAGING_DATABASE_URL` |
| prod | (P2) | 미생성 | — |

> 💡 dev 와 staging 분리 이유: 사용자가 로컬에서 자유롭게 실험할 수 있어야 하는데, staging 에 자동 적용되면 사용자 로컬 실험이 staging 을 망가뜨림. **dev 는 사용자 전용, staging 은 CD 자동 적용 전용.**

## 2. CI (PR 검증) — `.github/workflows/ci.yml`

PR 마다 자동 실행되는 job:

| Job | 내용 |
|---|---|
| `lint-test` | ruff check / format / mypy strict / pytest |
| `migration` | **임시 PG 컨테이너** 띄우고 → `alembic upgrade head` → `alembic check` (drift 감지) → `alembic downgrade base` (rollback smoke) |
| `docker` | Dockerfile multi-stage runtime 빌드 |

### `migration` job 의 핵심
- **격리된 PG**: GitHub Actions `services.postgres:17-alpine`. 실제 Supabase 와 무관 → 안전
- **drift 감지**: 모델만 바꾸고 마이그레이션 안 만든 PR 은 `alembic check` 에서 fail
- **dry-run SQL preview**: `alembic upgrade head --sql` 결과를 artifact 로 첨부 (7일 보관)
- **downgrade smoke**: 모든 마이그레이션이 base 까지 되돌릴 수 있는지 검증 (broken downgrade 방지)

## 3. CD (자동 적용) — `.github/workflows/migrate.yml`

main 브랜치 push 시 (= PR 머지 시) 자동 실행.

### 트리거 조건
- `main` 브랜치에 push (PR 머지 시 자동)
- 변경 path 가 다음 중 하나:
  - `alembic/**` (마이그레이션 파일)
  - `src/reaction_backend/db/**` (모델 변경)
  - `pyproject.toml` / `uv.lock` (의존성)
  - `.github/workflows/migrate.yml` (CD 자체)
- 또는 **수동 실행** (workflow_dispatch) — target revision 지정 가능 (예: 특정 revision 으로 downgrade)

### 안전 장치
- `environment: staging` — Settings → Environments 에서 **manual approval** 추가 가능
- `STAGING_DATABASE_URL` secret 없으면 즉시 fail
- `alembic check` 한 번 더 검증 후 적용
- `concurrency` — 동시 실행 1개만 (DB 충돌 방지)
- 실패 시 즉시 종료 (다음 step 안 함)

## 4. 첫 셋업 — 사용자 작업 (3단계)

### 4.1 Supabase staging 프로젝트 생성

1. https://supabase.com dashboard → 같은 organization (`hanium-reaction`) 안에서 **New project**
2. Name: `reaction-staging`
3. Database Password: **dev 와 다른 새 비밀번호**
4. Region: Northeast Asia (Seoul)
5. Plan: Free
6. 생성 후 → `Connect` 버튼 → **Session pooler** → URI 복사
   - 형태: `postgresql://postgres.<ref>:<pw>@aws-X-ap-northeast-2.pooler.supabase.com:5432/postgres`

### 4.2 GitHub Secrets 등록

1. https://github.com/hanium-reaction/reaction-backend/settings/secrets/actions
2. **New repository secret**
3. Name: `STAGING_DATABASE_URL`
4. Secret: 위에서 복사한 URI (비밀번호 포함)
5. Add secret

### 4.3 (선택) GitHub Environments 에서 manual approval

자동 적용이 불안하면 첫 적용은 사용자 승인 후 실행하도록:

1. https://github.com/hanium-reaction/reaction-backend/settings/environments
2. **New environment** → Name: `staging`
3. **Required reviewers** 체크 → 본인 또는 팀원 지정
4. Save

이러면 main 머지 후 CD 가 시작되기 전 **사람의 [Approve] 클릭 1번 필요**. 익숙해지면 disable.

## 5. 작동 흐름

```
[사용자 로컬]                    [GitHub Actions]                [Supabase]

1. 모델 수정                                                     dev (사용자)
2. alembic revision --autogenerate
3. uv run alembic upgrade head   ──────────────────────────────► (직접 적용, 사용자 책임)
4. git commit + push

5. PR 열기   ──────────────────► CI 실행
                                    └─ lint/test/migration/docker
                                       (격리된 PG, 실제 staging X)

6. PR 리뷰 + 머지 (main)    ───► migrate.yml 실행
                                    └─ STAGING_DATABASE_URL
                                       alembic upgrade head      staging
                                                              ──► (자동 적용)
```

## 6. 트러블슈팅

### CD가 실행되지 않음
- 변경된 파일이 trigger path (`alembic/**`, `src/reaction_backend/db/**` 등) 에 해당하는지 확인
- 그 외 변경만 있는 PR 은 CD 가 의도적으로 skip — `workflow_dispatch` 로 수동 실행 가능

### `STAGING_DATABASE_URL secret is not set`
- Settings → Secrets → Actions 에서 secret 등록 확인
- secret 이름 정확히 `STAGING_DATABASE_URL` (대문자, 언더스코어)

### Drift 감지로 fail
- 로컬에서 모델만 수정하고 `alembic revision --autogenerate -m "..."` 안 한 경우
- 마이그레이션 파일 생성 후 commit/push 다시

### Downgrade smoke 실패
- 마이그레이션의 `downgrade()` 가 깨짐 (`op.drop_table` 누락, enum drop 안 함 등)
- 마이그레이션 파일의 downgrade 부분 수정

### staging 적용 실패 (DB 에러)
- 마이그레이션이 staging 상태와 호환 안 됨 (예: 이미 있는 컬럼 add)
- GitHub Actions 로그에서 `alembic current` 결과 확인
- 필요시 `workflow_dispatch` 로 특정 revision 으로 downgrade

## 7. 후속 (이번 PR 범위 외)

| 영역 | 어디서 |
|---|---|
| 백엔드 앱 자동 배포 (Render/Fly.io/Cloud Run) | 별도 PR |
| prod 환경 추가 (`PROD_DATABASE_URL`) | 베타 출시 시점 |
| PR Preview (Supabase Branching) | Pro plan 도입 시 |
| Slack 알림 (CD 성공/실패) | 후속 |
| 자동 rollback (헬스체크 실패 시) | 후속 |
