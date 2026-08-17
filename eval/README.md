# `eval/` — 오프라인 평가 데이터셋

참가자 없이(사람 0명) 돌리는 L1 오프라인 평가의 입력이 여기 산다.
설계·지표 정의는 [`docs/experiments/experiment-plan-v1.md`](../docs/experiments/experiment-plan-v1.md) 가 단일 진실 소스다.

## `golden_recovery_cases.jsonl` — 회복 골든셋 120건

| 블록 | 건수 | 무엇을 보는가 |
|---|---|---|
| `single_tag` | 52 (13태그 × 4) | 태그당 짧은/긴 카드 × 오전/야간 블록 |
| `multi_tag` | 26 (13 × 2) | 함께 선택될 만한 2태그 조합에서 전략이 경합하는지 |
| `uncovered_tag` | 12 (3 × 4) | `TIME_SHORTAGE`/`OVERRUN`/`AVOIDANCE` — 현 시드에서 어떤 전략에도 안 걸리는 태그 |
| `boundary` | 20 | overwhelm 3/4/5, 연속실패 2/3/5, 23시 근접, 이력 0, 태그 미선택, 계약 위반(3태그) 등 |
| `adversarial` | 10 | 자기비난 회고 — 시스템이 그 프레임을 되받아 쓰는지 |

### 생성·재현

```bash
uv run python -m scripts.build_golden_recovery_cases
```

난수도 현재시각도 쓰지 않는다. 같은 커밋은 항상 같은 파일을 만들고,
`tests/test_golden_recovery_cases.py::test_file_on_disk_matches_the_generator` 가
**커밋된 파일 == 생성기 출력**을 고정한다. 생성기를 고쳤으면 파일도 다시 생성해 커밋할 것.

### 읽을 때 주의할 것

- **전 케이스 `synthetic: true`.** 라이브 `recovery_attempts` 가 0건이라(2026-08-17 실측)
  실 로그를 한 건도 쓰지 못했다. 보고서에 합성 비율을 반드시 명시한다.
- **`design_intent` 는 정답이 아니다.** 설계자가 쓴 것이므로 룰 엔진의 "정확도"로 쓰면
  자기충족적이다. 패딩률·커버리지 같은 **구조적 지표**와 사람 라벨링의 출발점으로만 쓴다.
- **`assertions.must_not_contain` 은 케이스 고유 추가분뿐이다.** 전역 금지어는 저장하지
  않고 `safety/banned_words.BANNED_REPLACEMENTS` 에서 읽는다 — 사전이 두 곳으로 갈라지면
  한쪽만 고쳐지기 때문이다.
- 적대적 케이스의 회고 문구에는 금지어(`실패했`, `한심` 등)가 **일부러 들어 있다.**
  금지어 필터는 **시스템 출력**에만 걸린다 — 사용자가 쓴 말을 검열하는 장치가 아니다.

## 관련 테스트

| 파일 | 무엇을 고정하나 |
|---|---|
| `tests/test_golden_recovery_cases.py` | 골든셋 무결성 — 블록별 건수, 재현성, 적대적 케이스가 실제로 적대적인지 |
| `tests/test_recovery_selection_coverage.py` | 룰 엔진 도달 공간 **전수 열거**(92개 입력) — PARK 도달 불가, 카드 수 2~3장, 패딩률 |
| `tests/test_recovery_catalog_sync.py` | 시드 ↔ conftest 미러 ↔ 설계 3자 동기화 |
