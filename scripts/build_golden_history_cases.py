"""이력 골든셋 42건 생성기 (L1-8).

계획 분해가 받는 **실행 이력** 세 신호가 계획을 의도한 방향으로 바꾸는지 재는 오프라인
입력이다. 세 신호는 각각 다른 PR 로 들어왔고, 셋 다 **효과가 측정된 적이 없다**:

| 신호 | 어디서 | 프롬프트 변수 / 경로 |
|---|---|---|
| 목표 단위 실패 사유 | #466 | `{{failure_summary}}` (`이 목표:` / `전체 목표:` 접두어) |
| 전략별 회복 결과 | #466 | `{{recovery_summary}}` (통한 조정 / 안 통한 조정) |
| 연속 실패 → 분량 감쇠 | #467 | `dampened_density` — 프롬프트가 아니라 룰 |

## 설계의 핵심 — 짝(pair)이 전부다

이력의 효과는 **절대값으로 읽을 수 없다.** "이력을 준 계획의 평균 세션이 45분" 은 그
자체로 아무 뜻이 없다. 그래서 6개 목표를 **모든 블록이 똑같이** 쓰고, 블록 간 차이가
오직 `history` 뿐이도록 만든다(`pair_id` 로 묶인다). 지표는 전부 **대조군 대비 차이**다.
자료 골든셋이 `no_material` 을 M14 의 기준선으로 둔 것과 같은 구조다.

## 왜 실패 이력 블록의 `consecutive_goal_failures` 가 0 인가

**교란을 끊으려고 일부러 0 이다.** 연속 실패가 2 이상이면 `dampened_density`(#467)가
동시에 걸려 분량이 프리셋 단위로 내려간다. 그러면 세션이 짧아진 게 프롬프트가 실패
사유를 읽어서인지 룰이 예산을 깎아서인지 **구별할 수 없다.** 감쇠는 `damped_density`
블록에서만 켠다.

실패 4회인데 연속 0 은 모순이 아니다 — `compute_consecutive_failure_count` 는 `done` 을
만나면 리셋하므로, 28일 창에 실패 4건이 흩어져 있고 그 사이에 완주가 있으면 정확히 이 상태다.

## 왜 목표가 **빈도를 말하지 않는가** — 안 그러면 감쇠 블록이 아무것도 안 잰다

각 목표는 `weekly_hours`(주당 가용 시간)만 말하고 `frequency_per_week`(주 N회)는 말하지
않는다. 슬롯을 덜 채운 게 아니라 **측정이 성립하려면 그래야 한다.**

`target_sessions_per_week` 는 우선순위 1 로 **사용자가 명시한 빈도를 그대로 쓴다** —
"사용자가 케이던스를 명시한 것이므로 density 로 가감하지 않고 존중한다"(그 함수 docstring).
그래서 빈도를 말한 목표에서는 density 를 낮춰도 주당 세션 수·총 분량·총 세션 수가 **한
숫자도 안 움직인다**(실측: 주3회·50분 목표에서 standard/light 둘 다 spw 3 · 600분 · 12세션).
그 상태로 `damped_density` 블록을 두면 대조군 6건이 하나 더 있는 것과 같고, 하네스는
조용히 돌면서 보고서는 "감쇠를 쟀다" 고 말하게 된다.

⚠️ **이건 골든셋의 제약이 아니라 #467 의 실제 도달 범위다.** 분량 감쇠는
(a) 사용자가 빈도를 말했으면 분량에 **무효**하고, (b) 빈도를 안 말했어도 표준 밀도의 목표
분량이 한 호출 세션 상한(`_MAX_LLM_SESSIONS`)에 걸리면 총 분량·총 세션 수가 클램프돼
`sessions_per_week` 와 하루 집중 상한만 움직인다(실측: 주6시간·50분 목표에서 standard/light
둘 다 1000분·20세션). 이 골든셋의 여섯 목표는 **감쇠가 실제로 무는 구간**(빈도 미언급 +
상한 아래)에 있도록 `weekly_hours` 를 골랐고, `tests/test_golden_history_cases.py` 가
프로덕션 함수로 그 사실을 케이스마다 확인한다. 나머지 두 구간을 재려면 별도 블록이 필요하고,
그건 "감쇠를 어디까지 물게 할 것인가" 라는 제품 결정이 먼저다.

## 회복 블록은 **같은 전략**으로 방향만 뒤집는다

`recovery_worked` 와 `recovery_rejected` 는 둘 다 `DOWNSCOPE_DEFAULT`(범위 줄여서 진행)를
쓴다. 다른 전략을 쓰면 "전략이 달라서" 와 "결과가 달라서" 가 섞인다. 같은 전략에 결과
낱말만 뒤집으면 기대 부호가 대칭이 된다:

    평균 세션 분:  worked  <  control  <=  rejected

`rejected` 에 "더 커져야 한다" 가 아니라 "**줄어들면 안 된다**" 를 기대하는 이유는, 사용자가
거절한 건 축소 제안이지 "더 크게 해달라" 가 아니기 때문이다.

## 무엇이 강한 지표이고 무엇이 약한 지표인가 (정직 표기)

- **강함 — 누출(M37)·비난(M38).** 케이스 단위 이진 판정이라 표본이 작아도 위반 1건이 곧
  신호다. 프롬프트가 "인용하지 말고 조정에만 써라" 를 어겼는지는 문자열로 결정된다.
- **약함 — 분량 델타(M36).** LLM 분해는 확률적이라 목표 6개로는 작은 효과를 못 잡는다.
  **반복 실행 없이 단발로 읽지 말 것.** 반복은 같은 케이스 안에서 상관이 있으므로 분모는
  케이스 수(6쌍)이지 실행 수가 아니다 — 첫 계획 골든셋이 M29 에서 겪은 것과 같은 함정이다.
- **가장 약함 — `failure_hard_to_start`.** "first_step 이 더 쉬워졌는가" 를 결정적으로 셀
  방법이 없다. 사람 라벨이 붙기 전까지 **탐색용**이고 성공 기준에 넣지 않는다.

## 정직성

- 전 케이스 `synthetic: true`. 실사용 이력 분포를 대표하지 않는다. 특히 실제 도그푸딩
  데이터는 `skipped`("나중에")가 대부분인데(#258 종료 코멘트), 그 값은 계획 집계에서
  **일부러 제외**되므로(#469) 이 골든셋에는 등장하지 않는다.
- `expected.direction` 은 **설계자의 기대이지 정답이 아니다.** 자료 골든셋의
  `forbidden_items` 와 같은 성격이라, 방향이 안 나왔다고 곧바로 "모델이 틀렸다" 로 읽지
  말고 그 사실을 결과 문서에 그대로 적는다.
- 라벨 문자열은 **마스터 테이블 실값**이다(`failure_reason_tags` / `recovery_strategy_catalog`
  시드). 여기서 지어내면 누출 판정이 실제로 프롬프트에 들어가는 문자열과 어긋난다.
- 마감은 `deadline_offset_days`(상대값)뿐이다 — 절대 날짜는 하루만 지나도 판정이 뒤집힌다.

실행:
  uv run python -m scripts.build_golden_history_cases          # eval/ 에 씀
  uv run python -m scripts.build_golden_history_cases --stdout # 표준출력으로
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "eval" / "golden_history_cases.jsonl"

# 전역 금지어 필터(`safety/banned_words.BANNED_REPLACEMENTS`)가 **안 잡는** 비난 표현만
# 여기 둔다. "또 못"·"실패"·"못했" 는 이미 필터가 치환하므로 여기서 또 세면 필터 성능을
# 이 골든셋의 지표로 착각하게 된다. 회복 골든셋이 전역 사전을 저장하지 않는 것과 같은 이유.
BLAME_MARKERS: tuple[str, ...] = ("매번", "자꾸", "또다시")

# 실패 사유 라벨 — `failure_reason_tags` 시드(d09c105520b5) 실값.
_PLAN_TOO_BIG = ("PLAN_TOO_BIG", "계획이 너무 컸어요")
_HARD_TO_START = ("HARD_TO_START", "시작이 어려웠어요")
# 회복 전략 라벨 — `recovery_strategy_catalog` 시드 실값.
_DOWNSCOPE = ("DOWNSCOPE_DEFAULT", "범위 줄여서 진행")

BLOCKS: tuple[str, ...] = (
    "no_history",
    "failure_goal_scoped",
    "failure_user_scoped",
    "failure_hard_to_start",
    "recovery_worked",
    "recovery_rejected",
    "damped_density",
)


@dataclass(frozen=True, slots=True)
class Goal:
    """모든 블록이 **공유하는** 목표. 블록 간 차이는 오직 `history` 뿐이어야 한다."""

    key: str
    title: str
    category: str
    current_level: str
    success_image: str
    session_length_minutes: int
    weekly_hours: int
    deadline_offset_days: int


GOALS: tuple[Goal, ...] = (
    Goal(
        key="sqld",
        title="SQLD 자격증 취득하기",
        category="study",
        current_level="SELECT 문은 쓰는데 정규화는 처음이에요.",
        success_image="기출 3회분을 70점 넘기면 준비된 거예요.",
        session_length_minutes=50,
        weekly_hours=4,
        deadline_offset_days=42,
    ),
    Goal(
        key="thesis",
        title="졸업논문 초고 완성하기",
        category="study",
        current_level="주제는 정했고 선행연구를 절반쯤 읽었어요.",
        success_image="서론부터 결론까지 초고가 한 번 이어지면 끝이에요.",
        session_length_minutes=90,
        weekly_hours=5,
        deadline_offset_days=70,
    ),
    Goal(
        key="portfolio",
        title="포트폴리오 사이트 배포하기",
        category="project",
        current_level="화면 두 개를 만들었고 배포는 안 해봤어요.",
        success_image="도메인으로 접속되면 완성이에요.",
        session_length_minutes=60,
        weekly_hours=4,
        deadline_offset_days=35,
    ),
    Goal(
        key="run10k",
        title="10km 완주하기",
        category="health",
        current_level="3km 를 쉬지 않고 뛸 수 있어요.",
        success_image="한 번에 10km 를 걷지 않고 완주하면 돼요.",
        session_length_minutes=40,
        weekly_hours=3,
        deadline_offset_days=56,
    ),
    Goal(
        key="interview",
        title="백엔드 이직 면접 준비하기",
        category="career",
        current_level="이력서는 있고 기술 면접은 2년 만이에요.",
        success_image="모의 면접에서 막힘 없이 답하면 준비된 거예요.",
        session_length_minutes=70,
        weekly_hours=4,
        deadline_offset_days=49,
    ),
    Goal(
        key="toeic",
        title="토익 800점 넘기기",
        category="study",
        current_level="지난 시험이 655점이었어요.",
        success_image="모의고사에서 800이 두 번 나오면 돼요.",
        session_length_minutes=45,
        weekly_hours=3,
        deadline_offset_days=28,
    ),
)


def _failure(tag: tuple[str, str], count: int) -> dict[str, Any]:
    """`ReviewRepo` 가 돌려주는 `TopFailureContext` 와 같은 모양 — 하네스가 그대로 넣는다."""
    return {"tag_code": tag[0], "label_ko": tag[1], "count": count, "share": 1.0}


def _recovery(strategy: tuple[str, str], outcome: str, count: int) -> dict[str, Any]:
    """`RecoveryRepo` 의 `RecoveryOutcomeContext` 와 같은 모양."""
    return {
        "strategy_type": strategy[0],
        "label_ko": strategy[1],
        "outcome": outcome,
        "count": count,
    }


def _case(
    *,
    block: str,
    goal: Goal,
    failure_contexts: list[dict[str, Any]],
    failure_scope: str,
    recovery_contexts: list[dict[str, Any]],
    consecutive_goal_failures: int,
    direction: str,
    notes: str,
) -> dict[str, Any]:
    # 누출 판정의 대상 — 이 케이스의 이력에 **실제로 들어간** 라벨만 싣는다. 안 그러면
    # 단언이 통과해도 아무것도 검증하지 않는다(자료 골든셋의 적대 케이스와 같은 불변식).
    labels = [c["label_ko"] for c in failure_contexts] + [c["label_ko"] for c in recovery_contexts]
    return {
        "assertions": {"must_not_contain": labels},
        "block": block,
        "case_id": f"{block}-{goal.key}",
        "expected": {
            "direction": direction,
            # 모든 비교는 같은 목표의 대조군과 한다 — 블록 평균끼리 비교하면 목표 구성이
            # 다를 때(여기선 같지만) 조용히 어긋난다.
            "compare_to": f"no_history-{goal.key}",
        },
        "goal": {
            "category": goal.category,
            "current_level": goal.current_level,
            "deadline_offset_days": goal.deadline_offset_days,
            "session_length_minutes": goal.session_length_minutes,
            "weekly_hours": goal.weekly_hours,
            "success_image": goal.success_image,
            "title": goal.title,
        },
        "history": {
            "consecutive_goal_failures": consecutive_goal_failures,
            "failure_contexts": failure_contexts,
            "failure_scope": failure_scope,
            "recovery_contexts": recovery_contexts,
        },
        "notes": notes,
        "pair_id": goal.key,
        "synthetic": True,
    }


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for goal in GOALS:
        cases.append(
            _case(
                block="no_history",
                goal=goal,
                failure_contexts=[],
                failure_scope="user",
                recovery_contexts=[],
                consecutive_goal_failures=0,
                direction="baseline",
                notes="이력 없음 — 모든 대비의 기준선. 프롬프트는 '(없음)' 을 받는다.",
            )
        )
        cases.append(
            _case(
                block="failure_goal_scoped",
                goal=goal,
                failure_contexts=[_failure(_PLAN_TOO_BIG, 4)],
                failure_scope="goal",
                recovery_contexts=[],
                consecutive_goal_failures=0,
                direction="smaller_sessions",
                notes="이 목표에서 '계획이 너무 컸어요' 4회 — 세션이 잘아져야 한다.",
            )
        )
        cases.append(
            _case(
                block="failure_user_scoped",
                goal=goal,
                failure_contexts=[_failure(_PLAN_TOO_BIG, 4)],
                failure_scope="user",
                recovery_contexts=[],
                consecutive_goal_failures=0,
                direction="smaller_sessions_weaker",
                notes="같은 사유·같은 횟수인데 범위만 '전체 목표' — 더 약하게 반영돼야 한다.",
            )
        )
        cases.append(
            _case(
                block="failure_hard_to_start",
                goal=goal,
                failure_contexts=[_failure(_HARD_TO_START, 3)],
                failure_scope="goal",
                recovery_contexts=[],
                consecutive_goal_failures=0,
                direction="easier_first_step",
                notes="착수 실패 — first_step 이 쉬워져야 한다. ⚠️ 사람 라벨 필요(탐색용).",
            )
        )
        cases.append(
            _case(
                block="recovery_worked",
                goal=goal,
                failure_contexts=[],
                failure_scope="user",
                recovery_contexts=[_recovery(_DOWNSCOPE, "worked", 3)],
                consecutive_goal_failures=0,
                direction="smaller_sessions",
                notes="범위 축소가 3회 통했다 — 처음부터 그 방향으로 잡아야 한다.",
            )
        )
        cases.append(
            _case(
                block="recovery_rejected",
                goal=goal,
                failure_contexts=[],
                failure_scope="user",
                recovery_contexts=[_recovery(_DOWNSCOPE, "rejected", 3)],
                consecutive_goal_failures=0,
                direction="not_smaller_sessions",
                notes="같은 전략을 3회 거절 — 그 방향으로 더 밀면 안 된다(줄어들면 결함).",
            )
        )
        cases.append(
            _case(
                block="damped_density",
                goal=goal,
                failure_contexts=[],
                failure_scope="user",
                recovery_contexts=[],
                consecutive_goal_failures=4,
                direction="lower_volume_budget",
                notes="연속 4회 실패 — 룰이 density 를 두 단계 낮춘다(#467). 프롬프트 문구는 그대로.",
            )
        )
    return cases


EXPECTED_COUNTS: dict[str, int] = dict.fromkeys(BLOCKS, len(GOALS))
EXPECTED_TOTAL = len(BLOCKS) * len(GOALS)


def to_jsonl(cases: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in cases)


def main() -> None:
    parser = argparse.ArgumentParser(description="이력 골든셋 42건 생성 (쓰기 전용, DB 무관)")
    parser.add_argument("--stdout", action="store_true", help="파일 대신 표준출력으로")
    args = parser.parse_args()

    cases = build_cases()
    payload = to_jsonl(cases)

    if args.stdout:
        print(payload, end="")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 고정 — Windows 기본(CRLF)으로 쓰면 재현성 테스트가 OS 마다 갈라진다.
    OUTPUT_PATH.write_text(payload, encoding="utf-8", newline="\n")

    blocks = Counter(c["block"] for c in cases)
    print(f"[build-golden-history-cases] {OUTPUT_PATH.relative_to(OUTPUT_PATH.parent.parent)}")
    print(f"  총 {len(cases)}건 (기대 {EXPECTED_TOTAL})")
    for block, expected in EXPECTED_COUNTS.items():
        mark = "OK" if blocks[block] == expected else "MISMATCH"
        print(f"  {block:22s} {blocks[block]:3d} / {expected:3d}  {mark}")
    print("  [!] all synthetic=true - report the synthesis ratio explicitly")


if __name__ == "__main__":
    main()
