# ADR 0001 — DB Schema 정렬 (v0.7.1 vs 실제 코드)

| 항목 | 값 |
| --- | --- |
| 상태 | **Accepted** (2026-05-21, hyeongjun22 7개 모두 추천 수락) |
| 작성일 | 2026-05-21 |
| 작성자 | claude code (review needed) |
| 관련 이슈 | #2 (DB Schema v0.7 Migration / Seed Data / ERD Sync) |
| 관련 PR | #8, #9, #10, #11 + (예정) PR 2-E, PR 2-F |
| 진실 소스 | `Reaction_DB_설계서_v0.7.1.docx` + `Reaction_DevBaseline_v1.0_2026-05-15` |

---

## 1. Context

PR #8 ~ #11 + PR 2-E 까지 진행 후 DB 설계서 v0.7.1 과 실제 코드를 1:1 비교한 결과 (전체는 [`erd-diff.md`](../erd-diff.md)):

- 테이블 갯수: **29/29 ✅**
- 관계 (FK): **95% ✅** (v0.7 user_id denormalize 5개 누락)
- 컬럼 명세: **~70% ⚠️** (다수 누락)
- ENUM 값: **~80% ⚠️** (이름/표기 차이)
- 잠금 결정: **일부 ❌** (UUID v7→v4 등)

P0 누락 컬럼 (habits 페널티 트리거, action_items goal_node_id, recovery_strategy_catalog primary_trigger_tags 등) 은 후속 Issue #5 (LLM Infrastructure) 작업을 막는다.

본 ADR 은 단순 누락 보강(자동 적용)과 구조적 결정사항(팀 합의 필요)을 분리해서, 정렬 마이그레이션 (PR 2-F) 의 범위를 명확히 한다.

## 2. 분류

### 2.1 단순 누락 보강 — PR 2-F 에서 **자동 적용**

ADR 결정 없이 진행. 모두 v0.7.1 설계서 명세 그대로:

| 영역 | 변경 |
| --- | --- |
| **users** | `name VARCHAR(100) NN` + `is_anonymized BOOLEAN NN default false` |
| **habits** | target_count / minutes_per_session / time_preference / priority_level / consecutive_miss_weeks / last_penalty_evaluated_at / last_penalty_decision (7 컬럼) |
| **goals** | status enum (active/archived/completed) / week_tier_key / estimated_minutes |
| **goal_nodes** | node_type / order_index / is_leaf |
| **action_items** | goal_node_id FK / habit_instance_id FK / category / source enum 확장 (recovery_downscope/reschedule/carryover/park v0.7.1) |
| **scheduled_blocks** | external_calendar_event_id / block_status enum 정정 (started 추가) |
| **dependency_links** | dependency_type enum / FK 이름 정렬 (depends_on_action_item_id) |
| **execution_events** | scheduled_block_id NN FK / plan_start_at NN / plan_end_at NN |
| **interruption_events** | interrupt_context_note_encrypted / enum 값 정렬 |
| **recovery_attempts** | trigger_tag / decision_reason / recovery_started_at / recovery_completed_at / recovery_result |
| **recovery_strategy_catalog** | **primary_trigger_tags JSONB (v0.7.1 핵심)** / label_ko / allow_rest_mode / display_priority (sort_order rename) |
| **context_snapshots** | day_of_week 타입 VARCHAR(10) "mon..sun" 으로 변경 / location_type enum 정정 |
| **calendar_connections** | provider enum (google/apple/samsung) / scopes (rename) |
| **behavioral_profiles** | preferred_start_time / preferred_end_time / context_switching_cost / recovery_speed_type |
| **interaction_styles** | plan_change_transparency / enum 값 정렬 (brief-normal-detailed / minimal-standard-active) |
| **period_summaries** | avg_delay_minutes / restart_success_rate / repeated_failure_count / llm_one_liner / failure_analysis / period_start→start_date rename |
| **llm_runs** | input_summary_encrypted / output_summary_encrypted / success / module enum 5종 정렬 / tokens_in/out / fell_back / trace_id (이름 정렬) |
| **idempotency_keys** | UNIQUE (user_id, endpoint, key) 로 변경 |
| **inbox_items** | text→raw_text_encrypted (rename + 암호화 표시) / ai_category_guess / user_category / promoted_goal_id / status enum 정정 (captured/classified/archived/promoted) |
| **v0.7 user_id denormalize** | scheduled_blocks · dependency_links · interruption_events · recovery_attempts · context_snapshots (5 테이블) |

