# `prompts/` — Prompt Registry

Issue #5 (Prompt Registry / Safety Filter) 범위. 프롬프트를 **버전 관리**해 A/B 테스트와 회귀 추적이 가능하게 한다.

구조 예시:
```
prompts/
├── registry.py                 # PromptId → 최신 활성 버전 매핑
├── interview/
│   ├── next_question.v1.md
│   └── clarity_score.v1.md
├── planning/
│   ├── goal_decompose.v1.md
│   └── action_item_generate.v1.md
├── review/
│   └── plan_quality.v1.md
├── diagnosis/
│   └── failure_type.v1.md
└── recovery/
    └── if_then_proposal.v1.md
```

규약:
- 모든 프롬프트에 **버전 명시** (`v1.md`, `v2.md`)
- 사용 시 `llm_runs.prompt_id + prompt_version` 으로 추적
- 새 버전 도입은 shadow A/B (10% rollout) 권장
