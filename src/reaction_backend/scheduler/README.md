# `scheduler/` — KST 기반 배치와 알림 sweep

이 패키지는 APScheduler의 in-process `AsyncIOScheduler`로 주기 작업을 등록하고, 사용자별
또는 전역 sweep을 실행한다. 애플리케이션 lifespan은 `SCHEDULER_ENABLED=true`일 때만
[`runtime.build_scheduler()`](runtime.py)을 시작한다. 설정 기본값은 `false`다.

## 현재 등록된 작업

모든 CronTrigger는 KST를 기준으로 한다. `build_scheduler()`가 등록하는 작업은 정확히 9개다.

| job id | 주기 | 실제 동작 |
| --- | --- | --- |
| `morning_brief` | 매일 06:00 | 활성 사용자별 오늘 브리프 생성·캐시 |
| `weekly_review` | 일요일 03:00 | 활성 사용자별 주간 KPI와 리뷰 precompute |
| `interruption_resolver` | 6시간마다 | 6시간 이상 미확정인 방해 이벤트를 미재개로 종결 |
| `expire_drafts` | 6시간마다 | 만료 시각을 지난 `draft` 계획 초안을 `expired`로 변경 |
| `expire_reflections` | 매일 04:00 | 회고 창을 지난 미회고 카드 정리 후 연결된 미완주 회복을 `abandoned`로 종결 |
| `expire_proposed_goals` | 매일 04:00 | TTL을 넘긴 `proposed` 목표 보관 |
| `habit_instances` | 매일 00:05 | 활성 습관의 현재 주 인스턴스를 get-or-create |
| `evening_reflection_notify` | 19~23시, 5분마다 | 사용자 설정 시각과 pending 회고를 확인해 발송 게이트 경유 알림 |
| `pre_card_notify` | 종일 5분마다 | 2~7분 뒤 시작하는 opt-in 카드 사전 알림 |

`expire_reflections` job 하나가 `run_expire_unreflected_cards()`와
`run_abandon_stale_recoveries()`를 순서대로 실행한다. `habit_instances`는 주 1회가 아니라
매일 실행한다. 메모리 job store는 월요일 실행 시각에 앱이 꺼져 있으면 다음 주까지 기회를
놓칠 수 있기 때문에, 고유 제약 기반 get-or-create를 매일 재시도해 하루 안에 보완한다.

사용자 익명화와 OAuth token refresh는 현재 `build_scheduler()`에 등록된 작업이 아니다.
알림 dispatcher도 별도 queue job이 아니며 두 알림 sweep이
[`safety/push_gate.py`](../safety/push_gate.py)를 거쳐 직접 발송한다.

## 모듈 지도

- [`runtime.py`](runtime.py): scheduler 생성, 9개 CronTrigger 등록, job별 세션과 repository
  조립.
- [`sweeps.py`](sweeps.py): 활성 사용자 전체를 순회하는 morning brief와 weekly review
  wrapper. 사용자별 예외를 격리한다.
- [`morning_brief.py`](morning_brief.py): 사용자 한 명의 룰 기반 브리프와 LLM fallback.
- [`weekly_review_precompute.py`](weekly_review_precompute.py): 주간 KPI와 기간 요약 upsert.
- [`interruption_resolver.py`](interruption_resolver.py): 오래 열린 방해 이벤트 종결.
- [`expire_drafts.py`](expire_drafts.py): 72시간 응답 없는 계획 초안 만료.
- [`expire_reflections.py`](expire_reflections.py): 회고 가능 경계 밖 카드와 회복 시도 정리.
- [`expire_proposed_goals.py`](expire_proposed_goals.py): 14일이 지난 잠정 목표 보관.
- [`habit_instances.py`](habit_instances.py): 활성 사용자와 습관을 순회해 주간 인스턴스 생성.
- [`notify_sweeps.py`](notify_sweeps.py): 저녁 회고와 카드 사전 알림 후보 선택·발송.

## 실행과 트랜잭션

```bash
SCHEDULER_ENABLED=true uv run uvicorn reaction_backend.main:app
```

`runtime._session_scope()`는 요청 밖에서 job마다 새 `AsyncSession`을 열고 예외 시 rollback한다.
각 job의 commit 위치는 구현 계약에 맞게 다르다. 전역 resolver는 wrapper가 commit하고,
만료 함수 일부는 내부 commit하며, 사용자 sweep은 사용자 단위 commit/rollback으로 실패를
격리한다. 새 작업은 기존 함수의 commit 계약을 확인해 이중 commit이나 부분 저장을 만들지
않아야 한다.

## 핵심 규약

- scheduler는 정확히 한 번 실행을 보장하지 않는다. 모든 작업은 재실행과 중복 실행에
  안전하도록 조회 조건, upsert/get-or-create, DB 고유 제약을 사용한다.
- 시각 정책은 KST로 계산하고 DB timestamp는 UTC로 저장한다. 주 경계는 agenda 조회와 같은
  `current_week_start_kst()`를 재사용한다.
- 알림은 [`safety/push_gate.py`](../safety/push_gate.py)의 quiet hours, 사용자 opt-in,
  주간 예산, 클래스별 중복 방지를 우회하지 않는다.
- 한 사용자의 실패가 전체 sweep을 중단하지 않게 격리하되, 실패 원인을 로그에 남긴다.
- 회복 만료 작업은 recovery 선택 과정에서 `ActionItem.status`를 임의 변경하지 않는다.
- cron에 새 함수를 등록할 때 job id, KST 시각, idempotency 근거, 트랜잭션 경계를 함께
  문서화하고 테스트한다.

## 검증

- 등록 job과 시간: [`tests/test_scheduler.py`](../../../tests/test_scheduler.py)
- 활성 사용자 sweep: [`tests/test_scheduler_sweeps.py`](../../../tests/test_scheduler_sweeps.py)
- 알림 후보·게이트: [`tests/test_notify_sweeps.py`](../../../tests/test_notify_sweeps.py),
  [`tests/test_push_gate.py`](../../../tests/test_push_gate.py)
- 습관 인스턴스: [`tests/test_habit_instances_sweep.py`](../../../tests/test_habit_instances_sweep.py)
- 만료 작업: [`tests/test_reflection_expiry.py`](../../../tests/test_reflection_expiry.py),
  [`tests/test_plan_draft_expiry.py`](../../../tests/test_plan_draft_expiry.py),
  [`tests/test_proposed_goal_expiry.py`](../../../tests/test_proposed_goal_expiry.py)

```bash
uv run pytest -v \
  tests/test_scheduler.py \
  tests/test_scheduler_sweeps.py \
  tests/test_notify_sweeps.py \
  tests/test_habit_instances_sweep.py \
  tests/test_reflection_expiry.py \
  tests/test_plan_draft_expiry.py \
  tests/test_proposed_goal_expiry.py \
  tests/test_push_gate.py
```

## 운영 제약

- 현재 job store는 프로세스 메모리에 있다. 재시작 후 과거 실행 시각을 영구 복구하지 않는다.
- 앱 인스턴스마다 scheduler가 하나씩 기동되므로 여러 인스턴스에서 설정을 켜면 같은 job이
  중복 실행된다. DB·발송 게이트가 안전망을 제공하지만 비용과 부하를 줄이려면 scheduler를
  켠 인스턴스를 하나로 운영한다.
- `SCHEDULER_ENABLED=false`인 환경에서는 이 패키지의 cron이 자동 실행되지 않는다. 필요한
  만료·생성 동작이 다른 외부 scheduler에서 호출되는지 별도로 확인해야 한다.
