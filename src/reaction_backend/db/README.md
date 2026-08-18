# `db/` — 비동기 PostgreSQL 연결과 ORM 모델

이 패키지는 Reaction의 SQLAlchemy 기반 영속성 토대다. 엔진·세션 생명주기, 공통 ORM
기반 클래스, 애플리케이션 모델을 제공하며 실제 쿼리는
[`repositories/`](../repositories/README.md)가 캡슐화한다. 스키마 변경 이력은 프로젝트
루트의 [`alembic/`](../../../alembic/README)에 둔다.

## 구성

- [`base.py`](base.py): 모든 모델의 `Base`, 생성·수정 시각을 제공하는 `TimestampMixin`,
  `archived_at`을 제공하는 `SoftDeleteMixin`.
- [`session.py`](session.py): PostgreSQL URL 정규화, 프로세스 단일 async engine과
  sessionmaker, FastAPI용 `get_db()`, 종료 시 pool을 닫는 `dispose_engine()`.
- [`models/`](models/): 테이블별 ORM 모델. [`models/__init__.py`](models/__init__.py)가
  Alembic 자동 발견과 통일된 import 경로를 위해 모든 모델을 export한다.

## 모델 구성

현재 `db.models`에서 export하는 모델 클래스는 32개다.

- 사용자·온보딩: `User`, `UserConsent`, `InterviewSession`, `InterviewSlotAnswer`,
  `BehavioralProfile`, `InteractionStyle`, `NotificationSetting`, `CalendarConnection`
- 계획·일정: `Goal`, `GoalNode`, `TimePolicy`, `FixedSchedule`, `Habit`, `HabitInstance`,
  `InboxItem`, `ActionItem`, `ScheduledBlock`, `DependencyLink`, `PlanDraft`
- 실행·회복: `ExecutionEvent`, `InterruptionEvent`, `ContextSnapshot`, `FailureReasonTag`,
  `ExecutionFailureTag`, `RecoveryStrategyCatalog`, `RecoveryAttempt`
- 집계·시스템: `DailyBrief`, `PeriodSummary`, `PolicySnapshot`, `LlmRun`, `IdempotencyKey`,
  `NotificationSend`

모델을 새로 추가하면 파일 작성에 그치지 않고 `models/__init__.py`에서 import와 `__all__`을
갱신해야 한다. 그렇지 않으면 `Base.metadata`를 사용하는 Alembic이 모델을 발견하지 못할 수
있다.

## 연결과 세션 계약

`session.normalize_async_url()`은 `postgres://`를 `postgresql://`로 정규화하고 표준
PostgreSQL URL을 `postgresql+asyncpg://`로 바꾼다. 이미 driver suffix가 있는 URL은 그대로
사용한다. `DATABASE_URL`은 설정에서 읽고 `DB_ECHO`는 SQL 로그 출력 여부를 제어한다.

엔진은 끊긴 연결을 확인하고 30분 주기로 재연결하며 asyncpg statement/prepared statement
cache를 비활성화해 PgBouncer transaction pooler와 호환되게 구성돼 있다. 세션은
`expire_on_commit=False`, `autoflush=False`다.

FastAPI 코드에서는 `Depends(get_db)`로 세션을 받는다. `get_db()`는 예외 시 rollback하고
종료 시 close하지만 정상 경로를 자동 commit하지 않는다. 트랜잭션 완료 여부는 라우트나
서비스 등 유스케이스 경계가 명시적으로 결정한다. Repository write는 보통 `flush()`까지
수행하고 임의로 commit하지 않는다.

## 데이터 규약

- 시간 컬럼은 timezone-aware `timestamptz`로 저장하고 UTC를 기준으로 한다. API 응답의
  KST 변환은 [`schemas/common.py`](../schemas/common.py)가 담당한다.
- 사용자 데이터는 hard delete하지 않고 지원되는 모델의 `archived_at`을 설정한다. 기본
  조회는 보관된 행을 제외해야 한다.
- 토큰과 민감한 메모의 평문을 저장하지 않는다. 암호화 필드는 `*_encrypted` 이름과
  [`safety/encryption.py`](../safety/encryption.py)의 경계를 따른다.
- 사용자 소유 데이터 쿼리는 항상 `user_id` 범위를 포함한다.
- 모델 변경은 [`alembic/`](../../../alembic/README)의 새 revision과 함께 배포한다.
- SQLAlchemy 모델을 API 응답 객체로 직접 반환하지 않고 schema 레이어에서 직렬화한다.

## 마이그레이션과 검증

```bash
uv run alembic upgrade head
uv run pytest -v tests/test_db.py tests/test_kst_serialization.py
```

- [`tests/test_db.py`](../../../tests/test_db.py): URL 정규화와 세션/모델 기본 계약
- [`tests/test_kst_serialization.py`](../../../tests/test_kst_serialization.py): UTC 저장과 KST
  응답 경계
- [`tests/test_action_item_repo_sql.py`](../../../tests/test_action_item_repo_sql.py),
  [`tests/test_goal_repo_sql.py`](../../../tests/test_goal_repo_sql.py): 주요 query 계약
- [`alembic/env.py`](../../../alembic/env.py): 모델 metadata와 migration 환경 연결

전체 검증은 프로젝트 루트에서 다음 순서로 실행한다.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
```

## 제약과 주의

- 실제 연결 검증과 migration 실행에는 PostgreSQL 및 유효한 `DATABASE_URL`이 필요하다.
- `autoflush=False`이므로 쿼리 전에 미반영 변경이 필요하면 명시적으로 `flush()`한다.
- 세션을 요청 범위 밖 background job에서 사용할 때는
  [`scheduler/runtime.py`](../scheduler/runtime.py)처럼 별도 scope와 예외 rollback을 둔다.
- 모델 모듈의 과거 설계 주석에 적힌 테이블 수보다 현재 export 수가 많을 수 있다. 현재
  구현 범위는 `models/__init__.py`와 Alembic head를 기준으로 확인한다.