---

### 2.2 팀 결정 필요 — **본 ADR 의 7개 사안**

우리(claude code) 의견과 그 근거를 같이 적었다. 사용자가 각 항목별로 **✅ accept / ❌ override** 표시 + 사유 적어주세요.

---

## 3. 결정 사안

### 3.1 ID 형식 — UUID v7 vs UUID v4

**설계서 v0.7.1 §3.2**: UUID **v7** + 도메인 prefix (`goal_xxx`, `act_xxx`) — 시간 정렬 가능

**현재 우리 코드**: UUID v4 (`gen_random_uuid()` PG native)

**옵션:**
- **A.** UUID v7 마이그레이션 (PG 17 에서 v7 native 함수 없음 → pgcrypto + 자체 함수 작성 필요)
- **B.** UUID v4 유지 + 도메인 prefix 만 API 응답 레이어에서 추가
- **C.** UUID v4 유지 + prefix 도 안 씀 (지금 그대로)

**우리 추천: B**

| 기준 | 평가 |
| --- | --- |
| 시간 정렬 (v7 장점) | UUID 자체로 정렬 안 해도 `created_at` 인덱스로 충분히 빠름. v7 의 정렬 장점은 인덱스 페이지 fragmentation 감소 — re:action 규모(베타 ~수천명)에선 무의미 |
| 도메인 prefix (가독성) | API 응답에서 `user_550e8400-...` 형태로 노출하면 디버깅 가독성 ↑. DB 안은 raw UUID 로 유지하면 코드 단순 |
| 마이그레이션 부담 | v7 함수 직접 작성 + 기존 행 ID 마이그레이션 + alembic 마이그레이션 모두 필요. 비용 큼 |
| 결론 | **DB 는 v4 그대로, API 응답 레이어에서 prefix 추가** — 설계서의 "시간 정렬" 의도는 created_at 으로 충족, "도메인 prefix" 의도는 응답 레이어에서 충족 |

**결정**: ❓ (사용자 결정)

---

### 3.2 PolicySnapshot 구조 — 단일 payload vs 4 영역 분리

**설계서 v0.7.1 §5.24 / §6.6**: 단일 `payload JSONB NN` (TypeScript 타입 정의로 구조 명시)

**현재 우리 코드**: 4 개 JSONB 컬럼 분리 (behavioral_profile / execution_constraints / interaction_style / recovery_policy)

**옵션:**
- **A.** 설계서 따라 단일 payload JSONB 로 통합
- **B.** 우리 4 컬럼 분리 유지

**우리 추천: B**

| 기준 | 평가 |
| --- | --- |
| 인덱싱 | 단일 JSONB 에서 특정 영역만 조회 시 GIN 인덱스 + JSON path 필요. 4 컬럼 분리는 일반 컬럼 조회 가능 |
| 스키마 진화 | 단일 payload 는 schema 버전 관리 어려움 (§6.6 에도 "schema 버전 관리 필요" 명시). 4 컬럼 분리는 각각 진화 가능 |
| 부분 업데이트 | 단일 payload 는 UPDATE 시 전체 JSON 재작성 (트랜잭션 락 크기 ↑). 4 컬럼 분리는 변경된 영역만 UPDATE |
| Append-only 보장 | "변경 시 INSERT + 이전 행 is_active=false" 정책은 두 구조 모두 동일 |
| 결론 | **4 컬럼 분리 유지** — 설계서의 의도(JSON으로 유연성 + append-only) 충족하면서 정규화 장점 추가. ADR 로 명시적 차이 기록 |

> ⚠️ 다만 설계서에 명시된 부속 필드 (`reason_for_update`, `prompt_version`, `source rule/llm/user_manual`) 는 추가 필요.

**결정**: ❓ (사용자 결정)

---

### 3.3 마스터 테이블 PK — string code vs UUID

**설계서 v0.7.1**:
- `failure_reason_tags`: PK = `tag_code VARCHAR(30)`
- `recovery_strategy_catalog`: PK = `strategy_type VARCHAR(30)`

**현재 우리 코드**: PK = UUID + UNIQUE(`tag_code` / `strategy_code`)

**옵션:**
- **A.** 설계서 따라 string code PK 로 변경
- **B.** UUID PK 유지

