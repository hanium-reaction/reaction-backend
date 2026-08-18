# `orchestrator/` — 계획·인터뷰·회복의 결정 흐름

이 패키지는 여러 규칙, LLM worker, repository 입력을 하나의 사용자 흐름으로 조립한다. 핵심 산출물은 먼저 검토 가능한 Draft로 만들고, 사용자 승인(HITL) 뒤에만 저장·활성화한다. LLM이 필요한 노드도 SDK를 직접 사용하지 않고 [`../llm/tool_executor.py`](../llm/tool_executor.py)의 `aiClient.run()`을 거친다.

## 현재 모듈과 책임

### 공통 경계

- [`_common.py`](_common.py): `user_id × agent` PostgreSQL transaction-scoped advisory lock. Interview·Planning·Recovery의 동시 요청을 5초 기다린 뒤 `409 AGENT_CONCURRENT_ACCESS`로 정규화한다.
- [`materials_resolver.py`](materials_resolver.py): 참고 자료 답변이 링크 하나뿐일 때 첫 URL의 본문을 안전한 web fetch 경로로 읽는다. 저장하지 않으며 실패하면 사용자가 본문을 붙여 넣도록 이유별 안내를 반환한다.

### 인터뷰

- [`interview.py`](interview.py): 필수 슬롯 순서, 종료 판정, 질문·답변 검증, 요약, `InterviewOutcome` 생성을 구현한 LangGraph 기반 FSM이다. 다음 슬롯과 종료 여부는 규칙이 결정하고 LLM은 문장 생성·모호성 평가·슬롯 추출·요약을 보조한다.
- [`interview_runner.py`](interview_runner.py): HTTP 요청 한 번당 질문 하나 또는 최종 요약을 반환하도록 FSM 노드를 턴 단위로 구동한다. 직렬화 가능한 상태는 라우터가 요청 사이에 저장한다.
- [`interview_adapter.py`](interview_adapter.py): 내부 `InterviewState`를 다른 오케스트레이터가 의존할 안정된 `InterviewOutcome` 경계로 변환하고 placeholder 목표를 판별한다.

### 첫 계획과 편집

- [`first_plan.py`](first_plan.py): `VALIDATING → PLANNING → REVIEWING → HITL` 그래프를 실행한다. 목표 분해와 품질 검토는 LLM+룰 폴백, 시간 배치는 규칙으로 처리하며 review feedback은 최대 2회 반복한다.
- [`first_plan_adapter.py`](first_plan_adapter.py): `InterviewOutcome`을 prompt·스케줄러 입력으로 바꾸고, 승인된 계획을 한 트랜잭션으로 적용한다. 기존 액션 카드 보존, 밀도·기간·정책 규칙, 초안 만료·대체 경계를 담당한다.
- [`first_plan_milestones.py`](first_plan_milestones.py): 세부 분해 전에 사용자가 확인할 3~5개 마일스톤을 생성한다. LLM 호출 실패 시 준비→핵심 진행→마무리 3단계 룰 폴백을 반환한다.
- [`goal_structuring.py`](goal_structuring.py): 시간 정책·고정 일정·습관으로 free/busy를 계산하고 정책 위반 저장을 막는 도메인 primitive와 transaction guard를 제공한다.
- [`plan_scheduler.py`](plan_scheduler.py): 여러 날에 걸친 action 배치, peak window 우선, 긴 작업의 세션 분할, 휴식 간격, 하루 집중 상한을 순수 규칙으로 계산한다.
- [`plan_edit.py`](plan_edit.py): 직접 편집 시간을 15분 경계로 맞추고 충돌과 `sleep`, `lunch`, `late_night_block` 정책 위반을 판정한다.

### 실행 후 회복과 재계획

