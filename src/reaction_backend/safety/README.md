# `safety/` — 안전성 필터

Issue #5 범위. LLM 출력과 사용자 입력에 대한 안전성 가드.

후속 모듈:
- `forbidden_terms.py` — 금지어 후처리 필터 (DevBaseline §4.2 잠금):
  - `실패`, `또 못`, `안 됐`, `못했`, `왜 안`, `다시 실수`, `실패율`
  - 발견 시 PRD §4.3 권장 표현 사전으로 치환 또는 재생성
- `pii_masker.py` — 로그/관측치에서 이메일/전화 마스킹
- `content_validator.py` — 입력 길이, 빈도, 어뷰즈 가드

규약:
- 모든 LLM 출력은 사용자에게 보여주기 전 `forbidden_terms` 통과 강제
- 통과 실패 시 1회 재생성, 그래도 실패 시 fallback 메시지로 대체
