# `integrations/google_calendar/` — Google Calendar (읽기 전용)

MVP 스코프: **read-only freebusy**. write-back(`events.insert`)은 P1.

스코프는 `https://www.googleapis.com/auth/calendar.freebusy` **하나**다. 스케줄러의 세 룰
(전이 버퍼·부하 감쇠·자투리)에 필요한 건 구간의 **길이와 인접성뿐**이고 제목·장소는 필요
없다 (ADR-0009 D4). `calendar.readonly` 로 넓히는 건 개인정보 범위를 넓히는 일이라 ADR 을
먼저 고쳐야 한다 — `tests/test_calendar_connect.py` 가 스코프 문자열을 고정한다.

## 지금 있는 것

- `oauth.py` — authorization code → 토큰 교환, refresh, revoke.
  `google-api-python-client` 를 쓰지 않는다(동기·무거움). 필요한 건 토큰 엔드포인트 POST
  하나뿐이라 `requests` + `to_thread` + 이중 timeout 으로 감싼다 — `web_fetch/fetcher.py`
  · `web_push/sender.py` 와 같은 관례이고 **새 의존성이 0** 이다.
- `token_store.py` — `calendar_connections` 읽기/쓰기. 평문 토큰이 이 모듈 밖으로 나가지
  않게 저장은 전부 `encrypt_oauth_token` 경유.
- `freebusy.py` — `freeBusy.query` + 날짜별 분해. 첫 계획(`first_plan.busy_for_day`)과
  주간 재계획(`POST /plans/replan` 의 `committed_busy`)의 다섯 번째 소스로 배선돼 있다
  (ADR-0009 D4).

## 캘린더를 언제 읽나

일정을 DB 에 복사하지 않는다 — **필요한 순간에 읽는다.** webhook 은 없다(`events.watch` 는 일정
제목까지 읽는 스코프가 필요해 ADR-0009 D4 범위 밖).

| 시점 | 범위 | 하는 일 |
| --- | --- | --- |
| `POST /plans/generate` · `POST /plans/mandala/next-cycle` | 계획 지평 전체를 한 번 | 캘린더를 피해 배치 |
| `POST /plans/replan` | 재배치 창 전체를 한 번 (60일 상한 — 넘으면 `warnings`) | 캘린더를 피해 재배치 |
| `GET /today/agenda` · `GET /plans/weekly` | 오늘 / 그 주 — **5분 캐시 · 2초 상한** | 이미 승인한 블록의 겹침 표시(`calendarConflict`) |
| 06:00 모닝 브리프 cron | 오늘 | 겹치는 카드를 `adjustment_hints` 맨 앞에 |
| `GET /calendar/freebusy` | 요청 구간 | 그대로 반환 |

겹쳐도 **옮기지 않는다**(자동 적용 금지) — 판정은 `domain/calendar_conflict.py` 하나.
access token(약 1시간)은 조회 시점에 만료 60초 전이면 그때 갱신한다(별도 갱신 cron 없음).

캐시는 화면 조회(`fetch_busy_for_screen`)에만 있다 — 계획 생성은 지평 전체를 한 번 읽어 반복 호출이
없다. 프로세스 메모리라 워커마다 따로이고(단일 인스턴스 전제), **실패는 캐시하지 않으며**,
연결·해제 직후 `clear_screen_cache(user_id)` 로 비운다.

## 후속

- **전이 버퍼**(외부 일정 앞뒤 이동 시간) — `busy_for_day` 에 직접. `pad_busy` 로 넣으면
  2차 패스가 무시한다 (ADR-0009 D4 ①).
- **부하 감쇠**(직전 연속 일정 길이 → 그 뒤 슬롯 허용 카드 길이) — ADR-0009 D4 ②.
- `events.py` — P1. 이 패키지는 아직 쓰기를 모른다.

## 연결 흐름 (FE ↔ BE)

1. FE 가 GIS `google.accounts.oauth2.initCodeClient({ scope: calendar.freebusy, ux_mode: 'popup' })`
   로 동의 팝업을 띄워 authorization code 를 받는다.
2. `POST /calendar/connect {code}` — BE 가 `redirect_uri=postmessage` 로 교환한다
   (`GOOGLE_OAUTH_REDIRECT_URI` 가 비어 있을 때의 기본값). 콘솔에 리디렉션 URI 등록 불필요.
3. `GET /calendar/connect` 로 상태를 다시 그린다.

켜려면(사람 손): Cloud 콘솔에서 **Calendar API 사용 설정** + 동의 화면에 `calendar.freebusy`
스코프 추가 + 웹 client 의 **승인된 JavaScript 원본**에 FE 도메인. 서버 `.env` 에
`GOOGLE_CALENDAR_ENABLED=true` · `GOOGLE_OAUTH_CLIENT_SECRET` — 손으로 넣지 말고
`calendar-oauth.yml` 을 쓴다: `mode=check` 가 GitHub secret(`STAGING_GOOGLE_OAUTH_CLIENT_SECRET`)
을 값 노출 없이 라이브 client_id 와 대조하고(`invalid_grant`=짝 맞음 · `invalid_client`=틀림),
`mode=enable` 이 통과 시에만 `.env` 에 기록·재기동, `mode=disable` 이 롤백. 동의 화면이 **테스트** 상태면
테스트 사용자만 연결할 수 있고 refresh token 이 7일 뒤 만료된다(그 뒤 재연결 안내로 떨어진다).

## 규약

- **refresh token 은 최초 동의 때만 온다.** 갱신 응답의 None 을 저장하면 연결이 하루 뒤에
  조용히 죽는다 — `token_store.save` 가 None 이면 기존 값을 유지한다. 연결(`POST /connect`)
  에서 안 오면: 살아 있는 연결이 있으면 그 값을 쓰고, 없으면 동의를 회수해 다음 시도가
  refresh token 을 받게 한다.
- 동의 화면에서 캘린더 체크를 풀면 교환은 성공하지만 스코프에서 빠진다 — 저장하지 않는다.
- 권한 박탈 / refresh 실패 → `revoked_at` set + 다음 진입 시 재연결 안내
  (`CALENDAR_NOT_CONNECTED`).
- **`freebusy` 는 commit 하지 않는다(flush 까지).** 계획 생성·재계획이 트랜잭션 단위
  advisory lock(`user_agent_lock`) 안에서 부르기 때문에, 여기서 commit 하면 lock 이 도중에
  풀린다. 갱신 토큰·회수 표시는 호출자의 commit 에 실린다 — lock 없는 조회 라우트는 스스로
  commit 한다.
- 연결 해제는 **우리 DB 를 먼저 확정**하고 원격 회수는 그 뒤에 best-effort. 순서를 뒤집으면
  Google 은 끊겼는데 우리는 연결됐다고 믿는 상태가 생긴다.
- hard delete 금지 — 해제는 `revoked_at` (AGENTS §2).
