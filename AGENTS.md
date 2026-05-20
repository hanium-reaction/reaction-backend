# AGENTS.md — re:action backend 작업 규칙

> Claude Code / Codex / Cursor 등 코드 에이전트는 작업 시작 시 이 문서를 먼저 읽어 주세요.
> 사람 기여자는 [`README.md`](README.md) 의 기여 가이드만 봐도 충분합니다.
> 진실 소스: `Reaction_DevBaseline_v1.0_2026-05-15` + `Reaction_DB_설계서_v0.7.1`. swagger.yaml v0.2.0은 **폐기**.

---

## 0. 한 줄 요약

> "이건 단순 CRUD가 아니라 **AI 에이전트 + 4계층 메모리** 시스템이다. HITL을 우회하지 말고, 금지어 필터를 끄지 말고, 정책 위반 블록을 만들지 말고, 원본 `action_item.status` 를 절대 변경하지 말 것."

---

## 1. 잠금된 제품 결정 (DevBaseline §1.4)

이 결정들은 **PR 코멘트로 의문 제기 가능, 코드로 우회 불가**. 우회하는 코드를 작성하지 말 것.

- 회복 시점: 21시 일괄 회고만. 실패 직후 자동 푸시·자동 회복 X.
- 누적 정책: 미회고 카드 최대 3일, 그 이후 `system_failure_reason='reflection_skipped'` 자동 만료.
- AI 출력 = **Draft Layer + [수락/수정/거절] 3버튼**. 자동 적용 금지 (Calendar write 포함).
- 회복 옵션 = if-then 코핑 플랜 (UX 4 그룹 / 내부 9 전략).
- 톤: "Be on your side, not on your case". 금지어 후처리 필터 강제.
- 알림: 주 ≤ 3건, 3 클래스만 (morning_brief / pre_card / evening_reflection).
- 익명화: 90일 비활성 자동, 매일 04:00 KST cron.
- 캘린더 MVP: read-only freebusy. write-back은 P1.
- 한국어 only (MVP). 다국어는 P3.
- Focus 최대 3, Maintain 최대 5, Parked 자유.
- 실패 사유 최대 2개 (13종 enum).
- 시간 저장 UTC, 응답 KST(+09:00).

## 2. 절대 하지 말 것

- `main` 에 직접 push / force push 하지 않는다.
- `uv.lock` 손 수정 금지. 의존성 변경은 `uv add` / `uv remove` 만.
- `requirements.txt` 신설 금지. `pip install` 금지 (lock과 어긋남).
- 폴더 구조나 패키지 이름을 임의로 바꾸지 않는다 (re:action 아키텍처가 폴더에 매핑되어 있음).
- `.env` (실제 환경변수) 커밋 금지. `.env.example` 만 커밋.
- **응답 envelope/에러 형태를 임의로 변경하지 않는다.** [`docs/api-contract.md`](docs/api-contract.md) PR과 동반.
- **원본 `action_item.status` 를 회복 결정으로 변경하지 않는다.** Resilience 지표의 전제 조건.
- LLM SDK (genai / google-generativeai 등) 를 라우터/에이전트에서 직접 import 하지 않는다. 모두 [`src/reaction_backend/llm/`](src/reaction_backend/llm/) Tool Executor 경유.
- 금지어 필터를 우회하거나 비활성화하지 않는다.
- 토큰/메모/refresh token을 평문으로 저장하지 않는다 (`*_encrypted` 컬럼).
- hard delete (`DELETE FROM ...`) 금지. soft delete (`archived_at = now()`).
- 자동 적용 코드 작성 금지 — 모든 변경은 사용자 명시 승인 (HITL) 이후.
- cron을 idempotent 하지 않게 작성하지 않는다 (다회 실행해도 안전해야 함).

## 3. 항상 할 것

