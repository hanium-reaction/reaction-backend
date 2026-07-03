# CI/CD

> PR 검증 (CI) + main 머지 시 staging 자동 적용 (CD: DB 마이그레이션 + 앱 배포).
>
> ⚠️ **staging 대상 정정**: 아래 §1~§6 은 원래 Supabase staging 전제로 작성됐다. 실제로는
> nxtcloud AWS 샌드박스 제약(액세스 키 발급 불가·리소스는 리전/보안그룹 고정)으로 **EC2 +
> RDS 조합이 staging** 이 됐다. `migrate.yml`(Supabase 전제, `STAGING_DATABASE_URL` secret
> 미설정 시 즉시 fail)은 현재 **미사용(dormant)** 이고, DB 마이그레이션은 `deploy.yml`
> 내부에서 EC2 위에 상시 구동 중인 self-hosted runner 가 RDS 에 직접 적용한다(§3.5).
> Supabase staging 을 실제로 쓰게 되면 migrate.yml 을 재활성화(secret 등록)하면 된다.

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

## 3.5 CD (앱 배포) — `.github/workflows/deploy.yml`

main 브랜치 push 시 자동 실행. **DB 마이그레이션 + 앱 재기동**을 한 잡에서 처리한다.

### 왜 GitHub 호스팅 러너 + SSH 가 아니라 self-hosted runner 인가
- nxtcloud 샌드박스 규칙상 EC2 보안그룹 22번(SSH)은 **My IP 로만** 열려 있어야 한다.
  GitHub 호스팅 러너는 IP 가 계속 바뀌어 SSH 로 배포하려면 22번을 `0.0.0.0/0` 으로
  열어야 하는데, 이는 nxtcloud 가이드가 명시적으로 지양하는 설정이다.
- 대신 **EC2 위에 GitHub Actions self-hosted runner 를 systemd 서비스로 상시 구동**한다.
  러너가 GitHub 에 **outbound** 로 폴링해 잡을 받아오므로 **인바운드 포트를 전혀 추가로
  열 필요가 없다.** SSH 개인키를 GitHub Secrets 에 둘 필요도 없다(가장 큰 장점).
- 등록: repo → Settings → Actions → Runners → New self-hosted runner (Linux x64) →
  `./config.sh` 실행 후 `./svc.sh install ubuntu && ./svc.sh start` 로 서비스화.
  라벨: `self-hosted, ec2, reaction-backend` (workflow 의 `runs-on` 과 매칭).
- **`pull_request` 트리거는 절대 쓰지 않는다** — fork PR 이 self-hosted runner 에서 임의
  코드를 실행할 수 있는 위험 때문 (`push: branches: [main]` 만 트리거, main 직접 push
  는 이미 AGENTS.md §2 로 금지되어 있어 PR 리뷰를 거친 코드만 배포된다).

### 잡 흐름
1. `actions/checkout` — 러너의 임시 워크스페이스(`_work/`)에 최신 main 체크아웃
2. `rsync -a --delete --exclude='.env' --exclude='.venv' --exclude='.git'` 로 상시
   앱 디렉터리(`/home/ubuntu/reaction-backend`)에 코드만 반영. `.env`(RDS URL·Gemini
   키 등, git 미추적) 와 `.venv`(증분 재사용) 는 보호된다.
3. `uv sync --no-dev --python 3.12` — 의존성 증분 갱신
4. `alembic current` (before) → `alembic upgrade head` → `alembic current` (after) —
   RDS 에 직접 적용 (러너가 이미 그 VPC/네트워크 안에 있어 추가 접근 허용 불필요)
5. `sudo systemctl restart reaction-backend` (ubuntu 유저 NOPASSWD sudo)
6. `/health` 폴링(최대 30s) — `status:"ok"` 아니면 job **실패** + 최근 로그 출력

### 안전 장치
- `concurrency: deploy-ec2` — 동시 배포 1개만 (같은 앱 디렉터리 경합 방지)
- `.venv`/`.env` 는 rsync exclude 로 절대 삭제·덮어쓰기 되지 않음
- 헬스체크 실패 시 job 자체가 빨간불 — 자동 rollback 은 아직 없음(§7 후속)

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

## 7. 후속

| 영역 | 상태 |
|---|---|
| ~~백엔드 앱 자동 배포~~ | ✅ `deploy.yml` (EC2 self-hosted runner) — 완료 |
| prod 환경 추가 (`PROD_DATABASE_URL`) | 베타 출시 시점 |
| PR Preview (Supabase Branching) | Supabase staging 전환 + Pro plan 도입 시 |
| Slack 알림 (CD 성공/실패) | 후속 |
| 자동 rollback (헬스체크 실패 시) | 후속 — 현재는 job 실패로만 표시, 이전 리비전 자동 복귀 없음 |
| self-hosted runner 이중화 (EC2 재부팅/장애 시 배포 불가) | 후속 — 현재 단일 러너 |
