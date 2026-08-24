# ADR-0008: 만다라트 실행 주기 — 64칸을 2주 계획과 주간 리포트로 잇는다

- 상태: 제안 (2026-08-24)
- 관련: ADR-0007 (마일스톤 층), #220 (만다라트 S29~S32), ADR-0005 §2.5.1 (First Plan 상태머신),
  ADR-0006 (알림 dispatch), `docs/ultimate-goal-mandalart-strategy.md` §11
- 구현(예정): `db/models/habit.py` · `orchestrator/mandala_adapter.py` ·
  `orchestrator/first_plan_adapter.py` · `orchestrator/weekly_review.py` ·
  `scheduler/runtime.py` · `api/routes/planning.py` · `api/routes/review.py`

## 배경

만다라트(#220)는 73행을 저장하고 축 승격(U10)까지 구현됐지만, **그린 다음에 아무 일도
일어나지 않는다.** 세 군데가 끊겨 있다.

**① 64칸이 실행과 연결된 적이 없다.** `action_items.goal_node_id` 에 값을 쓰는 코드는
`first_plan_adapter.py:1977` 한 곳뿐이고, 그 노드는 `tree_kind='plan'` 분해 트리다.
`persist_mandala`(`mandala_adapter.py:270-341`)는 `action_items` 를 만들지 않는다. 결과로
`_leaf_progress`(`mandala_adapter.py:366-379`)의 카드 롤업 분기는 **실사용에서 도달하지
않고**, 만다라 진척도는 사용자가 `PATCH /goals/mandala/nodes/{id}` 로 직접 찍는
`completed_at` 만으로 움직인다. 방어 코드(`_replaceable_action`, `_mandala_node_ids_among`)와
그 테스트는 존재하지만 테스트가 `goal_node_id` 를 수동 세팅한다.

**② 궁극목표의 시간 지평이 날짜가 되지 않는다.** `ultimate.horizon` 은 "3년/5년/7년/10년"
칩으로 필수 수집되고 `_horizon_years`(`ultimate_adapter.py:109`)가 int 로 파싱까지 하는데,
쓰이는 곳은 `mandala_adapter.py:63` 의 **프롬프트 문자열 한 줄**이 전부다. 승격된 Goal
(`goals.py:445-457`)에도 `deadline` 이 붙지 않는다. 계획 지평은 `deadline` 에서 파생되므로
(`_horizon_weeks`, `first_plan_adapter.py:394-419`), 만다라에서 내려온 목표는 **마감이 없는
목표**로 취급돼 항상 `_MAX_PLAN_WEEKS` 기본값을 받는다.

**③ 주간 리뷰가 만다라를 모른다.** `weekly_review` cron 은 일요일 03:00 KST 에 돌고
`period_summaries` 를 채우지만, `orchestrator/weekly_review.py` 에 만다라 관련 코드는 0건이다.
사용자는 "64칸 중 뭘 했는지" 를 어디서도 볼 수 없다.

ADR-0007 이 ①③의 뼈대(3층 구조·커서 모델·주기 전환)를 이미 승인했지만, 6단계 구현 순서
중 1단계(잘린 마일스톤 고지, `missing_milestones_notice`)만 완료됐다. ADR-0007 이 "이 설계의
관문이자 유일한 고위험 지점"이라 부른 2단계(마일스톤 영속)부터 미착수다.

이 ADR 은 ADR-0007 을 **만다라트에 결합하고 주기를 2주로 좁히는** 결정만 다룬다.

---

## 1. 64칸은 한 종류가 아니다 — 프로젝트형 / 반복형

가장 중요한 결정이다. 지금 코드는 모든 칸을 "언젠가 완료되는 것"으로 가정한다
(`completed_at` 단일 컬럼). 실제 만다라트는 그렇지 않다.

```
"사이드 프로젝트 배포"   → 끝이 있다.       계획으로 내려가야 한다
"코딩테스트 1일 1문제"   → 끝이 없다.       몇 번 했는지만 의미 있다
"1일 1커밋"             → 끝이 없다.       주 며칠 했는지가 지표다
"쓰레기 줍기"           → 끝이 없고 가볍다.  계획을 짜면 오히려 방해다
```

끝없는 칸에 `completed_at` 을 강요하면 사용자는 영원히 체크하지 않고, 리포트는 "64칸 중
3칸 완료"라는 **의미 없는 숫자**를 낸다. 반대로 이런 칸까지 `action_items` + 블록으로
배치하면 계획이 잡일로 가득 찬다 — 이 제품이 고치려는 문제 그 자체다.

**결정: 칸을 두 종류로 나누고, 종류는 `habits` 행의 존재로 판정한다.**

| 종류 | 판정 | 실행 경로 | 진척 표현 |
|---|---|---|---|
| **프로젝트형** (기본) | 연결된 `habits` 행 없음 | 마일스톤 → 2주 계획 → `action_items` + `scheduled_blocks` | 완료/미완료 (`completed_at` + 카드 롤업) |
| **반복형** | 연결된 `habits` 행 있음 | 계획을 만들지 않는다 | 주간 횟수 (`habit_instances.done_count`) |

**별도 `cell_kind` 컬럼을 두지 않는다.** 링크 유무가 곧 종류라 상태가 두 곳에 갈리지
않는다. 칸을 반복형으로 바꾸는 것 = `habits` 행을 만들어 붙이는 것, 되돌리는 것 = 그 습관을
soft delete 하는 것이다.

### 1.1 왜 새 테이블이 아니라 `habits` 인가

필요한 게 이미 전부 있다.

- `habits.frequency_per_week` (1~7 CHECK), `target_count`, `minutes_per_session`
- `habit_instances(habit_id, week_start)` UNIQUE + `done_count` — **주 단위 횟수 누적**
- `POST /habits/instances/{instanceId}/check` (`habits.py:218`) — 횟수 +1 API
- `habit_instances` cron 이 매일 00:05 get-or-create 로 주 인스턴스를 보장 (`runtime.py:218-224`)
- 3주 연속 미달 → 빈도 재설계 제안이 이미 구현돼 있다 (`orchestrator/habit_penalty.py`,
  `GET /reviews/habit-penalty`)

즉 "쓰레기 줍기를 몇 번 했나"는 **새 코드 없이** 이미 셀 수 있다. 빠진 것은 그 습관이 어느
만다라 칸에서 나왔는지를 아는 링크 하나뿐이다.

### 1.2 진척도 계산 변경

`compute_progress`(`mandala_adapter.py:382-428`)의 분모 8 고정 규약은 유지한다. 축의
`progress` 분자에서 **반복형 칸은 제외**하고, 대신 `coverage` 에는 "이번 주 1회 이상 수행"
으로 착수 판정한다. 반복형만 8칸인 축은 `progress = null`("판단 불가")이 되어야지 0% 로
보이면 안 된다 — `_leaf_progress` 가 terminal 카드 없을 때 `None` 을 내는 것과 같은 이유다.

---

## 2. 궁극목표 지평 → 실제 날짜 → 마일스톤

**결정:** `horizon_years` 를 궁극목표 Goal 의 `deadline` 으로 확정한다
(`now_kst().date() + horizon_years 년`, "기한 없음"이면 `None`). 축 승격 시
(`goals.py:413`) 승격된 Goal 은 궁극목표의 `deadline` 을 상속하지 않고 **사용자가 이번
학기 마감을 고른다** — 3년짜리 지평을 학기 목표에 그대로 물려주면 `_horizon_weeks` 가
항상 상한에 걸려 무의미해진다.

마일스톤 층은 ADR-0007 §1 을 그대로 따른다. 이 ADR 이 추가하는 것은 **입력이 인터뷰
`InterviewOutcome` 만이 아니라 만다라 축일 수 있다**는 것뿐이다. 축 하나(8칸)가
`plan_milestones` 프롬프트의 입력이 되고, 마일스톤 3~5개가 `node_type='milestone'` 로
영속된다 (ADR-0007 PR-2, 마이그레이션 0 — enum 슬롯이 이미 비어 있다).

---

## 3. 주기는 2주 — 단, 만다라 유래 목표에만

`_MAX_PLAN_WEEKS = 4`(`first_plan_adapter.py:364`)는 **바꾸지 않는다.** 기존 인터뷰 경로의
계획 생성·커버리지 고지 문구·관련 테스트가 전부 이 값에 묶여 있어서, 전역 변경은 이 ADR 의
범위 밖 회귀를 부른다.

**결정:** 만다라 트리를 소유한 goal(`_mandala_owned_goal_ids`, `first_plan_adapter.py:1730`)
에 한해 계획 창을 2주로 좁힌다. 창은 **rolling** 이다 — 매주 일요일 커서가 1주 전진하므로
사용자에게는 항상 앞으로 2주가 보인다. "2주마다 한 번"이 아니라 "매주 갱신되는 2주 창"이다.

```
주 1 일요일:  [ W2  W3 ]  ← 지금 보이는 창
주 2 일요일:  [ W3  W4 ]  ← 커서 1주 전진, W3 는 실적 반영해 다듬어짐
```

전환 판정은 ADR-0007 §5 의 가드 3개를 그대로 쓴다.

---

## 4. 일요일 밤 리포트 — 새 알림 클래스를 만들지 않는다

AGENTS §1 은 알림을 3 클래스(`morning_brief` / `pre_card` / `evening_reflection`)로 잠갔고,
`notification_sends` 에 DB CHECK 제약까지 걸려 있다. ADR-0006 §2 가 "완화가 필요하면 잠금
변경 절차로 — 코드로 우회하지 않는다"고 못 박았고, 잠정 목표 만료 알림이 같은 벽에서
포기된 선례가 `scheduler/README.md:38` 에 남아 있다.

**결정:** 4번째 클래스를 만들지 않는다. **일요일의 `evening_reflection` 알림에 주간
리포트를 얹는다.** 문구와 딥링크만 요일에 따라 갈라지고, 클래스·예산·중복 규칙은 그대로다.

- 주 ≤ 3건 예산(`push_gate.py:44-46`)을 잠식하지 않는다 — 일요일 저녁 알림은 어차피 나간다
- quiet hours `[23:00, 07:00)` 문제도 이미 풀려 있다 — evening 알림의
  `min(설정시각, 22:55)` 클램프(`notify_sweeps.py:67,120`)를 그대로 탄다

### 4.1 집계 시각을 옮긴다

현재 `weekly_review` cron 은 **일요일 03:00** 에 돌면서 `week_start_of(오늘)` 로 집계 창을
잡는다(`sweeps.py:82`). 그 창은 `[월 00:00, 다음 월 00:00)` 이라 **일요일 하루가 통째로 남은
상태**에서 집계된다. `force=False` idempotent skip(`weekly_review_precompute.py:62-65`)
때문에 그 주에 다시 돌아도 갱신되지 않는다.

**결정:** 주 1회 cron 을 쓰지 않는다. `habit_instances` 가 "매주 월요일"에서 "매일 00:05 +
get-or-create no-op"으로 바뀐 것과 **같은 이유**(MemoryJobStore 라 재기동 시 주 1회 job 이
통째로 유실 — `runtime.py:211-217`, `scheduler/README.md:52-57`)로, 잦은 폴 + idempotent
skip 패턴을 재사용한다. 일요일 저녁 시간대에만 실제 작업을 하고 나머지는 no-op 이다.

