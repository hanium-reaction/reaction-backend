# `memory/` — 4계층 메모리 추상화

re:action 의 핵심 차별점. 단순 CRUD가 아니라 **층위가 분리된 기억 시스템**.

| 레이어 | 책임 | 주요 테이블 |
| --- | --- | --- |
| **Planning** | 의도 — "무엇을 할 계획인가" | goals, goal_nodes, action_items, scheduled_blocks, habits, habit_instances, time_policies, fixed_schedules, dependency_links |
| **Raw Execution** | 사실 — "실제로 무엇이 일어났는가" | execution_events, interruption_events, context_snapshots (v0.6 14필드), execution_failure_tags |
| **Derived Stats** | 패턴 — "어떤 경향이 보이는가" | period_summaries (weekly/monthly), daily_briefs (캐시) |
| **Policy Snapshot** | 학습 — "다음에 어떻게 다르게 할까" | policy_snapshots (버전 이력), behavioral_profiles, interaction_styles |

추가 (P2): **Semantic Memory (Vector DB)** — insight embedding 으로 Recovery Coach 맥락 검색

규약:
- 라이터(write)는 자기 레이어만 쓴다. Recovery Coach가 PolicySnapshot 직접 변경 금지.
- 리더(read)는 자유. 하위 레이어로 내려갈 수록 raw, 상위 레이어로 올라갈 수록 derived.
