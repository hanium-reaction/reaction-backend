# P0 재실행 계약과 검증

기존 실행 시작/일시정지/재개, 복구 전략, 일정 승인, 체크인 계약을 재사용한다.
`reentry_attempts`는 첫 행동 수행의 자기 보고를 별도로 보관한다. 전체 과제 완료,
원래 실패 기록, recovery completion, 보상에는 첫 행동 보고만으로 쓰지 않는다.

## 사용자 상태

- prepare: 소유 과제를 잠그고 미종결 시도를 재사용한다. 실행 생성/과제 상태 변경 없음.
- initial/restart/resume은 시도 생성 당시 분류다. 이미 시작한 시도를 다시 여는 경우에는 executionStatus로 현재 상태를 표시한다.
- viewed: 클라이언트가 실제 화면을 표시한 후 보낸다. 시작 불가능한 미래/미승인 작업은 적격 노출로 찍지 않는다.
- start: 사용자가 행동을 명시적으로 확인한다. 기존 활성 실행을 재사용하거나 새 실행을 만들고 일시정지 구간을 닫는다. 과제 잠금 안에서 시도와 함께 커밋한다.
- first_step_completed: 서버 시각의 최초 보고만 저장. 현재 실행이 종결됐으면 거절한다. 반복 보고는 같은 시각을 반환한다.
- leave: 기존 실행을 일시정지하며 첫 행동 기록과 시도를 보존한다.
- 전체 과제 완료: 기존 `/today/check-ins`만 사용한다. 종결된 실행의 시도는 다음 prepare에서 닫히고 새 시도를 만든다.

복구 파생 작업은 열린 승인 일정이 없으면 시작할 수 없고, 미래 작업/시각도 막는다.
이것은 사용자 주도 실행 경로이며 자동 복구·푸시·AI 자동 적용을 추가하지 않는다.
준비 화면에서 편집한 행동은 암호화 저장하며 기존 익명화 경로에서 제거한다.

## 계측

created_at은 준비 요청, viewed_at은 적격 실제 노출, selected_at/started_at은 서버에서 확인한 선택/시작이다.
step_completed_at은 자기 보고다. 기존 execution 결과에 연결해 전체 완료를 구분할 수 있다.
`scripts/reentry_metrics.py`는 읽기 전용 기술 통계를 출력한다. 사용자별 최초 적격 노출,
10분 관찰 창(분석용 초깃값), 미착수 포함, 관찰 창 미완료 분리를 실행 테스트로 검증한다.
이는 무작위 A/B 배정이나 현실 행동 증명의 구현이 아니다. 본 실험의 배정·기간·최소 효과는 별도 확정해야 한다.

## 순서와 롤백

1. 추가 테이블 마이그레이션과 백엔드 PR을 먼저 검토·적용한다.
2. 기존 클라이언트는 새 API를 호출하지 않으므로 유지한다.
3. 프론트의 `VITE_REENTRY_P0=true` 빌드를 시험 환경에서 검증한 뒤 활성화한다. 기본값은 꺼짐이다.
4. 되돌릴 때 프론트 기능을 끄고 기존 실행 경로를 사용한다. 운영 기록이 있는 테이블 downgrade는 차단한다. 빈 시험 DB만 downgrade 가능하다.

## 시험 환경

`tests/test_reentry.py`: 실제 라우터 + 모의 저장소의 계약/오류/재시도.
`tests/test_reentry_sql.py`: PostgreSQL, 실제 저장소, rollback-only HTTP 트랜잭션.
기존 CI는 별도 PostgreSQL에 migration과 전체 테스트를 실행한다.

로컬 프론트 연결 검증은 `REENTRY_TEST_SERVER=1 uv run uvicorn tests.integration.reentry_server:app --host 127.0.0.1 --port 8019`로 실행한다.
이 전용 어댑터는 테스트 인증/메모리 저장소이며 운영 배포에 사용하지 않는다.
프론트는 `REACTION_API_TARGET=http://127.0.0.1:8019`로 Vite를 실행하고
`/tests/integration/reentry.html`에서 실제 화면과 HTTP 클라이언트를 검증한다.
외부 LLM·실사용자 데이터는 사용하지 않는다.
