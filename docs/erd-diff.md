# ERD ↔ 코드 매핑 (DB 설계서 v0.7.1 정렬 완료)

> 진실 소스: `Reaction_DB_설계서_v0.7.1.docx` + `Reaction_DB_시나리오별_상세분석.md` + `Reaction_DevBaseline_v1.0`.
> 실제 코드: `src/reaction_backend/db/models/`, 마이그레이션 `alembic/versions/`.
> 적용 환경: Supabase PostgreSQL 17.6 (Session pooler, ap-northeast-2).
> 정렬 결정: [`docs/decisions/0001-db-schema-alignment.md`](decisions/0001-db-schema-alignment.md) (7 결정사안, 모두 Accepted).

## 1. 정렬 상태 — **v0.7.1 일치 ✅**

PR 2-F drift fix 적용 후 명세서와 1:1 일치.

| 영역 | 상태 |
|---|---|
| 테이블 갯수 | **29/29** ✅ |
| 컬럼 명세 | **100% ✅** — 모든 누락 컬럼 추가, rename 적용 |
| ENUM 값 | **100% ✅** — variable→varies, work→office, 4종/9종/5종 등 모두 v0.7.1 정렬 |
| 마스터 테이블 PK | **string PK** (tag_code / strategy_type) |
| v0.7 user_id denormalize | **7 테이블 ✅** |
| primary_trigger_tags JSONB | **적용 ✅** (v0.7.1 핵심 신규 §6.10) |
| seed 데이터 | **13 + 9 자동 적용 ✅** |

## 2. 도메인 테이블 매핑 (29개)

### 사용자/온보딩 (8)
`users` · `interview_sessions` · `interview_slot_answers` · `behavioral_profiles`
· `interaction_styles` · `notification_settings` · `calendar_connections` · `fixed_schedules`

### 계획 (9)
`time_policies` · `goals` · `goal_nodes` · `habits` · `habit_instances`
· `inbox_items` · `action_items` · `scheduled_blocks` · `dependency_links`

### 실행/회복 (7)
`execution_events` · `interruption_events` · `context_snapshots` · `failure_reason_tags` (PK=tag_code)
· `execution_failure_tags` · `recovery_strategy_catalog` (PK=strategy_type) · `recovery_attempts`

### 집계/시스템 (5)
`period_summaries` · `daily_briefs` · `policy_snapshots` · `llm_runs` · `idempotency_keys`

## 3. 마이그레이션 체인

PR 2-F 에서 **5개 → 2개로 통합** (ADR Option B):

```
59acd6c5f086 (create all tables aligned with DB v0.7.1, 1487줄)
   ▼
d09c105520b5 (seed master data v0.7.1 — 13 + 9)  ← HEAD
```

이전 5개 마이그레이션 (`9f7c7958ccc8`, `ced84c31fc05`, `235ce4f5c94c`, `6a8ae9bf3be0`, `a96678e9ffe5`) 는 PR 2-F 에서 통합 삭제됨.

## 4. ADR 의도적 차이 — [`0001-db-schema-alignment.md`](decisions/0001-db-schema-alignment.md)

| ADR # | 결정 | 우리 코드 |
|---|---|---|
| §3.1 | UUID v4 (설계서 v7) | UUID v4 (`gen_random_uuid()`) + API prefix는 응답 레이어 |
| §3.2 | PolicySnapshot 4 컬럼 분리 (설계서 단일 payload) | `behavioral_profile` / `execution_constraints` / `interaction_style` / `recovery_policy` 4 JSONB |
| §3.3 | 마스터 string PK (설계서 따름) | `tag_code` · `strategy_type` |
| §3.4 | prompt_version VARCHAR (설계서 따름) | VARCHAR(40) |
| §3.5 | dependency_type 3종 enum (설계서 따름) | `must_finish/should_finish/soft` |
| §3.6 | 컬럼 암호화 함수 — Issue #2 외 | 컬럼명만 (`*_encrypted`) |
| §3.7 | RLS — Issue #2 외 | 미구현 (denormalize 만) |