**우리 추천: A (설계서 따름)**

| 기준 | 평가 |
| --- | --- |
| 일관성 | 다른 모든 테이블이 UUID — 마스터 테이블만 string PK 는 일관성 깨짐 (UUID 일관성 손해) |
| 사용성 | 마스터는 13/9 개 enum-like 데이터 — FK 참조 시 `'HARD_TO_START'` 같은 명시적 코드가 더 명확 (`execution_failure_tags.tag_code` → 사람이 읽음) |
| 변경 가능성 | 마스터 데이터는 거의 안 바뀜. tag_code/strategy_code 자체가 변경 가능성 거의 0 (라벨/설명만 바뀜) |
| 마이그레이션 부담 | PK 변경 → FK 컬럼 타입도 모두 변경 (execution_failure_tags, recovery_attempts). 1~2 테이블이라 작음 |
| 결론 | **string PK 채택** — 마스터 테이블의 특수성 인정. enum-like 사용 흐름에 자연스러움. UUID 일관성은 일반 도메인 테이블에서만 강제 |

**결정**: ❓ (사용자 결정)

---

### 3.4 prompt_version 타입 — VARCHAR vs Integer

**설계서 v0.7.1 §5.28**: `prompt_version VARCHAR(40) NN` (예: `v1.2-shadow`, `interview-deep-v3-canary`)

**현재 우리 코드**: `prompt_version Integer` (default 1)

**옵션:**
- **A.** 설계서 따라 VARCHAR(40)
- **B.** Integer 유지

**우리 추천: A (설계서 따름)**