- [`recovery.py`](recovery.py): 실패 태그와 회복 전략 카탈로그의 교집합·우선순위로 서로 다른 UX 그룹의 카드 2~4개를 선택하고 if-then 템플릿·목표 날짜·최소 리드를 계산한다.
- [`replan.py`](replan.py): 다음 주 월요일부터 남은 기존 액션을 미래 구간에 다시 배치하는 룰 전용 엔진이다. 기존 액션을 복제하거나 원본 상태를 변경하지 않는다.
- [`habit_penalty.py`](habit_penalty.py): 최근 3주가 연속이고 각 주 달성률이 50% 미만일 때만 비난 대신 빈도 재설계 제안을 계산한다.
- [`weekly_review.py`](weekly_review.py): 실행·회복 투영에서 준수율, 연속 실천일, 회복률, 지연, 카테고리 성공률, peak/drain 구간과 룰 한 줄을 계산한다.

### 프로필과 보조 콘텐츠

- [`profile_memory.py`](profile_memory.py): 인터뷰의 지속형 선호를 `behavioral_profiles`, `interaction_styles`, focus 설정으로 정규화해 저장하고 재인터뷰 슬롯을 시드한다.
- [`inbox_resources.py`](inbox_resources.py): 활성 목표 카테고리에 맞는 정적 자료를 사용자·slug 기준 멱등하게 인박스에 최대 1건씩 넣는다. savepoint를 사용해 자료 삽입 실패가 목표 생성을 깨지 않게 한다.
- [`__init__.py`](__init__.py): 패키지 표식이다.

## 핵심 계약과 불변조건

1. **Draft + HITL:** 계획, 회복, 재계획, 습관 빈도 변경은 자동 적용하지 않는다. 사용자에게 초안을 보여주고 명시적 승인 뒤에만 저장한다.
2. **LLM 단일 게이트:** 직접 Gemini SDK import를 금지한다. LLM 실패·예산 초과·시간 초과에도 각 흐름의 결정적 룰 폴백이 사용자 흐름을 완결해야 한다.
3. **규칙이 결정권 보유:** 슬롯 순서·인터뷰 종료, 시간 충돌·정책 위반, 회복 카드 선택, 실제 스케줄 배치는 규칙이 결정한다. LLM 출력만으로 상태를 활성화하지 않는다.
4. **경계 DTO:** First Plan은 내부 `InterviewState`가 아니라 `InterviewOutcome`에 의존한다. 라우터·DB 모델을 순수 계산 모듈 안으로 끌어오지 않는다.
5. **이력 보존:** recovery/replan은 실패한 실행과 원본 `action_item.status`를 변경하지 않는다. 새 회복 시도와 승인된 미래 블록으로 이어 간다.
6. **정책 보호:** `sleep` 등 절대 시간 정책 위반은 저장 전에 차단하고 트랜잭션을 롤백한다. 시간 계산은 KST, 저장 timestamp는 프로젝트 공통 UTC/KST 규약을 따른다.
7. **동시성:** Interview·Planning·Recovery의 상태 변경 구간은 `_common.user_agent_lock`의 transaction-scoped lock 안에서 마지막 한 번 commit하는 패턴을 따른다. session-level advisory lock으로 바꾸지 않는다.
8. **비난 금지:** 실패·저달성을 처벌 언어로 해석하지 않고 회복 선택지와 재설계 제안으로 변환한다. LLM 사용자 노출 문구는 최종 금지어 필터를 거친다.
9. **보조 기능 격리:** 링크 fetch와 추천 자료 삽입 실패는 계획·목표 생성의 핵심 트랜잭션을 실패시키지 않는다.

## 대표 흐름

### Deep Interview → First Plan

```text
interview_runner.start_interview()
  → 규칙이 다음 필수 슬롯 선택
  → aiClient가 질문/추출을 보조하고 실패 시 카탈로그 폴백
  → InterviewOutcome 확정
  → first_plan_milestones에서 계획 뼈대 확인
  → first_plan: 검증 → 분해 → 규칙 스케줄 → 품질 검토
  → 비활성 Draft 반환
  → 사용자 승인
  → first_plan_adapter가 정책 검증 후 단일 트랜잭션 저장
```

### 실패 실행 → 회복 → 다음 주 재계획