### 4.2 리포트가 보여주는 것

두 종류를 **섞지 않고 나눠서** 보여준다. 섞으면 "64칸 중 3칸"이라는 무의미한 숫자가 된다.

```
이번 주 만다라트

  끝낸 칸        2칸        (누적 7 / 64)
  굴린 칸        5칸        이번 주에 손댄 칸
  손 못 댄 축    2개        "오픈소스 분석", "기술 블로그"

  반복 중
    코딩테스트 1문제    5회      (목표 7회)
    1일 1커밋          6/7일
    쓰레기 줍기        2회      (목표 없음)
```

"손 못 댄 축"을 이름으로 말하는 게 이 리포트의 핵심이다 — 비율은 잊히지만 빈 축은 다음 주
행동을 바꾼다.

---

## 5. 다음 2주 계획은 Draft 로만 제안한다

AGENTS §1: "AI 출력 = Draft Layer + [수락/수정/거절] 3버튼. 자동 적용 금지."

**결정:** 리포트는 다음 2주 계획을 **자동으로 적용하지 않는다.** 리포트 하단에 Draft 제안
카드를 놓고, 사용자가 열어 승인해야 `action_items` + `scheduled_blocks` 가 된다. 승인 경로는
기존 `POST /plans/{planId}/approve` 의 supersede 규약(`first_plan_adapter.py:1583-1657`)을
그대로 탄다 — `source='user_edit'` 블록이 붙은 카드는 보존되고, hard delete 는 없다.