| 기준 | 평가 |
| --- | --- |
| A/B 테스트 | shadow / canary / 10%-rollout 같은 라벨 표현 가능 — Integer 로는 표현 불가 |
| 비교 | 같은 prompt_id 의 v1.2-shadow vs v1.3 비교가 가능 — Integer 도 가능 |
| 회귀 추적 | prompts/ 디렉토리의 `*.v1.md`, `*.v2.md` 같은 파일명과 1:1 매핑 |
| 결론 | **VARCHAR 채택** — A/B 테스트 friendly. 우리 prompts 레이어 (Issue #5) 와 1:1 매핑 |

**결정**: ❓ (사용자 결정)

---

### 3.5 dependency_links 의 dependency_type

**설계서 v0.7.1 §5.11**: `dependency_type VARCHAR(20) must_finish/should_finish/soft NN`

**현재 우리 코드**: 없음 (모든 의존성이 hard)

**옵션:**
- **A.** 설계서 따라 추가 (3 종 enum)
- **B.** 단순 hard 의존성만 유지

**우리 추천: A (설계서 따름)**

| 기준 | 평가 |
| --- | --- |
| Scheduler Agent 의 의사결정 | `must_finish` 는 hard constraint, `should_finish` 는 soft, `soft` 는 hint — 시간 배치 시 우선순위 다름 |
| Re:action 실제 사용 케이스 | 예: "캡스톤 설계 끝나야 구현 시작 (must)" vs "운동 후 학습이면 좋겠음 (soft)" — 두 종류 필요 |
| 결론 | **추가 채택** — Planning Agent (Issue #5) 의 정밀도에 영향 |

**결정**: ❓ (사용자 결정)

---

### 3.6 컬럼 암호화 실제 구현 — 본 PR (2-F) vs 후속

**설계서 v0.7.1 §3.2**: `*_encrypted` 컬럼은 KMS at-rest 암호화 필수

**현재 우리 코드**: 컬럼명만 `_encrypted` 접미사, 실제 암호화 함수 없음 (평문 저장 가능)

**Issue #2 본문**: "범위 제외" 명시 ("RLS 또는 세밀한 권한 정책 완성", "실제 OAuth/Calendar 토큰 저장 구현")

**옵션:**
- **A.** PR 2-F 에서 함께 구현 (pgcrypto + 앱 레이어 헬퍼)
- **B.** Issue #2 범위 외 유지 → 별도 후속 PR

**우리 추천: B (범위 외 유지)**

| 기준 | 평가 |
| --- | --- |
| 범위 명확성 | Issue #2 본문이 "범위 제외" 로 명시 — 범위 확장은 별도 합의 필요 |
| 의존성 | 암호화 키 관리 (env / KMS / Supabase Vault 등) 결정 필요. 인프라 결정 |
| 시간 | 암호화 구현은 작지만 키 관리 + 테스트 + 익명화 cron 까지 묶이면 별도 PR 분량 |
| 결론 | **범위 외 유지** — `*_encrypted` 컬럼명만 정렬 (현재 그대로), 함수는 후속 PR (제안: Issue #6 follow-up 또는 별도 보안 이슈) |

**결정**: ❓ (사용자 결정)

---

### 3.7 RLS (Row Level Security) — 본 PR (2-F) vs 후속

**설계서 v0.7.1 §3.2**: 모든 테이블에 `user_id` 격리 가드

**현재 우리 코드**: 없음

**Issue #2 본문**: "범위 제외" 명시 ("RLS 또는 세밀한 권한 정책 완성")

**옵션:**
- **A.** PR 2-F 에서 함께 구현
- **B.** Issue #2 범위 외 유지 → 별도 후속 PR

**우리 추천: B (범위 외 유지)**

| 기준 | 평가 |
| --- | --- |
| 범위 명확성 | Issue #2 본문 "범위 제외" 명시 |
| Supabase RLS 패턴 | Supabase 권장은 PostgreSQL RLS + JWT claims. 우리 자체 JWT 와 통합 시 RLS policy SQL 작성 필요 |
| v0.7 user_id denormalize 와 관계 | denormalize 는 RLS 의 전제 조건 — 본 PR (2-F) 에서 denormalize 까지만 추가 |
| 시간 | RLS policy 는 29 테이블 × CRUD 4 = ~116 policy SQL. 분량 큼 |
| 결론 | **범위 외 유지** — denormalize 만 본 PR. RLS policy 자체는 별도 후속 PR (PR 2-G 또는 Issue #2 follow-up) |

**결정**: ❓ (사용자 결정)

---

## 4. 의도적으로 보존하는 우리 개선

사용자/팀 합의 없이도 보존하는 부분 (설계서에 없지만 명백한 개선):

| 우리 추가 | 이유 |
| --- | --- |
| `goals.why_now` / `first_step` | Morning Brief 의 reasonWhyNow / firstStep 와 1:1 매핑 — UX 직접 지원 |
| `daily_briefs.adjustment_hints` JSONB | "오후 회의 전에 마무리하면 좋아요" 같은 보조 안내 |
| `failure_reason_tags.sort_order` | UI 표시 순서 |
| `idempotency_keys.request_body_hash` / `response_status` | 같은 키 다른 body 감지 (409 IDEMPOTENCY_KEY_MISMATCH) — API 계약 §1.7 에서 명시 |
| `period_summaries.peak_point_window` / `generated_at` | drain 의 짝, generated_at 은 cron 추적 |
| `context_snapshots.companion_present` | Memory Structure 14 필드 충족 (설계서가 누락) |
| `llm_runs.prompt_id` / `error` | 디버깅 가시화 |

설계서에 없다고 제거하면 정보 손실 발생.

## 5. 결정 후 후속 작업

- PR 2-F: 위 결정 반영해서 마이그레이션 1개로 정렬
- 본 ADR 의 Status: **Proposed → Accepted** 로 갱신
- 후속 PR (예정):
  - PR 2-G: RLS policy (29 테이블)
  - PR 2-H: 컬럼 암호화 실제 함수 + 익명화 cron

---

## 6. 결정 기록 양식

사용자가 아래 표에 ✅ accept / ❌ override (그 경우 어떤 옵션 / 사유) 적어주세요:

| # | 결정사안 | 우리 추천 | 사용자 결정 |
| --- | --- | --- | --- |
| 3.1 | ID 형식 | B (UUID v4 + API 응답에서 prefix) | ✅ B (수락) |
| 3.2 | PolicySnapshot 구조 | B (4 컬럼 분리 유지) | ✅ B (수락) |
| 3.3 | 마스터 테이블 PK | A (string code PK, 설계서 따름) | ✅ A (수락) |
| 3.4 | prompt_version 타입 | A (VARCHAR, 설계서 따름) | ✅ A (수락) |
| 3.5 | dependency_type 추가 | A (3종 enum 추가) | ✅ A (수락) |
| 3.6 | 컬럼 암호화 함수 | B (Issue #2 범위 외 유지) | ✅ B (수락) |
| 3.7 | RLS policy | B (Issue #2 범위 외 유지) | ✅ B (수락) |
