# reaction-backend

한이음 프로젝트 **re:action** 의 백엔드 — 청년 대학생을 위한 AI 실행 회복 코치.

> 단순 RESTful CRUD 백엔드가 아니라 **AI 에이전트 + 4계층 메모리** 시스템.
> 도메인 흐름과 데이터 모델은 `Reaction_DevBaseline_v1.0` + `Reaction_DB_설계서_v0.7.1` 을 진실 소스로 한다.

| 문서 | 위치 |
| --- | --- |
| API 계약 (16 도메인) | [`docs/api-contract.md`](docs/api-contract.md) |
| 아키텍처 (Orchestrator/Agent/Tool/Memory) | [`docs/architecture.md`](docs/architecture.md) |
| 에이전트/Claude 작업 규칙 | [`AGENTS.md`](AGENTS.md) |
| 프론트엔드 레포 | [hanium-reaction/reaction-frontend](https://github.com/hanium-reaction/reaction-frontend) |

---

## 요구사항

| 도구 | 버전 |
| --- | --- |
| Python | 3.12 |
| [uv](https://docs.astral.sh/uv/) | 0.9.x |
| Docker / Docker Compose | 26+ (선택) |

---

## 빠른 시작

### 로컬 (uv)

```bash
uv sync
cp .env.example .env   # 선택 — 기본값만으로도 동작
uv run uvicorn reaction_backend.main:app --reload
# → http://localhost:8000/health
# → http://localhost:8000/docs  (Swagger UI)
```

### Docker Compose (backend + 로컬 postgres)

```bash
docker compose up --build
# → http://localhost:8000/health   (DB ping 포함)
# → postgres on localhost:5432 (user/db: reaction, pass: reaction)
```

소스 (`src/`) 가 마운트되어 hot reload 됨.
`.env` 에 `DATABASE_URL` 이 있으면 Supabase 등 외부 DB 우선, 없으면 docker compose 내부 postgres 사용.

---

## 자주 쓰는 명령어

| 목적 | 명령 |
| --- | --- |
| 의존성 설치 | `uv sync` |
| 새 의존성 | `uv add <pkg>` / `uv add --dev <pkg>` |
| 개발 서버 | `uv run uvicorn reaction_backend.main:app --reload` |
| 린트 | `uv run ruff check .` |
| 포맷 | `uv run ruff format .` |
| 포맷 검사 | `uv run ruff format --check .` |
| 타입 검사 | `uv run mypy src` |
| 테스트 | `uv run pytest -v` |
| **Alembic 현재 버전** | `uv run alembic current` |
| **새 마이그레이션** | `uv run alembic revision --autogenerate -m "..."` |
| **마이그레이션 적용** | `uv run alembic upgrade head` |
| **한 단계 되돌리기** | `uv run alembic downgrade -1` |
| Docker 빌드 | `docker compose build` |
| Docker (postgres 포함) | `docker compose up` |

---

## 폴더 구조

```
reaction-backend/
├── .github/workflows/ci.yml         # PR 검증 (lint·typecheck·test·docker build)
├── docs/
│   ├── api-contract.md              # 16 도메인 API 계약 v0.3
│   └── architecture.md              # Orchestrator/Agent/Tool/Memory
├── src/reaction_backend/
│   ├── main.py                      # FastAPI 앱 + 16 라우터 include
│   ├── config.py                    # 환경설정 (pydantic-settings)
│   │
│   ├── api/routes/                  # 16 도메인 라우터 (health만 구현, 나머지 placeholder 501)
│   │   ├── health.py                # ✅ 구현됨
│   │   ├── auth.py / onboarding.py / interview.py
│   │   ├── time_policies.py / goals.py / habits.py
│   │   ├── planning.py / calendar.py / today.py
│   │   ├── reflection.py / recovery.py / review.py
│   │   ├── policy.py / notifications.py / settings.py
│   │
│   ├── schemas/                     # 공통 + 도메인 스키마
│   │   └── common.py                # ErrorResponse, HealthResponse, KST helper
│   │
│   ├── domain/                      # 순수 도메인 모델 (entity/VO) — 후속
│   ├── db/                          # SQLAlchemy + 마이그레이션 — Issue #2
│   ├── repositories/                # Repository 패턴 — Issue #2
│   │
│   ├── orchestrator/                # 3 Orchestrator (goal_structuring/recovery/interview)
│   ├── agents/                      # 9 Worker Agent
│   ├── llm/                         # Gemini Tool Executor (circuit breaker + fallback)
│   ├── prompts/                     # Prompt Registry — Issue #5
│   ├── safety/                      # 금지어 필터, PII 마스킹 — Issue #5
│   │
│   ├── integrations/
│   │   ├── google_oauth/            # id_token 검증, JWT 발급
│   │   └── google_calendar/         # freebusy + events.insert (idempotent)
│   │
│   ├── scheduler/                   # 8 cron 작업
│   ├── memory/                      # 4 계층 메모리 추상화
│   └── observability/               # llm_runs · metrics · audit
│
├── tests/
├── .env.example
├── Dockerfile                       # multi-stage: builder / dev / runtime
├── docker-compose.yml
└── pyproject.toml
```

각 폴더의 `README.md` 가 그 레이어의 책임 / 후속 모듈 / 규약을 설명한다.

---

## 데이터베이스 (Issue #2)

**진실 소스:** `Reaction_DB_설계서_v0.7.1` + `Reaction_DB_시나리오별_상세분석.md`.
**스택:** Supabase PostgreSQL (호스팅된 표준 PG) + SQLAlchemy(async) + asyncpg + Alembic.
**전략:** Supabase 부가 서비스(Auth/Storage/Realtime/Edge)는 미사용 — vendor-neutral 코드 유지. AWS 이전 시 DB는 `pg_dump | psql` 로 매끄럽게.

### 첫 셋업

1. Supabase 프로젝트 생성 → **Session pooler URL** 복사
2. `.env` 만들고 `DATABASE_URL=postgresql://postgres.<ref>:<pw>@aws-X-ap-northeast-2.pooler.supabase.com:5432/postgres`
   (코드가 자동으로 `postgresql+asyncpg://` 로 변환)
3. `uv run alembic current` 로 연결 확인 (PR 2-A 시점)

### 로컬 Postgres (Supabase 없이)

`.env` 에서 `DATABASE_URL` 비우거나 빼면 `docker compose` 의 내부 postgres 사용:
```
DATABASE_URL=postgresql://reaction:reaction@localhost:5432/reaction
```

### 모델/마이그레이션 위치

- ORM 모델: `src/reaction_backend/db/models/` (Issue #2 후속 PR에서 채워짐)
- 마이그레이션: `alembic/versions/`
- 모델 변경 시: 모델 수정 → `alembic revision --autogenerate -m "..."` → 생성된 파일 리뷰 → `alembic upgrade head`

### DB reset / seed

| 목적 | 명령 |
| --- | --- |
| 전체 drop + 재생성 + 마스터 seed | `uv run python -m scripts.db_reset` |
| Demo user 1명 + 부속 데이터 | `uv run python -m scripts.db_seed_demo` |

`db_reset` 은 `app_env=prod` 에서는 거부됨. `yes` 입력 확인 단계 있음.
**마스터 데이터** (13 failure tags + 9 recovery strategies) 는 `alembic upgrade head` 만으로 자동 seed 됨 (마이그레이션 `a96678e9ffe5`).

### ERD ↔ 코드 매핑

[`docs/erd-diff.md`](docs/erd-diff.md) — 29개 도메인 테이블 / 29개 ENUM / 마스터 데이터 / 의도적 누락 모두 한눈에.

### CI/CD

- **CI** (`.github/workflows/ci.yml`): PR 마다 lint / typecheck / test / docker build + **alembic check (drift 감지) + downgrade smoke**
- **CD** (`.github/workflows/migrate.yml`): main 머지 시 **staging Supabase 에 alembic 자동 적용** (GitHub Secrets `STAGING_DATABASE_URL` 필요)
- 가이드: [`docs/cicd.md`](docs/cicd.md) — Supabase staging 생성 + Secrets 등록 + manual approval 옵션

---

## 후속 이슈와의 연결

| 이슈 | 채워질 영역 |
| --- | --- |
| #1 follow-up | Auth / Onboarding / Interview 핵심 (`agents/interview_agent.py`, `orchestrator/interview.py`) |
| **#2-A** | `db/{session,base}.py`, `alembic/`, docker compose postgres, `/health` DB ping |
| **#2-B** | 사용자/온보딩 8 모델 (users, interview, behavioral, interaction, notification, calendar, fixed_schedule) |
| **#2-C** | 계획 9 모델 (time_policies, goals, goal_nodes, habits, habit_instances, inbox_items, action_items, scheduled_blocks, dependency_links) |
| **#2-D** | 실행/회복 7 모델 (execution_events, interruption_events, context_snapshots, failure_reason_tags, execution_failure_tags, recovery_strategy_catalog, recovery_attempts) |
| **#2-E** | 집계/시스템 5 모델 (period_summaries, daily_briefs, policy_snapshots, llm_runs, idempotency_keys) + 마스터 seed + reset 스크립트 + ERD diff |
| #3 Backend API Contract v0 | 도메인 라우터 실제 구현 |
| #5 LLM Infrastructure | `llm/`, `prompts/`, `safety/`, `agents/` 본 구현 |
| #6 Deep Interview + Analysis Confirm | 인터뷰 흐름 통합 (`orchestrator/interview.py` 완성 + S03 commit 트랜잭션) |

---

## 기여 가이드

1. `main` 에 직접 push 금지. 반드시 PR.
2. 새 의존성은 `uv add` 만 사용 (`pip install` X).
3. 새 endpoint 추가 시 [`docs/api-contract.md`](docs/api-contract.md) 같은 PR에 포함.
4. CI (lint · typecheck · test · docker build) 가 초록불일 때만 머지.

AI 에이전트(Claude Code, Codex 등)는 [`AGENTS.md`](AGENTS.md) 를 먼저 읽어 주세요.
