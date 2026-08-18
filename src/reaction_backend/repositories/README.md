# `repositories/` — 데이터 접근 경계

이 패키지는 도메인별 SQLAlchemy 조회와 쓰기를 캡슐화한다. API·오케스트레이터는 가능한 한
ORM 쿼리를 직접 조립하지 않고 repository 메서드를 사용해 사용자 범위, 보관 필터,
트랜잭션 규칙을 한곳에서 유지한다.

## 모듈 지도

현재 23개 repository 모듈이 있다.

| 영역 | 모듈 | 책임 |
| --- | --- | --- |
| 사용자·개인정보 | [`user_repo.py`](user_repo.py), [`consent_repo.py`](consent_repo.py), [`privacy_repo.py`](privacy_repo.py), [`profile_repo.py`](profile_repo.py) | 사용자 조회, 동의 이력, 개인정보 export·익명화 경계, 행동·상호작용 프로필 |
| 온보딩·계획 | [`interview_repo.py`](interview_repo.py), [`goal_repo.py`](goal_repo.py), [`time_policy_repo.py`](time_policy_repo.py), [`fixed_schedule_repo.py`](fixed_schedule_repo.py), [`plan_draft_repo.py`](plan_draft_repo.py), [`scheduled_block_repo.py`](scheduled_block_repo.py) | 인터뷰 답변, 목표와 잠정 목표, 시간 정책·고정 일정, 계획 초안과 블록 |
| 오늘 실행·회복 | [`action_item_repo.py`](action_item_repo.py), [`execution_repo.py`](execution_repo.py), [`interruption_event_repo.py`](interruption_event_repo.py), [`recovery_repo.py`](recovery_repo.py) | 오늘 카드, 실행 이벤트와 상태 전이, 방해 이벤트, 회복 전략·시도 |
| 습관·인박스·브리프 | [`habit_repo.py`](habit_repo.py), [`habit_instance_repo.py`](habit_instance_repo.py), [`inbox_repo.py`](inbox_repo.py), [`daily_brief_repo.py`](daily_brief_repo.py) | 습관 정의와 주간 인스턴스, 인박스 항목, 일일 브리프 캐시 |
| 알림·리뷰·정책 | [`notification_repo.py`](notification_repo.py), [`notification_send_repo.py`](notification_send_repo.py), [`review_repo.py`](review_repo.py), [`policy_snapshot_repo.py`](policy_snapshot_repo.py) | 알림 설정·구독, 발송 이력, 기간 요약, 정책 snapshot |

## 세션과 트랜잭션 계약

Repository는 생성자에서 `AsyncSession`을 받아 해당 유스케이스의 세션을 공유한다. write
메서드는 새 객체의 식별자나 서버 기본값이 필요할 때 `flush()`할 수 있지만 commit과 최종
rollback은 호출자 책임이다. FastAPI 요청은 [`db/session.py`](../db/session.py)의
`get_db()`를 사용하고 scheduler는 job 단위 세션 scope를 만든다.

여러 repository를 묶는 상태 전이는 하나의 세션에서 수행한다. 중간 commit으로 부분 상태를
남기지 않으며 예외 시 유스케이스 경계에서 rollback한다. repository 내부에서 새 세션을
만들어 호출자의 트랜잭션을 분리하지 않는다.

## 핵심 불변조건

- 사용자 소유 행은 항상 `user_id` 범위로 조회한다. URL의 식별자만으로 다른 사용자의 행을
  읽거나 바꾸지 않는다.
- 지원되는 모델은 hard delete 대신 `archived_at`을 기록하며, 일반 목록과 단건 조회는 보관
  행을 제외한다.
- 민감한 메모와 토큰은 암호화 경계를 거친 뒤 `*_encrypted` 필드에 저장한다.
- 회복 선택은 `ActionItem.status`를 변경하지 않는다. 실행 상태 전이는
  [`execution_repo.py`](execution_repo.py)의 명시적 경로에서만 수행한다.
- 동의와 알림 발송 기록처럼 감사에 필요한 이력은 append-only 의미를 보존한다.
- 습관 인스턴스 생성은 `(habit_id, week_start)` 고유성을 지키고 재실행 가능한
  get-or-create 흐름을 사용한다.
- 반복 호출이 가능한 endpoint와 batch는 DB 고유 제약과 명시적 조회를 함께 사용해
  멱등성을 유지한다.

## 새 repository 또는 메서드를 추가할 때

1. 모델·도메인별 기존 repository에 둘 수 있는지 먼저 확인한다.
2. 입력에 사용자 범위와 필요한 시각 경계를 명시한다. 시간은 UTC 저장, KST 정책 계산 경계를
   혼용하지 않는다.
3. 일반 조회에는 `archived_at IS NULL` 조건을 포함하고, 보관 행 조회가 필요하면 메서드
   이름과 호출 목적을 분리한다.
4. write는 flush까지 수행하고 commit은 호출자에게 남긴다. 여러 쓰기의 원자성을 SQL 테스트로
   고정한다.
5. 외부 API나 LLM 호출을 repository에 넣지 않는다.

## 검증

Repository SQL 계약은 실제 쿼리 조건과 상태 전이가 흐트러지지 않도록 다음 테스트들에서
검증한다.

- [`tests/test_action_item_repo_sql.py`](../../../tests/test_action_item_repo_sql.py)
- [`tests/test_execution_repo_history_sql.py`](../../../tests/test_execution_repo_history_sql.py)
- [`tests/test_goal_repo_sql.py`](../../../tests/test_goal_repo_sql.py)
- [`tests/test_inbox_repo_sql.py`](../../../tests/test_inbox_repo_sql.py)
- [`tests/test_notification_send_repo_sql.py`](../../../tests/test_notification_send_repo_sql.py)
- [`tests/test_policy_snapshot_repo_sql.py`](../../../tests/test_policy_snapshot_repo_sql.py)
- [`tests/test_review_repo_sql.py`](../../../tests/test_review_repo_sql.py)
- [`tests/test_action_status_immutability.py`](../../../tests/test_action_status_immutability.py)

```bash
uv run pytest -v \
  tests/test_action_item_repo_sql.py \
  tests/test_execution_repo_history_sql.py \
  tests/test_goal_repo_sql.py \
  tests/test_inbox_repo_sql.py \
  tests/test_notification_send_repo_sql.py \
  tests/test_policy_snapshot_repo_sql.py \
  tests/test_review_repo_sql.py \
  tests/test_action_status_immutability.py
```

## 제약

- 일부 repository 메서드는 PostgreSQL SQL 표현과 제약조건을 전제로 하므로 SQLite로 모든
  동작을 대체 검증할 수 없다.
- repository는 권한 정책 전체를 대신하지 않는다. 인증된 사용자 식별자를 안전하게 전달하는
  책임은 API dependency에 있고, repository는 전달받은 범위를 쿼리에 강제한다.
- 반환되는 ORM 객체를 세션 종료 뒤 수정해 자동 저장될 것이라 가정하지 않는다.
