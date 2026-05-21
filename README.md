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

### Docker Compose

```bash
docker compose up --build
# → http://localhost:8000/health
```

소스 (`src/`) 가 마운트되어 hot reload 됨.

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
| Docker 빌드 | `docker compose build` |

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

## 후속 이슈와의 연결

| 이슈 | 채워질 영역 |
| --- | --- |
| #1 follow-up | Auth / Onboarding / Interview 핵심 (`agents/interview_agent.py`, `orchestrator/interview.py`) |
| #2 DB Schema v0.7 | `db/`, `repositories/`, alembic |
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