전환 제안 생성은 `replan` 이 아니다. 주간 `replan`(`orchestrator/replan.py`)은 룰 only 로
**기존 카드를 뒤로 미는 것만** 하고 새 내용을 만들지 못한다 — ADR-0007 §배경 ② 가 지적한
그 한계다.

---

## 6. 큰 목표 수정

리포트에서 3주 연속 손 못 댄 축이 나오면, 그 축을 **줄이거나 바꾸자고 제안**한다. 실행이
아니라 제안이다(§5 와 같은 이유). 수정 수단은 이미 있다:

- 칸/축 텍스트: `PATCH /goals/mandala/nodes/{id}` (U9)
- 축 8칸 재생성: `POST /plans/mandala/{planId}/regenerate-branch` (U5) — 승인 전 초안 한정
- 마일스톤 재조정: ADR-0007 PR-6 (미구현, `MilestoneDraft` 에 id 필드 신설 필요)

궁극목표 본문 자체를 바꾸는 것은 **재인터뷰**(`kind='ultimate'`)로만 한다. 몇 년에 한 번
바뀌는 값이라 인라인 편집 경로를 따로 두지 않는다.

---

## 7. 마이그레이션

**필수 1건 (additive).**

```python
op.add_column("habits", sa.Column("goal_node_id", sa.UUID(), nullable=True))
op.create_foreign_key("fk_habits_goal_node_id", "habits", "goal_nodes",
                      ["goal_node_id"], ["id"], ondelete="SET NULL")
op.create_index("ix_habits_goal_node_id", "habits", ["goal_node_id"])
```

