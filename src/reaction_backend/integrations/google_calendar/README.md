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
- `freebusy.py` — `freeBusy.query` + 날짜별 분해. `first_plan.busy_for_day` 의 다섯 번째
  소스로 배선돼 있다 (ADR-0009 D4).

⚠️ **60s TTL 캐시는 두지 않았다.** 계획 생성이 지평 전체를 `fetch_busy_by_day` 로 **한 번에**
조회하므로 generate 한 번이 API 를 한 번만 친다 — 캐시가 막을 반복 호출이 구조적으로 없다.
날짜마다 부르는 구조로 바꾸면 그때 다시 판단할 것.

## 후속

- **전이 버퍼**(외부 일정 앞뒤 이동 시간) — `busy_for_day` 에 직접. `pad_busy` 로 넣으면
  2차 패스가 무시한다 (ADR-0009 D4 ①).
- **부하 감쇠**(직전 연속 일정 길이 → 그 뒤 슬롯 허용 카드 길이) — ADR-0009 D4 ②.
- `events.py` — P1. 이 패키지는 아직 쓰기를 모른다.

## 규약

- **refresh token 은 최초 동의 때만 온다.** 갱신 응답의 None 을 저장하면 연결이 하루 뒤에
  조용히 죽는다 — `token_store.save` 가 None 이면 기존 값을 유지한다.
- 권한 박탈 / refresh 실패 → `revoked_at` set + 다음 진입 시 재연결 안내
  (`CALENDAR_NOT_CONNECTED`).
- 연결 해제는 **우리 DB 를 먼저 확정**하고 원격 회수는 그 뒤에 best-effort. 순서를 뒤집으면
  Google 은 끊겼는데 우리는 연결됐다고 믿는 상태가 생긴다.
- hard delete 금지 — 해제는 `revoked_at` (AGENTS §2).