```text
실패 태그 + 활성 전략 카탈로그
  → recovery.select_strategies(): 2~4개 카드
  → 사용자 카드 선택/승인
  → recovery_attempt와 새 회복 블록 저장
  → weekly_review가 실행·회복 KPI 계산
  → replan이 남은 기존 액션의 미래 배치 Draft 생성
  → 사용자 승인 뒤 미래 미착수 블록만 교체
```

## 검증

- 인터뷰: [`test_interview_runner.py`](../../../tests/test_interview_runner.py), [`test_interview_route.py`](../../../tests/test_interview_route.py), [`test_interview_storage.py`](../../../tests/test_interview_storage.py)
- 첫 계획/HITL: [`test_orchestrator_handoff.py`](../../../tests/test_orchestrator_handoff.py), [`test_planning_route.py`](../../../tests/test_planning_route.py), [`test_first_plan_busy.py`](../../../tests/test_first_plan_busy.py), [`test_materialize_goals.py`](../../../tests/test_materialize_goals.py)
- 스케줄·승인·대체: [`test_plan_scheduler.py`](../../../tests/test_plan_scheduler.py), [`test_plan_approve_replace.py`](../../../tests/test_plan_approve_replace.py), [`test_plan_discard.py`](../../../tests/test_plan_discard.py), [`test_plan_supersede_sql.py`](../../../tests/test_plan_supersede_sql.py)
- 참고 자료: [`test_materials_resolver.py`](../../../tests/test_materials_resolver.py)
- 회복/재계획: [`test_recovery.py`](../../../tests/test_recovery.py), [`test_golden_recovery_cases.py`](../../../tests/test_golden_recovery_cases.py), [`test_recovery_selection_coverage.py`](../../../tests/test_recovery_selection_coverage.py), [`test_replan.py`](../../../tests/test_replan.py)
- 프로필·습관·리뷰·인박스: [`test_profile_memory.py`](../../../tests/test_profile_memory.py), [`test_habit_penalty.py`](../../../tests/test_habit_penalty.py), [`test_reviews.py`](../../../tests/test_reviews.py), [`test_inbox_resources.py`](../../../tests/test_inbox_resources.py)

전체 회귀는 저장소 루트에서 `uv run pytest`로 실행한다.

## 확장 방법

- 새 흐름은 먼저 입력·출력 경계 DTO와 순수 규칙을 정의하고 단위 테스트한다. LLM 문장 생성이 필요하면 schema, 버전 prompt, 결정적 폴백을 추가해 `aiClient.run`으로만 연결한다.
- 영속화가 필요한 경우 계산 단계와 저장 단계를 분리하고, repository/adapter가 승인 상태와 policy guard를 확인한 뒤 한 트랜잭션으로 적용한다.
- 새 동시 상태머신은 `user_id × agent` lock 적용 여부와 commit 위치를 명시한다.
- 새 시간 규칙은 자정 통과, 반열린 구간, KST 변환, 기존 block 충돌을 테스트한다.
- 새 메모리 학습은 기존 사실을 덮어쓰지 않고 버전 또는 새 시도 기록으로 남긴다.

## 알려진 제약

- 첫 계획의 규칙 스케줄러는 가용 시간이 부족하면 일부 카드를 미배치로 남길 수 있으며, LLM으로 정책을 무시해 강제 배치하지 않는다.
- 링크 자료는 링크-only 답변의 첫 URL만 읽고 본문을 저장하지 않는다. 로그인 필요, 비텍스트 파일, SSRF 차단, timeout은 안내 후 기존 `(없음)` 경로로 돌아간다.
- 직접 편집 정책 검사는 현재 `sleep`, `lunch`, `late_night_block`에 한정된다. `no_touch`, `break_min`, `custom` 전체 해석기는 아니다.
- 주간 리뷰의 `restart_success_rate`, `repeated_failure_count`, `policy_update_candidates`는 현재 필요한 추가 조인이 없어 값이 비어 있을 수 있다.
- LangGraph를 쓰는 인터뷰·첫 계획도 사용자 대기와 DB 영속화는 라우터/adapter가 담당한다. 한 번의 graph 호출이 전체 사용자 세션을 소유하지 않는다.
