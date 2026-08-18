# `memory/` — 4계층 메모리의 문서 경계

이 디렉터리는 re:action이 데이터를 “계획 → 실행 사실 → 파생 통계 → 다음 행동 정책”으로 분리한다는 아키텍처 경계를 설명한다. 현재 `memory/` 안에 저장소나 서비스 구현은 없고 [`__init__.py`](__init__.py)도 비어 있다. 실제 영속화는 [`../db/models/`](../db/models/)와 [`../repositories/`](../repositories/), 계산·조정은 [`../orchestrator/`](../orchestrator/)에 있다.

## 현재 구성

- [`__init__.py`](__init__.py): 빈 패키지 표식이다. 공개 API나 런타임 등록은 하지 않는다.
- 이 README: 여러 모델과 저장소가 지켜야 하는 계층 규약을 한곳에 기록한다.

## 4계층과 실제 구현 위치

### 1. Planning — 사용자가 하려는 일

`goals`, `goal_nodes`, `action_items`, `scheduled_blocks`, `habits`, `habit_instances`, `time_policies`, `fixed_schedules`, `dependency_links`, `calendar_connections`, `behavioral_profiles`, `interaction_styles`, `interview_*` 모델이 계획·선호·현재 의도를 표현한다. 모델은 [`../db/models/`](../db/models/), 계획 생성과 저장 경계는 [`../orchestrator/first_plan.py`](../orchestrator/first_plan.py)와 [`../orchestrator/first_plan_adapter.py`](../orchestrator/first_plan_adapter.py)에 있다.

### 2. Raw Execution — 실제로 일어난 사실

`execution_events`, `interruption_events`, `context_snapshots`, `execution_failure_tags`, `failure_reason_tags`, `recovery_strategy_catalog`, `recovery_attempts`가 실행과 회복의 원시 사실을 보존한다. 조회·기록은 [`../repositories/execution_repo.py`](../repositories/execution_repo.py)와 회복 라우트·저장소가 담당한다.

### 3. Derived Stats — 원시 사실에서 계산한 결과

`period_summaries`와 `daily_briefs`가 주간/일간 집계 결과를 담는다. 현재 주간 KPI의 순수 계산은 [`../orchestrator/weekly_review.py`](../orchestrator/weekly_review.py), DB 매핑과 저장은 [`../repositories/review_repo.py`](../repositories/review_repo.py), 브리프 캐시는 [`../repositories/daily_brief_repo.py`](../repositories/daily_brief_repo.py)가 담당한다.

### 4. Policy Snapshot — 다음 결정을 위한 버전된 정책

`policy_snapshots`가 학습된 정책의 이력을 보존한다. 직접 저장은 [`../repositories/policy_snapshot_repo.py`](../repositories/policy_snapshot_repo.py)가 담당한다. 인터뷰에서 확인한 지속형 선호는 [`../orchestrator/profile_memory.py`](../orchestrator/profile_memory.py)와 [`../repositories/profile_repo.py`](../repositories/profile_repo.py)를 통해 `behavioral_profiles`, `interaction_styles` 등에 반영된다.

## 핵심 계약과 불변조건

1. 쓰기 주체는 자신이 책임지는 계층만 변경한다. 다른 계층의 데이터를 참고해도 그 계층의 소유권을 가져오지 않는다.
2. 읽기는 계층을 가로지를 수 있다. Derived Stats가 Raw Execution을 집계하고, 회복 흐름이 Planning과 Raw Execution을 함께 읽는 것은 허용된다.
3. 회복 제안은 Policy Snapshot을 직접 변경하지 않는다. 정책 변경 후보와 실제 정책 버전 저장은 명시적인 승인·전용 경계를 거친다.
4. 회복과 forward replan은 기존 실패 실행이나 원본 `action_item.status`를 덮어쓰지 않는다. 새 회복 시도나 미래 블록을 추가·교체해 이력을 보존한다.
5. 사용자 데이터 삭제는 hard delete가 아니라 soft delete/익명화 정책을 따른다. 암호화 대상 memo·OAuth token·LLM payload는 [`../safety/encryption.py`](../safety/encryption.py)의 도메인별 helper를 사용한다.
6. 시간 저장은 UTC, 사용자 노출과 달력일 경계는 KST 규약을 따른다.

## 대표 흐름

```text
Planning: goal/action/scheduled block
  → Raw Execution: 실행 결과와 실패 태그를 append
  → Recovery: 원시 사실을 읽고 recovery_attempt를 생성
  → Derived Stats: 주간 KPI와 daily brief를 계산·저장
  → Policy Snapshot: 승인된 정책 변화만 새 버전으로 저장
  → 다음 Planning이 저장된 선호·정책을 읽음
```

예를 들어 [`../orchestrator/replan.py`](../orchestrator/replan.py)는 남은 Planning 항목과 실행 결과를 읽어 미래 배치 초안만 계산한다. 승인 단계가 기존 미래 블록을 취소·교체하되 기존 액션과 과거 실행 사실은 보존한다.

## 검증

- [`test_profile_memory.py`](../../../tests/test_profile_memory.py): 인터뷰 선호의 프로필 저장·재시드
- [`test_recovery.py`](../../../tests/test_recovery.py), [`test_recovery_completion.py`](../../../tests/test_recovery_completion.py): 실행 사실을 보존하는 회복 흐름
- [`test_replan.py`](../../../tests/test_replan.py), [`test_replan_route.py`](../../../tests/test_replan_route.py): 원본 액션 재사용과 미래 블록 재배치
- [`test_reviews.py`](../../../tests/test_reviews.py), [`test_review_repo_sql.py`](../../../tests/test_review_repo_sql.py): Derived Stats 계산과 저장 쿼리
- [`test_inbox_repo_sql.py`](../../../tests/test_inbox_repo_sql.py): 암호화된 원문을 포함한 저장 경계

전체 회귀는 저장소 루트에서 `uv run pytest`로 실행한다.

## 확장 방법

- 새 데이터가 어느 계층의 진실인지 먼저 결정하고, 해당 [`../db/models/`](../db/models/) 모델과 전용 repository에 쓰기 API를 둔다.
- 계층 간 집계는 ORM row를 순수 도메인 구조로 변환한 뒤 orchestrator에서 계산하고, 결과 저장은 repository가 맡도록 분리한다.
- Policy Snapshot 변경은 기존 행을 덮어쓰지 않고 새 버전으로 추가하며, 변경 사유와 승인 경계를 보존한다.
- 공통 추상화가 실제로 두 개 이상의 소비자에게 필요해질 때만 이 패키지에 런타임 API를 추가한다.

## 알려진 제약

- `memory/` 자체에는 실행 코드, 통합 조회 API, 인덱서가 없다. 현재는 아키텍처 규약을 표현하는 경계다.
- Semantic Memory, embedding, vector DB 검색은 구현되어 있지 않다.
- 여러 계층을 한 번에 탐색하는 범용 “memory service”는 없으며 각 route/orchestrator가 필요한 repository를 조합한다.
- 일부 파생 지표는 아직 원시 이벤트 조인이 없어 `None`으로 남을 수 있다. 현재 주간 KPI의 구체 범위는 [`../orchestrator/weekly_review.py`](../orchestrator/weekly_review.py)를 기준으로 한다.
