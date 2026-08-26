"""연속 실패 에스컬레이션 — L0~L2 레벨 판정 (근거 대장 §5).

세션 없는 순수 함수 모음 — `orchestrator/recovery.py::select_strategies` 와 같은 설계
원칙(결정적, DB/프레임워크 의존 없음, 테스트가 히스토리를 직접 구성해 검증 가능)을
따른다.

**카운터를 DB 컬럼/테이블로 안 두는 이유**: 근거 대장 §5.1 은 "별도 집계 테이블 또는
파생 뷰"를 요구했다. 여기서는 그중 어느 쪽도 아니라 **매번 이력에서 계산하는 순수
함수**를 택했다 — 별도 테이블은 마이그레이션·백필·드리프트(갱신 누락) 위험이 생기고,
SQL 뷰로 "연속" + "partial_done 동결"을 표현하면 윈도우 함수가 이 함수의 5줄짜리 루프
보다 훨씬 읽기 어렵다. 이력 자체(`execution_events`/`recovery_attempts`)는 이미
불변으로 쌓이고 있으므로, 매번 다시 계산해도 항상 최신이라는 이점도 있다.

⚠️ **스코프 경계 (정직 표기)**:
- **L3(재협상 3장)은 "상태 판정"만 있다 — 재협상 3장 UX 자체는 아직 없다.** `EscalationLevel`
  에 `"L3"` 은 있고 `determine_escalation_level` 도 L3 를 정확히 판정하지만,
  `orchestrator/recovery.py::select_strategies` 는 L3 를 L1/L2 가 이미 가진 보호
  장치(DOWNSCOPE_DEFAULT 배제)만 이어받을 뿐 §5.2 가 요구한 "4그룹 통상 카드 대신
  재협상 3장([목표 축소]/[기한 재설정]/[일시 중단])"은 아직 안 만든다 — 그 UX 는
  FE 쪽 PARK 수락 플로우(`reaction-frontend#223`)가 아직 수락 안 돼 화면이 없다.
- **L4(stand-down)는 여전히 뺐다** — 진입 조건(`overwhelm≥4`)의 신호 자체가
  프로덕션에 없다(`context_snapshots` 실제 캡처 미완, #19-B-2 유예).
- **이 모듈은 레벨을 계산하는 로직만 제공한다.** `routes/recovery.py`나
  `orchestrator/recovery.py::select_strategies` 에 실제로 배선(L2 의 ENVIRONMENT_SHIFT
  선두 강제, L1 의 acknowledgment 활성화 등)하는 건 별도 작업이다. 특히 L1 의
  "acknowledgment 활성화"는 v3 프롬프트가 이미 "AVOIDANCE 태그일 때만"이라는 고정
  조건을 갖고 있어서(#272/#275), 거기에 "에스컬레이션 레벨"이라는 새 조건을 얹으려면
  프롬프트에 새 template 변수를 추가해야 한다 — 그러면 모든 버전이 같은 placeholder
  계약을 지켜야 하는 기존 테스트(`tests/prompts/test_recovery_prompts.py::test_every_version_matches_code_variables`)
  때문에 v1/v2 도 같이 손대야 한다. 그건 이번 작업 범위 밖이라고 판단했다.
- **"동일 카드"/"동일 (계보, tag_code)"/"동일 goal" 이력을 정확히 무엇으로 조회할지**
  (특히 회복으로 생성된 파생 카드까지 잇는 계보 그래프)는 이 모듈이 책임지지 않는다 —
  호출부가 이미 올바르게 필터링·시간 역순 정렬한 이력 리스트를 넘긴다고 가정한다.

모든 함수는 **최신이 먼저**(index 0 = 가장 최근) 오는 리스트를 받는다.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

ExecutionOutcome = Literal["done", "over_done", "partial_done", "failed"]
"""`execution_events.completion_status` 중 종결된 값만 — `in_progress` 는 아직 안
끝난 실행이라 이력에 안 들어간다(호출부가 쿼리 단계에서 제외하고 넘긴다)."""

RecoveryDecisionOutcome = Literal["accepted", "edited", "rejected", "skipped"]
"""`recovery_attempts.user_decision` 중 결정된 값만 — `pending` 은 아직 결정 전이라
이력에서 제외한다."""

RecoveryResultOutcome = Literal["completed", "abandoned"]
"""`recovery_attempts.recovery_result` 중 종결된 값만 — `pending` 은 제외한다."""

EscalationLevel = Literal["L0", "L1", "L2", "L3"]

# 근거 대장 §5.4 — "설계자 판단"으로 이미 고정된 값. acknowledgment 조건(overwhelm 등)과
# 달리 로그 재추정이 필요한 값이 아니다.
L1_CONSECUTIVE_FAILURE_THRESHOLD = 2
L1_RECOVERY_ABANDONED_THRESHOLD = 1
L2_SAME_TAG_FAILURE_THRESHOLD = 3
L3_GOAL_FAILURE_THRESHOLD = 4
L3_REJECTED_STREAK_THRESHOLD = 2


class EscalationCounters(NamedTuple):
    """근거 대장 §5.1 의 카운터 — L3(§5.2) 판정에 쓰는 `same_goal_failure_count` 포함."""

    consecutive_failure_count: int
    same_tag_failure_count: int
    same_goal_failure_count: int
    recovery_rejected_streak: int
    recovery_abandoned_streak: int


class EscalationState(NamedTuple):
    counters: EscalationCounters
    level: EscalationLevel


def compute_consecutive_failure_count(outcomes_most_recent_first: list[ExecutionOutcome]) -> int:
    """연속 실패 수를 센다 — `consecutive_failure_count` 와 `same_tag_failure_count`
    둘 다 이 함수 하나로 계산한다. 카운팅 규칙은 완전히 같고(§5.1 표에서 두 행이
    "동일"이라 명시), 다른 건 호출부가 어떤 기준(같은 카드 vs 같은 (계보,tag_code))
    으로 이력을 필터링해서 넘기느냐뿐이다.

    §5.1 규칙:
    - `done`/`over_done` 을 만나면 그 즉시 리셋(더 안 본다) — 그 이전 실패는 안 센다.
    - `partial_done` 은 **동결** — 증가도 리셋도 하지 않고 그냥 건너뛴다. "매일 조금씩만
      하고 마는 사용자"가 매번 partial_done 으로 카운터를 리셋해버리면 정확히 그 사용자가
      에스컬레이션 사각지대에 빠진다(원문 근거).
    - `failed` 만 실제로 센다.
    """
    count = 0
    for outcome in outcomes_most_recent_first:
        if outcome in ("done", "over_done"):
            break
        if outcome == "partial_done":
            continue
        count += 1  # "failed"
    return count


def compute_recovery_rejected_streak(
    decisions_most_recent_first: list[RecoveryDecisionOutcome],
) -> int:
    """§5.1: `rejected`/`skipped` 증가, `accepted`/`edited` 만나면 리셋(더 안 본다)."""
    count = 0
    for decision in decisions_most_recent_first:
        if decision in ("accepted", "edited"):
            break
        count += 1  # "rejected" | "skipped"
    return count


def compute_recovery_abandoned_streak(
    results_most_recent_first: list[RecoveryResultOutcome],
) -> int:
    """§5.1: `abandoned` 증가, `completed` 만나면 리셋(더 안 본다)."""
    count = 0
    for result in results_most_recent_first:
        if result == "completed":
            break
        count += 1  # "abandoned"
    return count


def determine_escalation_level(counters: EscalationCounters) -> EscalationLevel:
    """§5.2 레벨 정책(L0~L3) — 더 강한 조건부터 검사한다("순서의 근거": 재협상(L3)이
    단서 전환(L2)보다 강한 개입).

    L3 는 "동일 goal 4회 연속 실패" **또는** "회복 2회 연속 rejected" 중 하나만
    충족해도 된다(OR, L1 과 같은 형태). L2 는 "동일 태그 3회 연속 실패" 하나뿐이라
    단순 비교. L1 은 "동일 카드 2회 연속 실패 **또는** 회복 1회 abandoned" 중 하나만
    충족해도 된다(OR).
    """
    if (
        counters.same_goal_failure_count >= L3_GOAL_FAILURE_THRESHOLD
        or counters.recovery_rejected_streak >= L3_REJECTED_STREAK_THRESHOLD
    ):
        return "L3"
    if counters.same_tag_failure_count >= L2_SAME_TAG_FAILURE_THRESHOLD:
        return "L2"
    if (
        counters.consecutive_failure_count >= L1_CONSECUTIVE_FAILURE_THRESHOLD
        or counters.recovery_abandoned_streak >= L1_RECOVERY_ABANDONED_THRESHOLD
    ):
        return "L1"
    return "L0"


def compute_escalation_state(
    *,
    same_card_outcomes_most_recent_first: list[ExecutionOutcome],
    same_tag_outcomes_most_recent_first: list[ExecutionOutcome],
    same_goal_outcomes_most_recent_first: list[ExecutionOutcome],
    recovery_decisions_most_recent_first: list[RecoveryDecisionOutcome],
    recovery_results_most_recent_first: list[RecoveryResultOutcome],
) -> EscalationState:
    """다섯 이력 리스트에서 카운터 + 레벨을 한 번에 계산 — 호출부의 실제 진입점."""
    counters = EscalationCounters(
        consecutive_failure_count=compute_consecutive_failure_count(
            same_card_outcomes_most_recent_first
        ),
        same_tag_failure_count=compute_consecutive_failure_count(
            same_tag_outcomes_most_recent_first
        ),
        same_goal_failure_count=compute_consecutive_failure_count(
            same_goal_outcomes_most_recent_first
        ),
        recovery_rejected_streak=compute_recovery_rejected_streak(
            recovery_decisions_most_recent_first
        ),
        recovery_abandoned_streak=compute_recovery_abandoned_streak(
            recovery_results_most_recent_first
        ),
    )
    return EscalationState(counters=counters, level=determine_escalation_level(counters))