- 새 작업은 새 branch 에서. `feat/<slug>`, `fix/<slug>`, `chore/<slug>`, `docs/<slug>`.
- 코드 변경 후 PR 전 로컬 검증:

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy src
  uv run pytest -v
  ```

- 새 의존성은 `uv add <pkg>` (런타임) / `uv add --dev <pkg>` (개발). `pyproject.toml` + `uv.lock` 함께 커밋.
- 새 endpoint 추가 시 [`docs/api-contract.md`](docs/api-contract.md) 갱신, 같은 PR.
- 모든 응답은 직접 객체 반환 (envelope 없음), 에러만 `ErrorResponse` envelope.
- 시간 응답은 [`schemas/common.py`](src/reaction_backend/schemas/common.py) 의 `now_kst()` 사용.
- 새 LLM 호출은 `llm/` Tool Executor 통해서. prompt는 `prompts/<domain>/<name>.v1.md`.
- 새 cron 추가 시 [`scheduler/README.md`](src/reaction_backend/scheduler/README.md) 의 시간표 갱신.
- Idempotency-Key 가 필요한 4 endpoint 는 [`docs/api-contract.md` §1.7](docs/api-contract.md) 표에 따라 헤더 검사.

## 4. 어디에 무엇을 넣는가 (폴더 가이드)

| 추가하려는 것 | 위치 |
| --- | --- |
| 새 endpoint | `src/reaction_backend/api/routes/<domain>.py` (+ schema는 `schemas/<domain>.py`) |
| 새 도메인 entity | `src/reaction_backend/domain/<name>.py` (프레임워크 의존성 없음) |
| 새 ORM 모델 | `src/reaction_backend/db/models/<name>.py` |
| 새 Repository | `src/reaction_backend/repositories/<name>_repo.py` |
| 새 Worker Agent | `src/reaction_backend/agents/<name>_agent.py` |
| 새 Orchestrator 상태 | `src/reaction_backend/orchestrator/<name>.py` |
| 새 LLM 호출 패턴 | `src/reaction_backend/llm/` + `prompts/<domain>/<name>.v1.md` |
| 새 외부 API | `src/reaction_backend/integrations/<provider>/` |
| 새 cron | `src/reaction_backend/scheduler/<name>.py` + 시간표 갱신 |
| 안전성 가드 | `src/reaction_backend/safety/` |
| 메트릭/로그 | `src/reaction_backend/observability/` |

## 5. 코드 컨벤션

ruff (`pyproject.toml`) + mypy strict가 단일 소스. 사람이 별도 스타일 가이드를 두지 않는다.

- Python 3.12 문법:
  - PEP 695 generic: `class Foo[T]: ...`
  - `list[int]`, `X | None`
- FastAPI 의존성 주입은 `Annotated[T, Depends(...)]`.
- 비동기: 라우터·repository는 async. CPU bound (rule scheduler)는 sync OK.
- 도메인 모듈 import 방향: routers → agents/orchestrator → repositories → db/integrations. 역방향 금지.

## 6. 테스트 규칙

- pytest + `fastapi.testclient.TestClient`.
- 각 router는 happy path 1 + 실패 케이스 1 최소.
- 외부 IO (LLM/Calendar/DB)는 fake/stub. agent 단위 테스트는 LLM 호출 stub.
- 통합 테스트는 `tests/integration/` 분리.
- prompt 변경은 `tests/prompts/` 회귀 테스트로 보호.

## 7. 커밋 / PR 규칙

- 커밋 메시지: `type: 짧은 설명` (`feat` / `fix` / `chore` / `docs` / `refactor` / `test` / `ci`).
- PR 한 개는 한 가지 일만.
- PR 본문:

  ```
  ## 이슈
  Closes #<n>  또는  partially addresses #<n>

  ## 변경
  - …

  ## 어떻게 테스트했는지
  - …

  ## 리뷰어 체크 포인트
  - DB 마이그레이션 필요?
  - 새 envelope/에러 코드?
  - LLM 비용 영향?
  - HITL 게이트 우회 없는지?
  ```

## 8. 사람에게 먼저 물어볼 것

다음은 임의로 결정 X — PR 코멘트 또는 이슈로 합의:

- 잠금 결정 (§1) 변경
- 응답 envelope / 에러 코드 체계 변경
- 새 외부 의존성 추가 (LLM provider, 결제, 대시보드)
- 데이터베이스 마이그레이션 (특히 컬럼 삭제/타입 변경)
- staging/production 배포 설정 변경
- 토큰/PII 보관 위치 변경

## 9. 빠른 참조

- API 계약: [`docs/api-contract.md`](docs/api-contract.md)
- 아키텍처 / 흐름: [`docs/architecture.md`](docs/architecture.md)
- 이슈 트래커: https://github.com/hanium-reaction/reaction-backend/issues
- 프론트 레포: https://github.com/hanium-reaction/reaction-frontend