nullable 이라 기존 행은 전부 NULL(= 만다라와 무관한 일반 습관)로 즉시 VALID 다.
`ON DELETE SET NULL` 은 칸이 사라져도 습관 기록이 남게 한다 — hard delete 금지(§2)와 같은
방향이다.

**마이그레이션 0 인 것:**

- `node_type='milestone'` — enum 에 이미 있고 아무도 안 쓴다 (ADR-0007 §9)
- `goals.status='completed'` — 동일
- 리포트 지표 — `GET /reviews/weekly` 가 이미 즉석 계산 폴백을 한다(`review.py:142-143`).
  스냅샷 보존이 필요해지면 그때 `period_summaries` 에 JSONB 1컬럼을 추가한다 (별도 합의)

---

## 8. 구현 순서

| PR | 내용 | 마이그레이션 | 선행 |
|---|---|---|---|
| **A** | `habits.goal_node_id` + 반복형 칸 ↔ 습관 링크 API. 만다라 칸 시트에서 "이건 반복" 선택 | 1건 | — |
| **B** | 계획 인터뷰 `goals.heaviest` 가 승격된 만다라 축 목표를 동적 보기로 포함(§11 항목 6 "핵심 접합점" 마지막 이음매). 실행 트리는 여전히 `/plans/generate` 가 새로 만든다 | 0 | — |
| **C** | `horizon_years` → `deadline` 확정 ✅ + 마일스톤 영속(ADR-0007 PR-2, `_archive_goal_nodes` 층 분리) ✅ | 0 | B |
| **D** | 만다라 유래 goal 2주 rolling 창 + 커서 전진 | 0 | C |
| **E** | 일요일 밤 리포트 — cron 시각 이동(잦은 폴 + idempotent), 만다라 지표 집계, `GET /reviews/weekly` 확장 | 0 | A·B |
| **F** | 저녁 회고 알림 일요일 분기(문구·딥링크). 새 클래스 없음 | 0 | E |
| **G** | 다음 2주 Draft 제안 + 승인 (ADR-0007 PR-5) | 0 | D·E |
| **H** | 손 못 댄 축 축소 제안 (ADR-0007 PR-6 마일스톤 재조정 포함) | 0 | G |

