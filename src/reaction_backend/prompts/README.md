# `prompts/` — 파일 기반 Prompt Registry

이 패키지는 LLM 시스템 프롬프트의 파일명, 버전, 변수 계약을 관리하는 단일 진실 소스다. [`registry.py`](registry.py)가 파일을 자동 발견하고, [`../llm/tool_executor.py`](../llm/tool_executor.py)가 prompt ID를 렌더링해 호출·폴백 기록에 같은 버전을 남긴다.

## 현재 구성

- [`registry.py`](registry.py): 지원 도메인 스캔, 버전 정렬, 캐시, 조회, 변수 치환을 구현한다.
- [`brief/morning_brief.v1.md`](brief/morning_brief.v1.md): 아침 브리프 구조화 응답
- [`failure_diagnosis/classify.v1.md`](failure_diagnosis/classify.v1.md): 실패 원인 태그 분류
- [`habit_penalty/evaluate.v1.md`](habit_penalty/evaluate.v1.md): 반복 미실행 평가
- [`inbox/classify.v1.md`](inbox/classify.v1.md): 인박스 원문의 작업 분류
- [`interview/ambiguity_score.v1.md`](interview/ambiguity_score.v1.md): 인터뷰 답변 모호성 점수
- [`interview/next_question.v1.md`](interview/next_question.v1.md): 다음 질문 문장 생성
- [`interview/slot_extraction.v1.md`](interview/slot_extraction.v1.md): 자유 답변의 슬롯 추출
- [`interview/summary.v1.md`](interview/summary.v1.md): 인터뷰 확인 요약
- [`planning/goal_decompose.v1.md`](planning/goal_decompose.v1.md): 목표를 노드·실행 카드로 분해
- [`planning/plan_milestones.v1.md`](planning/plan_milestones.v1.md): 계획 뼈대용 3~5개 마일스톤 생성
- [`planning/plan_quality.v2.md`](planning/plan_quality.v2.md): 계획 품질 검토
- [`recovery/if_then_proposal.v1.md`](recovery/if_then_proposal.v1.md), [`recovery/if_then_proposal.v2.md`](recovery/if_then_proposal.v2.md): 회복 if-then 문구. 버전을 생략하면 현재 v2가 선택된다.
- [`__init__.py`](__init__.py): prompt 패키지 표식이다.

지원 도메인은 `interview`, `planning`, `recovery`, `brief`, `inbox`, `review`, `habit_penalty`, `failure_diagnosis` 여덟 개다. `review`는 registry가 허용하지만 현재 prompt 파일은 없다.

## 파일명과 ID 계약

1. 파일은 `prompts/<domain>/<name>.v<version>.md` 형식이어야 한다. `name`은 소문자 영숫자와 `_`, 버전은 `1` 또는 `1.2` 같은 숫자 형식만 허용한다.
2. 호출 ID는 `domain/name` 또는 `domain/name@v1.2`다. 버전을 생략하면 숫자 tuple 비교로 가장 높은 버전을 자동 선택한다.
3. 지원하지 않는 domain과 파일명 규칙에 맞지 않는 Markdown은 경고 후 registry에서 무시한다.
4. 변수는 `{{variable_name}}` 형식의 단순 치환이다. 필요한 변수가 없으면 빈 문자열로 조용히 진행하지 않고 `PromptRenderError`를 발생시켜 LLM Tool Executor의 결정적 폴백으로 보낸다.
5. registry 상태는 프로세스에서 캐시된다. 테스트나 핫리로드에서 파일이 바뀌면 `registry.reload()`로 캐시를 비워야 한다.
6. 실제 실행은 반드시 [`../llm/tool_executor.py`](../llm/tool_executor.py)를 통한다. 이 경로가 prompt ID/version, 모델, 토큰, fallback 여부를 `llm_runs`에 연결한다.

## 대표 흐름

```text
aiClient.run(prompt_id="recovery/if_then_proposal", variables=...)
  → registry.get(): 최신 v2 선택
  → PromptTemplate.render(): {{var}} 치환 또는 PromptRenderError
  → tool_executor: 톤 prefix + provider + 금지어 필터
  → llm_runs: prompt_id="recovery/if_then_proposal", prompt_version="2"
```

버전 고정이 필요한 회귀나 점진 전환은 `recovery/if_then_proposal@v1`처럼 full ID를 명시한다.

## 검증

- [`tests/prompts/test_brief_prompts.py`](../../../tests/prompts/test_brief_prompts.py): 브리프 파일과 구조화 출력 계약
- [`tests/prompts/test_interview_prompts.py`](../../../tests/prompts/test_interview_prompts.py): 인터뷰 prompt 변수와 응답 규칙
- [`tests/prompts/test_planning_prompts.py`](../../../tests/prompts/test_planning_prompts.py): 목표 분해·마일스톤·품질 검토 prompt
- [`tests/prompts/test_recovery_prompts.py`](../../../tests/prompts/test_recovery_prompts.py): 회복 v1/v2 등록과 최신 버전 선택
- [`test_orchestrator_handoff.py`](../../../tests/test_orchestrator_handoff.py): 오케스트레이터가 올바른 prompt/schema/fallback 계약으로 넘기는지 검증

전체 회귀는 저장소 루트에서 `uv run pytest`로 실행한다.

## 확장 방법

1. 기존 지원 domain 아래에 새 `<name>.v1.md`를 추가한다. 새 domain이 필요하면 `SUPPORTED_DOMAINS`와 테스트를 함께 변경한다.
2. prompt의 모든 `{{var}}`를 호출자의 `variables` 매핑과 테스트 fixture에 반영한다.
3. 출력 JSON 계약은 호출자의 Pydantic schema와 일치시키고, 사실을 모를 때 지어내지 않는 규칙 및 HITL/비난 금지 정책을 prompt에 명시한다.
4. 기존 prompt를 바꿀 때는 덮어쓰기보다 새 버전 파일을 추가한다. 자동 최신 선택이 의도된 호출과 고정 버전을 써야 하는 호출을 구분한다.
5. 추가 후 `registry.reload()`를 사용하는 단위 테스트와 실제 `aiClient.run` 연결 테스트를 작성한다.

## 알려진 제약

- shadow A/B, rollout 비율, 실험군 할당, 별도 active-version 설정은 구현되어 있지 않다. 현재는 버전 생략 시 항상 숫자상 최신 파일을 선택한다.
- 템플릿 엔진은 조건문·반복·escaping이 없는 단순 문자열 치환이다.
- 파일 변경 자동 감지는 없다. 장기 실행 프로세스에서 즉시 반영하려면 재시작하거나 `reload()`를 호출해야 한다.
- prompt 파일 자체의 front matter나 JSON schema를 registry가 정적으로 검증하지 않는다. 출력 검증은 LLM Tool Executor의 Pydantic schema가 담당한다.
