# 배포 런북 — reaction-backend (Issue #24)

> 호스팅 결정: **백엔드 = Render (Docker)** · DB = Supabase(prod) · 프론트 = Vercel.
> IaC: [`render.yaml`](../render.yaml) Blueprint. 시연일 안정성을 위한 운영 절차.

## 0. 구조

```
[Android Chrome / 브라우저]
        │ https
[Vercel — reaction-frontend]  ──API──▶  [Render — reaction-backend (Docker)]
                                              │ asyncpg
                                        [Supabase Postgres (prod, ap-northeast-2)]
```

## 1. ⚠️ 먼저 발급/준비해야 할 것 (PM 수동 — 코드로 못 함)

| 항목 | 발급처 | 메모 |
| --- | --- | --- |
| **Supabase prod 프로젝트 + `DATABASE_URL`** | supabase.com | Session pooler URL (ap-northeast-2). staging 과 **별도 프로젝트** 권장 |
| **`GEMINI_API_KEY`** | Google AI Studio | 무료 티어로 시작, 6월 2주차 유료 전환 예정 |
| **`GOOGLE_OAUTH_CLIENT_ID`** | Google Cloud Console | FE 와 동일 client. **승인된 JS 원본**에 Vercel·Render 도메인 추가 |
| **`JWT_SECRET`** | 로컬 생성 | `python -c "import secrets; print(secrets.token_hex(32))"` |
| **`COLUMN_ENCRYPTION_KEY`** | 로컬 생성 | `python -m reaction_backend.safety.encryption` (staging 과 **다른 키**, 분실 시 복호화 불가) |
| **(FE) VAPID 키** | 로컬 생성 | #25 Web Push 용. `npx web-push generate-vapid-keys` |

> 시크릿은 git 에 절대 커밋 금지. Render/Vercel 대시보드 또는 비밀번호 관리자에만 저장.

## 2. 백엔드 배포 (Render Blueprint)

1. Render 대시보드 → **New → Blueprint** → `hanium-reaction/reaction-backend` 연결 → `render.yaml` 자동 감지.
2. 생성된 `reaction-backend` 서비스 → **Environment** 탭에서 `sync:false` 시크릿 6개 입력:
   `DATABASE_URL` · `JWT_SECRET` · `GEMINI_API_KEY` · `COLUMN_ENCRYPTION_KEY` · `GOOGLE_OAUTH_CLIENT_ID` · `CORS_ALLOW_ORIGINS`
   - `CORS_ALLOW_ORIGINS` 는 JSON 배열: `["https://reaction-frontend.vercel.app"]`
3. **Manual Deploy** → 빌드(Docker runtime stage) → `/health` 200 확인.
4. 발급된 URL(`https://reaction-backend-xxxx.onrender.com`)을 FE 환경변수로 전달(아래 4).

## 3. DB 마이그레이션

Render 는 마이그레이션을 **돌리지 않는다** (runtime 이미지에 `alembic/` 미포함, `src` 만 복사).
→ 기존 CD [`.github/workflows/migrate.yml`](../.github/workflows/migrate.yml)가 Supabase 에 `alembic upgrade head` 적용.
prod DB 를 이 워크플로의 대상(Secret)으로 추가하면 main 머지 시 자동 반영. (최초 1회는 수동 `alembic upgrade head` 도 가능 — 로컬에서 `DATABASE_URL=<prod>` 로.)

## 4. 프론트(Vercel) 연동

- Vercel 프로젝트 환경변수: `VITE_API_BASE_URL = <Render URL>`, `VITE_GOOGLE_CLIENT_ID = <client_id>` (공개값).
- 재배포 후 Android Chrome 에서 로그인→온보딩 흐름 확인.

## 5. ⚠️ 콜드스타트 (시연 주의)

Render **free** 플랜은 15분 무요청 시 슬립 → 첫 요청 30~60초 지연. **시연일에는:**
- (권장) **starter 플랜**($7/mo)으로 일시 업그레이드 → 슬립 없음, 또는
- 시연 직전 워밍업 요청 + 외부 uptime 핑(예: cron-job.org 가 `/health` 5분 간격).

## 6. 남은 #24 작업 (후속)

- [ ] **cron 트리거 진입점** — `scheduler/*.py` job 함수(Morning Brief·interruption·익명화)는 구현됐으나 실행 진입(`python -m reaction_backend.scheduler.run <job>` 같은 CLI)이 없음. 추가 후 `render.yaml` 의 cron 블록 활성화.
- [ ] **90일 비활성 익명화 cron** — `users.last_active_at < now()-90d AND anonymized_at IS NULL` → 마스킹. 컬럼은 존재(`last_active_at`·`anonymized_at`), job 로직 + repo 만 추가하면 됨.
- [ ] **데모 시드 확장** — `scripts/db_seed_demo.py` 에 시연 시나리오 데이터(인터뷰 완료 사용자 1명 등).
- [ ] **prod env 분리 검증** — staging/prod Supabase·시크릿 분리, `AUTH_STUB_MODE=false` 확인.
