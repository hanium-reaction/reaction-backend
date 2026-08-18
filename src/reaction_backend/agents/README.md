# `agents/` — 에이전트 경계 패키지

이 패키지는 오케스트레이터가 호출할 에이전트 구현을 둘 수 있는 경계다. 현재 코드에는
[`__init__.py`](__init__.py)만 있으며 독립 에이전트 클래스나 런타임 등록은 없다.
실제 인터뷰, 계획, 회복, 요약 흐름은
[`orchestrator/`](../orchestrator/README.md)가 담당하고 LLM 호출은
[`llm/`](../llm/README.md)을 통해 수행한다.

## 현재 책임

- `api`·`orchestrator`·`llm` 사이에서 향후 독립 실행 단위가 필요할 때 사용할 import 경계를
  제공한다.
- 현재 실행 흐름에 없는 에이전트가 이미 동작하는 것처럼 문서화하거나 이 패키지에서 직접
  외부 LLM SDK를 호출하지 않는다.

## 연계 규약

- 사용자 상태를 바꾸는 제안은 수락·수정·거절 가능한 HITL 흐름을 유지한다.
- LLM 제공자 호출, 비용 기록, 도구 실행은 [`llm/`](../llm/README.md)에 위임한다.
- 결정적 정책과 상태 전이는 도메인·오케스트레이터·repository 레이어에 둔다. 프롬프트
  출력만으로 데이터베이스를 갱신하지 않는다.
- 한국어 출력, 금지어, 알림 예산과 같은 잠금 정책은 기존 안전 레이어를 우회하지 않는다.
- 새 모듈은 `api -> orchestrator -> domain -> repositories` 의존 방향을 거슬러 import하지
  않는다.

## 구현을 추가할 때

1. 책임과 입력·출력 타입을 명확히 하고, 동일 정책의 기존 단일 진실 소스가 있는지 확인한다.
2. LLM이 필요하면 [`llm/provider.py`](../llm/provider.py)와 등록된 프롬프트를 사용한다.
3. 구조화 출력은 스키마로 검증하고 실패 시 결정적 fallback을 둔다.
4. 쓰기 작업은 명시적인 사용자 승인과 repository 계약을 통과시킨다.
5. 에이전트 단위 테스트와 오케스트레이터 통합 테스트를 함께 추가한다.

## 검증

현재 이 패키지 자체의 전용 테스트는 없다. 실제 LLM·오케스트레이션 계약은 다음 테스트가
검증한다.

- [`tests/test_orchestrator_handoff.py`](../../../tests/test_orchestrator_handoff.py)
- [`tests/test_interview_runner.py`](../../../tests/test_interview_runner.py)
- [`tests/test_provider_thinking.py`](../../../tests/test_provider_thinking.py)
- [`tests/test_llm_cost_accounting.py`](../../../tests/test_llm_cost_accounting.py)
- [`tests/test_banned_words.py`](../../../tests/test_banned_words.py)

```bash
uv run pytest -v tests/test_orchestrator_handoff.py tests/test_interview_runner.py
```

## 제약

- 현재 `agents/`는 빈 경계 패키지이며 애플리케이션 실행 경로에서 직접 사용되지 않는다.
- 새 에이전트의 필요성과 책임이 확정되기 전에는 현재 동작을 이 위치로 임의 이전하지 않는다.
