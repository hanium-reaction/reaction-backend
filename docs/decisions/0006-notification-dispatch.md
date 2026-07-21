# ADR-0006: Web Push 발송 — 게이트 직접발송 · 예산 해석 · 발송 이력 테이블

- 상태: 승인 (2026-07-21)
- 관련: Issue #20 (알림 cron 2종), #16 (Web Push 구독), #24 (스케줄러 운영)
- 구현: `safety/push_gate.py` · `integrations/web_push/` · `scheduler/notify_sweeps.py` ·
  `notification_sends` 테이블

## 배경

베이스라인 §1.4 잠금: "알림: 주 ≤ 3건, 3 클래스만 (morning_brief / pre_card /
evening_reflection)" (AGENTS.md §1) + "23~07시 자동 푸시 금지" + "같은 클래스 24h 중복
금지" (architecture.md §3). 그러나 **적용 의미가 문서에 미정의**인 지점이 셋 있었고,
enforce 에 필요한 상태 저장 테이블이 설계서 v0.7.1 에 없었다. 이 ADR 은 그 해석을
박제한다. 잠금 문구 자체의 변경이 아니라 **문면 그대로의 구현 해석**이다 — 재해석이
필요하면 이 문서를 PR 로 고친다.

## 1. 큐+dispatcher 대신 단일 게이트 직접발송

`scheduler/README.md` 의 `notification_dispatcher`(5분 폴, 예약 알림 발송)와 ADR-0005 §7
"알림 큐 단계에서 enforce"는 **큐 테이블 + 소비자** 모델을 시사했다. MVP 는 큐를 두지
않는다:

- 발송 시점 판단(19~23시 5분 폴)이 이미 cron 에 있어, 큐에 넣고 5분 뒤 다시 꺼내면
  지연만 늘고 정확성이 나아지지 않는다.
- ADR-0005 의 실제 요구는 "enforce 지점이 하나일 것"이다 — 큐가 아니라 **게이트 함수**
  (`push_gate.send_push`)로 충족한다. 모든 발송 경로(cron 2종, 이후 morning_brief 푸시)가
  이 함수를 거치고, `WebPushSender` 직접 호출은 금지다(패키지 docstring).
- 나중에 재시도·스케줄 발송이 필요해지면 게이트 뒤에 큐를 끼워도 호출부는 불변.

## 2. "주 ≤ 3건" = 사용자별 · 전 클래스 합산 · rolling 7일 · 실발송만

- **합산**: AGENTS.md §1 은 3 클래스를 한 문장에 묶어 상한을 하나만 둔다. ADR-0005 §7 도
  morning_brief 푸시가 "주 ≤ 3건 budget 체크"를 거친다고 명시 — 클래스별 예산이라는
  독해를 지지하는 문구는 레포 어디에도 없다.
- 산술 귀결을 직시한다: **cron 은 매일 시도하지만 사용자가 실제 받는 푸시는 주 3건**이다.
  회고 알림도 주 3회를 넘지 못한다. 이것은 버그가 아니라 알림 피로 최소화라는 제품
  결정의 문면 그대로다. 완화(예: evening 면제, pre_card 한정)가 필요하면 잠금 변경
  절차(합의)로 — 코드로 우회하지 않는다.
- **rolling 7일**: 달력 주 기준은 주 경계에서 최대 6건 몰림을 허용한다. "주 ≤ 3건"의
  의도(빈도 상한)에 rolling 이 부합.
- **실발송만 카운트**: 게이트에 막힌 시도가 예산을 소모하면 한 건도 못 받은 사용자의
  예산이 바닥나는 모순.

## 3. "같은 클래스 24h 중복 금지" = KST 달력일 1건

rolling 24h 로 구현하면 매일 같은 시각대 cron 이 **래칫**된다: 어제 21:03 발송 → 오늘
21:00 폴은 23h57m < 24h 로 차단 → 21:05 발송 → 내일 21:10 … 규칙의 의도("하루 두 번
보내지 마라")를 지키면서 래칫이 없는 구현이 KST 달력일 dedup 이다.

경계 케이스: 어제 23시 근처 발송 + 오늘 아침 발송이 24h 미만 간격일 수 있으나, 23~07
금지가 야간 발송 자체를 막아 실제로는 발생 구간이 좁다.

## 4. 저녁 회고 알림은 "회고할 카드가 있을 때만"

빈 회고 화면으로 부르는 푸시는 소음이고, 주 3건 예산에서 진짜 회고 기회를 밀어낸다.
pending 판정은 회고 화면·만료 cron 과 **같은 경계**(`pending_reflection_since` +
`_reflectable_from`)를 재사용한다 — 알림을 받고 들어왔는데 화면이 비는 불일치를
구조적으로 차단.

## 5. `notification_sends` 테이블 (설계서 외 추가)

주 3건·클래스 dedup 은 "이미 보낸 이력"이 있어야 enforce 가능하고, 재시작·다중
인스턴스에서 성립하려면 DB 여야 한다. 설계서 v0.7.1 에 해당 테이블이 없음을 확인
(erd-diff.md — 발송 로그·budget 추적 테이블 부재). plan_drafts·user_consents 와 같은
'보존한 개선' 선례로 추가한다. INSERT only (`llm_runs` 원칙).

컬럼: `id · user_id(FK CASCADE) · notification_class(3값 CHECK) · sent_at` +
timestamps. 인덱스 `(user_id, sent_at)` — 게이트 조회 2종이 전부 이 범위 스캔.

## 6. VAPID 미설정 = 조용한 degrade · 키는 EC2 에서만 생성

`GEMINI_API_KEY` 부재 패턴과 동일: 키 없으면 발송만 `unconfigured` 로 skip, 앱·cron 은
정상. 라이브 키는 `provision-vapid.yml` 워크플로가 **EC2 러너 위에서 생성해 .env 에
직접 기록**한다 — private key 가 레포·Actions 로그·로컬 어디에도 존재하지 않는다.
public key 만 워크플로 출력으로 노출 (FE `applicationServerKey` 용 공개값).

## 7. 경계 명세

- quiet hours = `[23:00, 07:00)` — 23:00 정각 금지, 07:00 정각 허용.
  `eveningReflectionTime=23:00` 설정 사용자의 회고 알림은 발송되지 않는다 (§15 명시,
  FE 는 22:55 상한 권장).
- pre_card 리드타임 = 2~7분 전 (2분 리드 + 5분 폴, architecture.md §6 "2분 전"의 구현).
  `started` 블록 제외, 카드 archived 제외, 비활성 사용자 제외.
- 클래스 화이트리스트 밖의 발송 요청은 `ValueError` — 조용히 보내지 않는다.

## 검증

- `tests/test_push_gate.py` — 잠금 3규칙 경계값 (23:00/22:59/06:59/07:00, 4번째 발송
  차단, 래칫 회귀, 예산 미소모)
- `tests/test_notify_sweeps.py` — sweep 층 (시각 존중, pending 조건, opt-in, 실패 격리)
- `tests/test_notification_send_repo_sql.py` — fake 전면대체 함정 대응: 실 SQL
  literal_binds (예산 카운트 무클래스필터 · pre_card 후보 활성사용자 3조건)
- `tests/test_scheduler_sweeps.py` — job 등록 + 트리거(시각·폴 간격·타임존) 고정
