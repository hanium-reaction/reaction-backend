# `domain/` — 프레임워크 독립 도메인 규칙

이 패키지는 HTTP, FastAPI, SQLAlchemy에 의존하지 않는 순수 정책을 보관한다. 현재 구현된
모듈은 [`action_cancel.py`](action_cancel.py) 하나이며 오늘 카드의 취소 가능 여부와 거절
사유를 단일 진실 소스로 제공한다.

## `action_cancel.py`

공개 계약은 다음과 같다.

- `CANCELLABLE_STATUS = "planned"`
- `CANCELLABLE_SOURCES = {"inbox", "manual"}`
- `is_cancellable(status, source, has_execution_history) -> bool`
- `rejection_reason(status, source, has_execution_history) -> str | None`

카드는 아래 세 조건을 모두 만족할 때만 취소할 수 있다.

1. 상태가 `planned`다.
2. 출처가 `inbox` 또는 `manual`이다.
3. 실행 이력이 없다.

상태가 달라졌거나 실행 이력이 있으면 이미 시작한 일이라는 안내를 반환한다. 출처가 계획,
습관, 회복 등 취소 허용 목록 밖이면 계획에 묶인 카드라는 안내를 반환한다. 취소 가능한
경우 `rejection_reason()`은 `None`이다.

이 판정은 [`GET /today/agenda`](../api/routes/today.py)가 내려주는 `cancellable` 값과
[`POST /today/actions/{id}/cancel`](../api/routes/today.py)의 서버 가드가 함께 사용한다.
표시와 실제 상태 전이의 규칙이 갈리지 않도록 새 조건은 이 모듈에 한 번만 추가한다.

## 규약

- ORM 모델, 요청 객체, 전역 설정을 받지 않고 필요한 원시값만 입력으로 받는다.
- 규칙 함수는 데이터베이스를 읽거나 쓰지 않고 같은 입력에 같은 결과를 반환한다.
- 사용자가 이미 시작한 카드의 실행 이력은 삭제하지 않는다. 취소 대신 체크인·회고 흐름을
  사용한다.
- 반환 문구는 사용자 화면에 노출될 수 있으므로 한국어 톤과 금지어 정책을 지킨다.
- 도메인 규칙을 라우트나 프런트엔드에 복제하지 않는다.

## 검증

[`tests/test_action_cancel.py`](../../../tests/test_action_cancel.py)는 허용 출처, 상태, 실행 이력
조합과 거절 문구를 검증한다. 라우트 통합 동작은
[`tests/test_today.py`](../../../tests/test_today.py)에서 함께 확인한다.

```bash
uv run pytest -v tests/test_action_cancel.py tests/test_today.py
```

## 제약

- 현재 이 패키지가 제공하는 도메인 규칙은 카드 취소 정책뿐이다.
- 계획·습관·회복에서 파생된 카드의 취소는 주간 계획, 습관 집계, 회복 지표와 연결되므로
  이 함수의 허용 목록을 넓히는 것만으로 지원할 수 없다. 연관 상태 전이와 테스트를 함께
  설계해야 한다.
