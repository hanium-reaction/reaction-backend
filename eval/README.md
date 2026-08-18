# `eval/` — 회복 선택 오프라인 평가 데이터

이 디렉터리는 참가자나 운영 로그 없이 반복 실행할 수 있는 L1 오프라인 평가 입력을
보관한다. 평가 설계와 지표 정의의 기준 문서는
[`docs/experiments/experiment-plan-v1.md`](../docs/experiments/experiment-plan-v1.md)다.

## 데이터셋

[`golden_recovery_cases.jsonl`](golden_recovery_cases.jsonl)은 합성 회복 상황 120건을 JSON
Lines 형식으로 저장한다. 한 줄이 한 케이스이며 모든 레코드에는 다음 키가 있다.

`case_id`, `block`, `synthetic`, `action_item`, `execution`, `failure_tags`, `context`,
`reflection_memo`, `assertions`, `design_intent`, `notes`

| 블록 | 건수 | 검증 목적 |
| --- | ---: | --- |
| `single_tag` | 52 | 실패 사유 13종 각각의 짧은/긴 카드와 오전/야간 조합 |
| `multi_tag` | 26 | 두 태그가 함께 들어올 때 전략 우선순위와 경합 |
| `uncovered_tag` | 12 | 과거 커버리지 공백 태그의 회귀 케이스 |
| `boundary` | 20 | overwhelm, 연속 실행 이력, 23시 경계, 태그 없음·과다 등 경계값 |
| `adversarial` | 10 | 자기비난 문구를 시스템 출력이 되풀이하지 않는지 |

`uncovered_tag`는 데이터 블록의 호환성용 이름이다. 현재 선택 규칙에서는 이 블록의
`TIME_SHORTAGE`, `OVERRUN`, `AVOIDANCE`도 실제 전략과 연결되므로 “현재 미지원 태그”로
해석하면 안 된다.

## 생성과 재현

프로젝트 루트에서 다음 명령으로 커밋된 파일을 재생성한다.

```bash
uv run python -m scripts.build_golden_recovery_cases
```

생성기는 난수와 현재 시각을 사용하지 않는다. 같은 코드에서는 같은 JSONL이 나와야 하며
[`tests/test_golden_recovery_cases.py`](../tests/test_golden_recovery_cases.py)가 생성기 출력과
커밋된 파일의 일치를 검증한다. 생성 규칙을 변경했다면 생성기와 결과 파일을 함께 갱신한다.

## 해석 규약

- 현재 파일의 모든 케이스는 `synthetic: true`다. 운영 사용자 데이터나 실제 성과라고
  보고하지 않는다.
- `design_intent`는 설계자의 기대를 기록한 참고값이지 정답 라벨이 아니다. 룰 엔진의
  정확도 근거로 단독 사용하지 않고 커버리지·패딩률 검토와 사람 라벨링의 출발점으로 쓴다.
- `assertions.must_not_contain`은 케이스별 추가 금지 표현만 담는다. 전역 금지어의 단일 진실
  소스는 [`safety/banned_words.py`](../src/reaction_backend/safety/banned_words.py)다.
- 적대적 입력의 `reflection_memo`에는 의도적으로 자기비난 표현이 들어갈 수 있다. 금지어
  검사는 사용자 입력을 검열하기 위한 것이 아니라 시스템이 생성한 출력에 적용된다.
- 평가 산출물이나 보고서에는 합성 데이터 비율, 실행 커밋, 선택 규칙 버전을 함께 남긴다.

## 관련 코드와 테스트

- 생성기: [`scripts/build_golden_recovery_cases.py`](../scripts/build_golden_recovery_cases.py)
- 선택 규칙 전수 열거: [`tests/test_recovery_selection_coverage.py`](../tests/test_recovery_selection_coverage.py)
- 기준 데이터 동기화: [`tests/test_recovery_catalog_sync.py`](../tests/test_recovery_catalog_sync.py)
- 파일 무결성과 재현성: [`tests/test_golden_recovery_cases.py`](../tests/test_golden_recovery_cases.py)
- 전역 금지어: [`tests/test_banned_words.py`](../tests/test_banned_words.py)

```bash
uv run pytest -v \
  tests/test_golden_recovery_cases.py \
  tests/test_recovery_selection_coverage.py \
  tests/test_recovery_catalog_sync.py \
  tests/test_banned_words.py
```

## 제약

- 이 데이터셋은 구조적 도달성·안전 규칙의 회귀를 빠르게 찾는 용도다. 실제 사용자 만족도,
  장기 회복률, 문구 자연스러움을 대신 측정하지 않는다.
- JSONL을 직접 손으로 수정하면 생성기와 불일치한다. 필요한 케이스는 생성기에 먼저 반영한다.
- 데이터 블록 이름은 역사적 맥락을 포함할 수 있으므로 현재 구현 상태는 테스트와 선택 규칙을
  함께 확인한다.
