# Re:Action Backend

> 계획 실패를 기록하고, 사용자가 선택한 회복 행동으로 다시 실행에 연결하는 AI 코칭 백엔드

이 저장소는 한이음 프로젝트 **Re:Action**의 FastAPI 백엔드입니다. 인증·목표·계획·실행·회고·회복·주간 리뷰 API, PostgreSQL 영속화, Gemini 호출 게이트, Web Push, 정기 작업을 제공합니다.

Re:Action은 일반 CRUD 서비스가 아니라 다음 세 원칙을 중심으로 설계되었습니다.

1. AI 출력은 초안이며 사용자의 승인 전에는 적용하지 않습니다.
2. 실패한 원본 실행 기록을 덮어쓰지 않고 회복 시도를 별도로 기록합니다.
3. 외부 호출과 자동 작업은 비용·보안·멱등성 가드를 통과해야 합니다.

- 프론트엔드: [hanium-reaction/reaction-frontend](https://github.com/hanium-reaction/reaction-frontend)
- API 계약: [`docs/api-contract.md`](docs/api-contract.md)
- 아키텍처: [`docs/architecture.md`](docs/architecture.md)
- 작업 규칙: [`AGENTS.md`](AGENTS.md)

## 제품 흐름

```text
Google/stub 인증
      ↓
딥 인터뷰 → 목표·프로필 기억 → 첫 계획 초안 → 사용자 승인
                                      ↓
                              오늘의 행동·집중 기록
                                      ↓
                         완료 / 부분 완료 / 잘 안됨
                                      ↓
                    실패 사유 → 회복안 초안 → 사용자 결정
                                      ↓
                               재계획·주간 리뷰
```

## 구현 범위

| 영역 | 상태 | 주요 범위 |
| --- | --- | --- |
| 인증 | 구현 | Google ID token 검증, 자체 JWT, refresh/logout, 로컬 stub |
| 인터뷰·온보딩 | 구현 | 세션, 슬롯 답변, 다음 질문, 완료, 목표·프로필 반영 |
| 목표·습관·Inbox | 구현 | CRUD, 목표 계층, 습관 인스턴스, 자료/한 걸음 채택 |
| 계획 | 구현 | 마일스톤, 첫 계획, 블록 편집, 승인·폐기·재계획 |
| 오늘 실행 | 구현 | 아젠다, 상세, 시작, 일시정지, 재개, 체크인·취소 |
| 회고·회복 | 구현 | 실패 태그, 일괄 회고, 회복안, 결정, 회복 기반 재계획 |
| 주간 리뷰 | 구현 | 주간 집계, 습관 페널티 제안·수락 |
| 설정·개인정보 | 구현 | 톤·프로필, 동의 이력, 2단계 확인 익명화 |
| 알림 | 구현 | 구독 관리, VAPID 공개키, Web Push 발송 게이트 |
| Google Calendar | stub/준비 중 | connect/disconnect 501, freebusy·preview·insert는 고정 응답 stub |
| 스케줄러 | 구현 | APScheduler 기반 9개 등록 작업, 기본 비활성 |

운영 사용자 수, 완료율 개선, 회복률 향상 같은 성과 수치는 코드만으로 입증할 수 없습니다. 보고서와 발표에는 별도의 실험·운영 근거가 있을 때만 사용하세요.

## 아키텍처

```text
FastAPI routes + Pydantic schemas
              ↓
Orchestrators / domain rules
              ↓
Repositories + SQLAlchemy models
        ↙        ↓         ↘
 Gemini       PostgreSQL   Integrations
 Tool gate                OAuth/Web Push/Web fetch
              ↓
      Scheduler + Safety gates
```

### API 레이어

`src/reaction_backend/main.py`가 18개 라우트 모듈을 등록합니다. 성공 응답은 도메인 객체를 직접 반환하고, 오류만 `ErrorResponse` 형태를 사용합니다. 시간은 내부적으로 UTC에 저장하고 API에서는 KST 오프셋이 포함된 ISO 8601을 사용합니다.

API 변경 시 [`docs/api-contract.md`](docs/api-contract.md)와 변경 이력을 같은 PR에서 갱신해야 합니다.

### 오케스트레이션

현재 실제 오케스트레이션 코드는 `src/reaction_backend/orchestrator/`에 있습니다.

- 인터뷰 질문·슬롯 수집과 목표 materialize
- 마일스톤·첫 계획 생성과 검토
- 일정 배치와 블록 편집
- 회복 제안·재계획
- 프로필 메모리와 주간 리뷰
- Inbox 자료 해석과 행동 채택

`agents/`는 독립 Worker Agent의 경계만 남아 있고 현재 실행 로직의 중심은 오케스트레이터입니다. README와 발표에서 `agents/`에 구현이 존재한다고 과장하지 마세요.

### LLM 게이트

모든 Gemini 호출은 `src/reaction_backend/llm/`을 통과합니다.

- 태스크별 모델 선택
- timeout·재시도·fallback
- 구조화 출력
- 토큰·비용 기록
- 일일 사용자 예산
- 금지어 후처리

라우터나 오케스트레이터에서 LLM SDK를 직접 호출하면 안 됩니다. 프롬프트는 `src/reaction_backend/prompts/<domain>/*.vN.md`로 버전 관리합니다.

### 데이터와 메모리

- PostgreSQL + SQLAlchemy async + asyncpg
- Alembic 마이그레이션
- 사용자·목표·계획·실행·회복·리뷰·알림·동의 모델
- append-only 이벤트·동의 기록
- soft delete와 익명화
- 민감 필드 AES-GCM 암호화

`memory/`는 4계층 메모리 개념의 문서 경계이며, 실제 영속화는 `db/models/`와 `repositories/`가 담당합니다.

### 안전 장치

- AI 초안의 Human-in-the-loop 승인
- 원본 `action_item.status` 불변 규칙
- 암호화 필드와 PII 마스킹
- 알림 주당 한도·야간 금지·클래스 중복 방지
- 사용자별 LLM 토큰 예산
- 배포 환경에서 인증 stub 차단
- 멱등 키가 필요한 승인/일괄 처리 endpoint 보호

## 요구 사항

| 도구 | 기준 |
| --- | --- |
| Python | 3.12 |
| 패키지 관리 | uv 0.9.x |
| 데이터베이스 | PostgreSQL 17 권장 |
| 컨테이너 | Docker / Docker Compose(선택) |

의존성의 정확한 버전은 [`uv.lock`](uv.lock)을 기준으로 합니다.

## 빠른 시작

### 1. uv로 실행

```bash
uv sync
cp .env.example .env
uv run uvicorn reaction_backend.main:app --reload
```

Windows PowerShell:

```powershell
uv sync
Copy-Item .env.example .env
uv run uvicorn reaction_backend.main:app --reload
```

- 헬스 체크: `http://localhost:8000/health`
- Swagger UI: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

앱은 `DATABASE_URL` 없이도 기동할 수 있지만 DB를 사용하는 endpoint는 동작하지 않습니다. 연동 테스트에는 PostgreSQL 설정이 필요합니다.

### 2. Docker Compose로 실행

`docker-compose.yml`은 `.env`를 읽으므로 먼저 파일을 준비해야 합니다.

```bash
cp .env.example .env
docker compose up --build
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

- API: `http://localhost:8000`
- PostgreSQL: `localhost:5432`
- 로컬 DB 기본값: user/db/password = `reaction`

## 환경 변수

전체 목록과 주석은 [`.env.example`](.env.example)을 기준으로 합니다.

### 필수 또는 배포 시 필수

| 변수 | 용도 |
| --- | --- |
| `DATABASE_URL` | PostgreSQL 연결 |
| `JWT_SECRET` | 자체 JWT 서명. 32바이트 이상 권장 |
| `GOOGLE_OAUTH_CLIENT_ID` | 실제 Google ID token 검증 |
| `COLUMN_ENCRYPTION_KEY` | 캘린더 토큰·메모·LLM payload 암호화 |
| `GEMINI_API_KEY` | 실제 Gemini 호출 |

### 선택 기능

| 변수 | 용도 |
| --- | --- |
| `AUTH_STUB_MODE` | 로컬/개발용 인증 우회. staging/prod에서는 부팅 차단 |
| `SCHEDULER_ENABLED` | in-process APScheduler 기동. 기본 `false` |
| `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` | Web Push 발송 |
| `LLM_MODEL*` | 태스크별 고정 Gemini 모델 |
| `LLM_*BUDGET*` / `LLM_*TIMEOUT*` | 비용·지연 가드 |
| `CORS_ALLOW_ORIGINS` | 허용 웹 origin |

실제 `.env`, private key, token, password는 커밋하지 마세요.

## 데이터베이스

```bash
uv run alembic current
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe_change"
uv run alembic downgrade -1
```

모델 변경 절차:

1. `src/reaction_backend/db/models/` 수정
2. migration 생성
3. 생성된 DDL과 downgrade 검토
4. `uv run alembic upgrade head`
5. 테스트와 drift 검사

초기화 스크립트는 데이터를 제거하므로 대상 환경을 확인한 뒤 사용하세요. `APP_ENV=prod`에서는 reset이 거부됩니다.

## 스케줄러

`SCHEDULER_ENABLED=true`일 때만 앱 lifespan에서 APScheduler를 시작합니다. 현재 런타임은 단일 인스턴스를 전제로 하며 모든 작업은 중복 실행에 안전해야 합니다.

등록 작업:

- 모닝 브리프 사전 생성
- 주간 리뷰 사전 계산
- 장기 중단 상태 해소
- 만료된 계획 초안 정리
- 회고하지 않은 카드·미완주 회복 정리
- 오래된 잠정 목표 보관
- 저녁 회고·사전 카드 알림
- 주간 습관 인스턴스 생성

세부 시간표와 함수 계약은 [`src/reaction_backend/scheduler/README.md`](src/reaction_backend/scheduler/README.md)를 참고하세요.

## 검증

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
```

테스트는 라우트, 저장소, 오케스트레이션, 프롬프트, 스케줄러, 회복 규칙, Web Push를 포함합니다. 외부 IO는 fake/stub으로 격리합니다.

## 프로젝트 구조

```text
reaction-backend/
├─ .github/workflows/          CI, 배포, 운영 작업
├─ alembic/                    데이터베이스 마이그레이션
├─ docs/                       API·아키텍처·배포·결정 기록
├─ eval/                       회복 품질 golden case 평가
├─ scripts/                    seed, backfill, preview, 운영 도구
├─ src/reaction_backend/
│  ├─ api/                     라우트, 의존성, 미들웨어, 예외 처리
│  ├─ auth/                    확인 토큰 등 인증 보조
│  ├─ content/                 사용자 노출 정적 콘텐츠
│  ├─ db/                      세션과 ORM 모델
│  ├─ domain/                  프레임워크 독립 도메인 규칙
│  ├─ integrations/            OAuth, Web Push, 안전한 웹 fetch
│  ├─ llm/                     Gemini provider와 Tool Executor
│  ├─ orchestrator/            인터뷰·계획·회복 상태 흐름
│  ├─ prompts/                 버전 관리 프롬프트
│  ├─ repositories/            영속화 경계
│  ├─ safety/                  금지어·암호화·비용·푸시 가드
│  ├─ scheduler/               cron 작업과 APScheduler 등록
│  └─ schemas/                 API 요청·응답 모델
└─ tests/                      단위·통합·프롬프트 회귀 테스트
```

각 주요 레이어의 상세 계약은 해당 폴더의 README를 참고하세요.

## 배포

- 현재 저장소에는 EC2 self-hosted runner 기반 배포와 Render Blueprint 자료가 함께 있습니다.
- 환경별 실제 URL은 API 계약에서 아직 `TBD`입니다.
- 배포 대상을 선택할 때 [`docs/DEPLOY.md`](docs/DEPLOY.md), [`docs/DEPLOY_AWS.md`](docs/DEPLOY_AWS.md), [`docs/cicd.md`](docs/cicd.md)의 현재 상태를 함께 확인하세요.
- staging/production 설정 변경은 팀 합의 없이 수정하지 않습니다.

## 기여 규칙

1. `main`에 직접 push하지 않고 새 브랜치와 PR을 사용합니다.
2. 의존성은 `uv add`/`uv remove`로만 변경합니다.
3. `uv.lock`을 직접 편집하지 않습니다.
4. API 변경은 계약 문서와 함께 제출합니다.
5. 데이터베이스 마이그레이션, 새 외부 의존성, 인증·개인정보 보관 변경은 먼저 합의합니다.
6. AI·캘린더 자동 적용과 금지어 필터 우회는 허용하지 않습니다.

자세한 규칙은 [`AGENTS.md`](AGENTS.md)에 있습니다.

## 현재 제한 사항

- Google Calendar 연결과 실제 읽기/쓰기 동작은 stub입니다.
- `agents/`, `memory/`, `observability/`는 아키텍처 경계이며 독립 실행 모듈은 제한적입니다.
- scheduler는 in-process 메모리 job store를 사용하므로 다중 인스턴스 운영에는 별도 조정이 필요합니다.
- API 계약과 일부 오래된 소스 주석에는 과거 구현 상태가 남아 있을 수 있으므로 실행 코드를 우선 확인합니다.
- 저장소에 별도 라이선스 파일이 없습니다.

## 문서 인덱스

- [`docs/api-contract.md`](docs/api-contract.md): API 경로·응답·오류·멱등성 규약
- [`docs/api-change-log.md`](docs/api-change-log.md): API 변경 이력
- [`docs/architecture.md`](docs/architecture.md): 개념 아키텍처와 제품 원칙
- [`docs/erd-diff.md`](docs/erd-diff.md): ERD와 ORM 매핑
- [`docs/cicd.md`](docs/cicd.md): CI/CD
- [`docs/DEPLOY.md`](docs/DEPLOY.md), [`docs/DEPLOY_AWS.md`](docs/DEPLOY_AWS.md): 배포 런북
- [`docs/BUDGET.md`](docs/BUDGET.md): 인프라·LLM 예산 메모
- [`eval/README.md`](eval/README.md): 회복 품질 평가 데이터
- [GitHub contributors](https://github.com/hanium-reaction/reaction-backend/graphs/contributors)

외부 공개·배포·재사용 전에 팀의 라이선스 정책을 확인하세요.