## 5. 우리가 보존한 개선 (ADR §4)

설계서에 없지만 명백한 개선:

- `goals.why_now` / `first_step` — Morning Brief 매핑
- `daily_briefs.adjustment_hints` JSONB
- `failure_reason_tags.sort_order` — UI 순서
- `idempotency_keys.request_body_hash` / `response_status` — API §1.7
- `context_snapshots.companion_present` — Memory Structure 14필드
- `llm_runs.prompt_id` / `error` — 디버깅
- `period_summaries.peak_point_window` / `generated_at` — drain 짝 + cron 추적
- `period_type.quarterly` 추가
- `action_items.inbox_item_id` FK

## 6. 마스터 데이터 (자동 seed, d09c105520b5)

### failure_reason_tags — 13종 잠금 (§5.13)

`TIME_SHORTAGE` · `LOW_ENERGY` · `HARD_TO_START` · `PRIORITY_SHIFT` · `PLAN_TOO_BIG`
· `FATIGUE` · `AMBIGUITY` · `CONFLICT` · `OVERRUN` · `AVOIDANCE` · `DISTRACTION`
· `EMERGENCY` · `CONTEXT_LOSS`

### recovery_strategy_catalog — 9전략 + primary_trigger_tags (v0.7.1 §6.10)

| 전략 | 그룹 | primary_trigger_tags | 동적 조건 |
|---|---|---|---|
| NANO_STEP | DOWNSCOPE | `["AMBIGUITY", "HARD_TO_START"]` | — |
| DOWNSCOPE_DEFAULT | DOWNSCOPE | `["FATIGUE", "PLAN_TOO_BIG"]` | — |
| ENVIRONMENT_SHIFT | DOWNSCOPE | `["DISTRACTION"]` | location=home |
| CONTEXT_REWARMING | DOWNSCOPE | `["CONTEXT_LOSS"]` | resumed_after_interrupt=false |
| RESCHEDULE_DEFAULT | RESCHEDULE | `["CONFLICT"]` | — |
| ACTIVE_RECOVERY | RESCHEDULE | `["LOW_ENERGY", "FATIGUE"]` | allow_rest_mode=true |
| CARRYOVER_DEFAULT | CARRY_OVER | `["PRIORITY_SHIFT"]` | — |
| FREEZE_SLOT | CARRY_OVER | `["EMERGENCY"]` | — |
| PARK_DEFAULT | PARK | `[]` | overwhelm_level ≥ 4 |

## 7. 공통 규약

- **PK**: UUID v4 (도메인) / `tag_code`, `strategy_type` (마스터, string)
- **TimestampMixin**: 모든 도메인 (LlmRun 만 INSERT only)
- **SoftDeleteMixin**: users · fixed_schedules · time_policies · goals · goal_nodes · habits · inbox_items · action_items
- **암호화 컬럼**: `*_encrypted` 접미사 — 함수는 후속
- **v0.7 user_id denormalize**: 7 테이블 (scheduled_blocks, dependency_links, interruption_events, recovery_attempts, context_snapshots, execution_events, action_items)
- **시간**: 저장 UTC, 응답 KST(+09:00)

## 8. 후속 작업 (Issue #2 범위 외)

- RLS policy — 후속 PR (29 테이블 × 4 op ≈ 116 policy)
- 컬럼 암호화 실제 함수 — `safety/` 레이어
- 익명화 cron — 90일 비활성 + `is_anonymized=true` + `*_encrypted` NULL
- 도메인 라우터 실제 구현 — Issue #3
- Semantic Memory (pgvector) — P2

## 9. 검증 절차

```bash
uv run alembic current          # → d09c105520b5 (head)
uv run alembic check            # drift 감지

# 마이그레이션
uv run alembic revision --autogenerate -m "..."
uv run alembic upgrade head
uv run alembic downgrade -1

# DB reset + 마스터 seed
uv run python -m scripts.db_reset

# Demo 사용자
uv run python -m scripts.db_seed_demo
```