**A·B 가 관문이다.** 이 둘 없이는 만다라 칸과 실행이 여전히 남남이라 리포트가 셀 것이
`completed_at` 수동 체크밖에 없다.

> **정정(2026-08-24)**: 최초 초안의 "B"는 `action_items.goal_node_id` 에 mandala leaf id 를
> 직접 기록하는 안이었다. 이는 `docs/ultimate-goal-mandalart-strategy.md` §11 항목 6 —
> "셀 → ActionItem 직결 ❌ 하지 않음. `action_items.target_date` 가 NOT NULL 이고 배치
> 정책 가드가 전부 `/plans/generate` 안에 있다. 경로는 **셀 → 승격 Goal → 딥 인터뷰 →
> `/plans/generate` → approve**" — 와 정면으로 충돌한다(만다라 leaf 는 날짜 개념이 없고,
> 승격은 축 단위지 leaf 단위가 아니다). 실제로 비어 있던 건 그 문서가 이미 정한 경로의
> 마지막 이음매 하나뿐이었다 — 계획 인터뷰의 `goals.heaviest` 가 승격된 축을 몰라서,
> 사용자가 이미 승격해 둔 목표를 인터뷰에서 매번 다시 타이핑해야 했다
> (`docs/ultimate-goal-mandalart-strategy.md:71` "핵심 접합점"). B 를 그 이음매를 잇는
> 것으로 다시 정의한다 — 구현: `orchestrator/interview.py::_question_options`,
> `mandala_adapter.fetch_promoted_goal_titles_for_user`.

프론트는 별도 레포다 — 칸 종류 선택 UI, 반복 카운트 체크, 주간 리뷰 만다라 섹션, 2주 계획
화면이 대응 PR 로 필요하다.

---

## 9. 리스크 · 사람 합의가 필요한 항목

| # | 항목 | 상태 | 근거 |
|---|---|---|---|
| 1 | `habits.goal_node_id` 마이그레이션 | ⚠️ **합의 필요** (AGENTS §8 — DB 마이그레이션) | §7 |
| 2 | 일요일 저녁 알림 문구를 리포트로 바꾸는 것 | ⚠️ 합의 권장 — 클래스·예산은 불변이나 사용자가 받는 내용이 바뀐다 | §4 |
| 3 | `weekly_review` cron 시각 이동 (일 03:00 → 잦은 폴) | ✅ 선례 있음 (`habit_instances`, `scheduler/README.md:52-57`) | §4.1 |
| 4 | 4번째 알림 클래스 | ❌ **하지 않음** — AGENTS §1 잠금, ADR-0006 §2 | §4 |
| 5 | `_MAX_PLAN_WEEKS` 전역 변경 | ❌ **하지 않음** — 기존 경로 회귀 | §3 |
| 6 | 반복형 칸을 자동으로 완료 처리 | ❌ **하지 않음** — 반복형엔 완료 개념이 없다. 횟수만 센다 | §1 |
| 7 | 계획 승인 없이 2주 계획 자동 적용 | ❌ **하지 않음** — AGENTS §1 자동 적용 금지 | §5 |
| 8 | 축 승격 시 궁극목표 `deadline` 상속 | ❌ **하지 않음** — 사용자가 학기 마감을 고른다 | §2 |

---

## 10. 되돌리기

PR A 의 컬럼은 nullable 이라 값을 비우면 전 시스템이 이전 동작으로 돌아간다(모든 칸이
프로젝트형). PR D 의 2주 창은 만다라 goal 한정 분기라 분기 하나를 제거하면 기존 4주로
복귀한다. PR F 는 문구 분기라 조건 한 줄이다. 되돌릴 수 없는 결정은 없다.
