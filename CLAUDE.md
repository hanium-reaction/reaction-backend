# CLAUDE.md

이 레포의 모든 에이전트 작업 규칙은 [`AGENTS.md`](AGENTS.md) 에 모여 있습니다.

Claude Code 에이전트는 작업 시작 전 [`AGENTS.md`](AGENTS.md) 를 먼저 읽어 주세요.

특히 다음 항목은 우회 금지:

- §1 잠금된 제품 결정 (DevBaseline §1.4)
- §2 절대 하지 말 것 — main 직접 push / LLM SDK 직접 import / HITL 우회 / hard delete / 평문 토큰 저장
- §4 어디에 무엇을 넣는가 (폴더 가이드)
