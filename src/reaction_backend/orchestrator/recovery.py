"""Recovery 후보 선택 룰 엔진 — Orchestrator 2 의 DETECTING→DIAGNOSING rule 경로 (S19).

LLM 0회: `recovery_strategy_catalog.primary_trigger_tags` ↔ 실패 태그 매칭과
`display_priority` 만으로 UX 4 그룹 × 최대 1카드, 총 2~4장을 결정한다 (api-contract §12).
LLM(Recovery Coach)은 선두 카드의 if-then 문구 personalize 에만 쓰이고,
실패 시 본 룰 결과(카탈로그 템플릿)가 그대로 노출된다 (PRD §9 — 8초 fallback).

**예외 — L3(재협상, #328)**: 태그 매칭을 완전히 건너뛰고 DOWNSCOPE/RESCHEDULE/PARK
3방향 고정 카드를 낸다(`select_renegotiation_strategies`). 근거 대장 §5.2가 요구한
"동일 goal 반복 실패는 카탈로그가 아니라 목표·기한 자체를 다시 본다"는 다른 개입
의도라, 위 태그 매칭 규칙과 별도 경로로 분리했다.

순수 함수로 유지 — DB/프레임워크 의존 없음 (단위 테스트 대상).
"""

from __future__ import annotations

import re
from datetime import time, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime

    from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
    from reaction_backend.orchestrator.escalation import EscalationLevel

MIN_CARDS = 2
MAX_CARDS = 4

# 회복 카드가 '내일'로 넘어가는 그룹 — CARRY_OVER 만 하루 뒤다 (UX 4 그룹 중 1).
_CARRY_OVER_GROUP = "CARRY_OVER"
_PARK_GROUP = "PARK"

# 재관여 앵커의 기본 시각 — morning_brief 기본 발송 시각(`NotificationSetting`
# 08:00)과 가까운 "아침" 시간대. 사용자별 실제 설정은 이 순수 함수가 알 수 없다
# (repo 접근이 없다) — 실제 발송 시각 정밀화는 T2 배선(§6.2)의 몫으로 미룬다.
RE_ENGAGEMENT_ANCHOR_HOUR = 9

# 카탈로그에 전략이 없을 때(비활성 등) 회복 카드의 기본 소요 시간 — 최소 회복 단위.
DEFAULT_RECOVERY_MINUTES = 5

# DOWNSCOPE 가 원본에서 **남기는** 비율 — "범위를 좁혀 다시 한 번".
#
# 0.4 인 이유: 절반 이상 남기면 사용자가 방금 못 해낸 분량과 크게 다르지 않아 "좁혔다"는
# 신호가 안 서고, 1/4 이하로 줄이면 원본과의 연결이 끊겨 회복이 아니라 다른 일이 된다.
# 하한(전략의 최소 회복 단위)과 상한(원본)이 양끝을 잡으므로 이 값은 그 사이의 기울기다.
DOWNSCOPE_RETAIN_RATIO = 0.4

# 회복 카드 소요 시간의 눈금(분). 카탈로그의 `min_recovery_unit_minutes` 가 전부 5분 배수라
# 원본에서 파생한 값도 같은 눈금에 둔다 — "17분" 같은 카드는 사용자에게 근거 없어 보인다.
RECOVERY_MINUTE_STEP = 5

# 보정된 회복 블록은 '지금'보다 최소 이만큼 뒤에 둔다 (#174).
# 승인 직후 다시 과거가 되지 않게 하는 하한이자, pre_card 스윕이 이 블록을 **최소 1회는 보게**
# 하는 값이다 — 스윕은 5분 폴로 `[now+2m, now+7m)` 만 보므로, 블록이 `now+7분` 이후여야
# INSERT 다음 폴이 그 창에 걸린다. 15분 격자 올림과 합쳐 실효 리드는 10~25분.
RECOVERY_MIN_LEAD_MINUTES = 10

# 밤에는 블록을 새로 만들지 않는다 — `safety/push_gate` 의 quiet hours 시작(23시)과 같은 경계.
# orchestrator 는 safety 를 import 하지 않으므로(순수 유지) 값이 갈라지지 않게 테스트로 고정한다.
RECOVERY_NIGHT_CUTOFF_HOUR = 23

# 오늘 안에 자리가 없어 다음날로 넘길 때의 배치 시각 — `safety/push_gate` 의 quiet hours
# 끝(07시)과 같은 경계, 같은 이유로 import 대신 테스트로 고정한다(#258 도그푸딩 — 완주 0건의
# 원인 중 하나로 발견된 과거 배치 결함의 수정).
RECOVERY_MORNING_START_HOUR = 7

# 활동 시간대를 **모를 때만** 쓰는 기본 창 (07:00~23:00). 위 두 상수의 다른 이름이 아니라
# "사용자가 말해 준 게 없을 때의 추정치"라는 뜻이다 — 아는 사용자에게는 본인이 말한 창을 쓴다.
RECOVERY_DEFAULT_WINDOW_START = time(RECOVERY_MORNING_START_HOUR, 0)
RECOVERY_DEFAULT_WINDOW_END = time(RECOVERY_NIGHT_CUTOFF_HOUR, 0)

# PARK_DEFAULT 의 동적 트리거 임계값 — `recovery_strategy_catalog.py` 설계 주석
# "PARK_DEFAULT ← overwhelm_level >= 4" 를 그대로 상수화.
PARK_DEFAULT_STRATEGY_TYPE = "PARK_DEFAULT"
OVERWHELM_PARK_THRESHOLD = 4

# L2 단서 전환(근거 대장 §5.2)의 강제 대상 — "ENVIRONMENT_SHIFT 선두 강제".
ENVIRONMENT_SHIFT_STRATEGY_TYPE = "ENVIRONMENT_SHIFT"

# L1 축소→분해(근거 대장 §5.2)의 금지 대상 — "오늘은 절반만, 가능한 만큼만"이 원문이 금지한
# "전체를 15분만"(축소) 패턴과 같은 스타일(모호한 비율)인 유일한 DOWNSCOPE 전략.
DOWNSCOPE_DEFAULT_STRATEGY_TYPE = "DOWNSCOPE_DEFAULT"

# L3 재협상(#328, 근거 대장 §5.2)의 3방향 — 순서가 곧 카드 노출 순서(목표 축소 → 기한
# 재설정 → 일시 중단). CARRY_OVER 는 뺀다 — "내일로 그냥 미루기"는 계획 자체를 조정하는
# 게 아니라 재협상의 취지(카탈로그가 아니라 목표·기한을 다시 본다)와 안 맞는다.
_RENEGOTIATION_GROUPS = ("DOWNSCOPE", "RESCHEDULE", "PARK")

# COMEBACK 프리픽스(근거 대장 §4.1) — D6(Milkman et al. 2021, '놓친 뒤 복귀' 개입)를
# 5번째 UX 그룹 없이(AGENTS.md §1 잠금) 문구 층에만 얹는다. "역시 잘하시네요" 류 자존감
# 부양이 아니라(A1 — 그 조건이 자기자비보다 약했다) **지금 이 순간**에 초점을 맞춘
# 상황적 문구 — tone 규칙(비난 없는 청유형, 금지어 없음)과 같은 원칙.
COMEBACK_ACK_PREFIX = "다시 돌아온 지금이 중요해요. "


class _SafeFormatDict(dict[str, str]):
    """템플릿 변수 누락 시 빈 문자열 치환 — `{first_step}` 등."""

    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""


def render_template(template: str, variables: dict[str, str] | None = None) -> str:
    """카탈로그 `if_then_template` 의 `{변수}` 를 치환. 누락 변수는 빈 문자열 + 공백 정리."""
    rendered = template.format_map(_SafeFormatDict(variables or {}))
    return " ".join(rendered.split())


def with_comeback_ack(text: str, *, escalation_level: EscalationLevel | None) -> str:
    """연속실패≥2(에스컬레이션 발생) 일 때만 선두 카드 문구에 컴백 프리픽스를 얹는다.

    근거 대장 §4.1 — L1/L2 에스컬레이션은 둘 다 "동일 카드/계보 반복 실패"(§5.2)가
    전제라 이미 계산된 `escalation_level` 을 그대로 재사용한다(새 카운터 불필요, 스키마
    변경 0). LLM personalize 가 성공했든 실패해 카탈로그 템플릿이 그대로 노출됐든
    똑같이 적용한다 — 이 프리픽스는 LLM 출력이 아니라 고정 문구라 personalize 성패와
    무관하다(L2 는 LLM 호출 자체를 건너뛰지만 그래도 컴백 신호는 필요하다).
    """
    if escalation_level is None:
        return text
    return f"{COMEBACK_ACK_PREFIX}{text}"


def without_comeback_ack(text: str) -> str:
    """`with_comeback_ack` 의 역 — 제안 문구를 카드 본문(첫 걸음)으로 옮길 때 프리픽스를 뗀다.

    프리픽스는 **제안을 보여주는 그 순간**의 말이다("다시 돌아온 지금이 중요해요"). 수락해서
    만들어진 카드에 남으면 다음 날 오늘 화면·알림에서도 계속 같은 말을 하게 된다.
    """
    return text.removeprefix(COMEBACK_ACK_PREFIX).strip()


# 회복 카드 제목 꼬리표 — 원본 제목 뒤에 붙여 "무슨 일의 어떤 회복인지"를 한눈에 보이게 한다.
# 분 단위나 날짜 말("5분", "내일")은 일부러 안 넣는다: 카드 길이는 원본에서 파생되고
# (`recovery_action_minutes`), 블록 날짜는 승인 시각에 따라 달라져 제목과 어긋날 수 있다.
RECOVERY_TITLE_SUFFIX: dict[str, str] = {
    "DOWNSCOPE": "가볍게 다시",
    _CARRY_OVER_GROUP: "이어서",
}
_RECOVERY_TITLE_SEPARATOR = " · "
# `action_items.title` 컬럼 길이(String(300)).
RECOVERY_TITLE_MAX_LENGTH = 300


def recovery_action_title(original_title: str | None, option_group: str) -> str:
    """수락한 회복으로 만드는 새 카드의 제목 — **원본 제목 + 그룹 꼬리표**.

    예전엔 제안 문구(`suggested_action_text`)를 그대로 제목으로 썼다. 선두 카드만 LLM 이
    다듬고 나머지는 카탈로그 템플릿이라, 형제 카드를 고르면 다음 날 오늘 화면·주간 그리드·
    아침 알림에 "내일 같은 슬롯으로 그대로 옮겨드릴까요?" 같은 **질문 문장**이 카드 이름으로
    떴다 — 무슨 일인지도 안 보이고, 당일인데 "내일"이라고 적혀 있었다.

    이미 회복 카드였던 것을 또 회복하면 꼬리표가 쌓이지 않게 기존 꼬리표를 먼저 뗀다
    ("과제 · 가볍게 다시 · 가볍게 다시" 방지). 원본을 못 읽으면(보관 등) 꼬리표 없이
    "다시 해보기" 로 둔다 — 지어낸 제목보다 짧고 정직한 편이 낫다.
    """
    suffix = RECOVERY_TITLE_SUFFIX.get(option_group)
    base = " ".join((original_title or "").split())
    known_tails = tuple(f"{_RECOVERY_TITLE_SEPARATOR}{s}" for s in RECOVERY_TITLE_SUFFIX.values())
    while base.endswith(known_tails):
        base = next(base.removesuffix(t) for t in known_tails if base.endswith(t))
    if not base:
        return "다시 해보기"
    if suffix is None:
        return base[:RECOVERY_TITLE_MAX_LENGTH]
    tail = f"{_RECOVERY_TITLE_SEPARATOR}{suffix}"
    return f"{base[: RECOVERY_TITLE_MAX_LENGTH - len(tail)]}{tail}"


def select_strategies(
    failure_tags: list[str],
    strategies: list[RecoveryStrategyCatalog],
    *,
    min_cards: int = MIN_CARDS,
    max_cards: int = MAX_CARDS,
    overwhelm_level: int | None = None,
    escalation_level: EscalationLevel | None = None,
) -> list[RecoveryStrategyCatalog]:
    """실패 태그 → 전략 카드 선택.

    규칙 (DB 설계서 §6.10 + api-contract §12):
    1. `primary_trigger_tags` 와 실패 태그의 교집합 크기로 점수화.
    2. 같은 option_group 은 최고 점수 1개만 (동점은 display_priority 낮은 쪽).
    3. 점수 내림차순 → display_priority 오름차순으로 최대 `max_cards`.
    4. 매칭이 `min_cards` 미만이면, 아직 없는 그룹에서 display_priority 순으로 패딩
       (태그가 없거나 모호해도 항상 선택지를 보여준다 — "Be on your side").
    5. **동적 트리거**: `overwhelm_level >= OVERWHELM_PARK_THRESHOLD` 면 `PARK_DEFAULT`
       를 "태그 1개 매칭"과 같은 점수(1)로 후보에 넣는다 — 카탈로그 설계
       (`recovery_strategy_catalog.py` "PARK_DEFAULT ← overwhelm_level >= 4")를 기존
       점수 체계 그대로 확장한 것뿐, 새 가중치 축을 도입하지 않는다(일반 정서축 가중치
       `w_affect` 등은 근거 대장 §5.4 가 "골든셋 민감도 분석 선행, 값 없이 배포 불가"로
       명시적으로 막아 둔 별개 사안이라 여기서 손대지 않는다). 실 태그 매칭이 이미 더 높은
       점수거나 동점에서 더 낮은 `display_priority` 를 가지면 그쪽이 그대로 이긴다 —
       `overwhelm_level` 은 PARK 자리를 강탈하지 않고, 아무 매칭도 없을 때만 채운다.
       `overwhelm_level=None`(기본값, 호출부가 값을 안 넘길 때)이면 이 규칙은 완전히
       비활성 — 기존 동작과 100% 동일하다.
    6. **L2 단서 전환 강제** (근거 대장 §5.2): `escalation_level == "L2"` 면 활성
       `ENVIRONMENT_SHIFT` 전략을 위 1~5 규칙의 결과와 **무관하게** 맨 앞으로 강제한다.
       "동적 트리거"(규칙 5, 매칭이 없을 때만 후보로 끼워 넣음)보다 강한 개입이다 —
       룰 매칭 점수가 더 높은 카드가 있어도 밀어낸다("선두 강제"). 같은 option_group
       (`ENVIRONMENT_SHIFT` 는 DOWNSCOPE)의 기존 카드는 "동시 노출 1카드" 규칙에 따라
       빠진다. 카탈로그에 `ENVIRONMENT_SHIFT` 가 없거나 비활성이면 이 규칙은 조용히
       no-op — 카드 개수가 깨지지 않는다. `escalation_level=None`(기본값)이면 완전히
       비활성 — 기존 동작과 100% 동일하다.
    7. **L1 축소→분해** (근거 대장 §5.2): `escalation_level` 이 `"L1"`/`"L2"`
       면 `DOWNSCOPE_DEFAULT` 를 후보 자체에서 뺀다. 카탈로그 5개 DOWNSCOPE 전략 중
       유일하게 "오늘은 절반만, 가능한 만큼만"처럼 모호한 비율(축소)로 쓰여 있고,
       나머지(`NANO_STEP`/`CONTEXT_REWARMING`/`SELF_FORGIVENESS_NANO`)는 이미
       "딱 한 걸음/5분만"처럼 구체적 하위 단계(분해) 스타일이라 이 규칙은 **빼기만**
       한다 — 나머지가 이기도록 두면 기존 점수·패딩 로직이 자연히 분해 스타일을
       선택한다(별도 강제 로직 불필요). `FATIGUE`/`PLAN_TOO_BIG` 실매칭이 있었다면
       그 슬롯은 매칭 0 으로 떨어질 수 있고, 그러면 규칙 4 패딩이 다음 우선순위
       DOWNSCOPE 전략(`NANO_STEP`)으로 채운다.

    **L3(재협상)는 이 규칙들을 안 탄다** — 함수 맨 앞에서 `select_renegotiation_strategies`
    로 즉시 위임한다(#328). 태그 매칭·점수·패딩 전부 무관하고 DOWNSCOPE/RESCHEDULE/PARK
    3방향 각 1장을 고정으로 낸다 — 그 함수의 docstring 참고. 규칙 6(L2 단서 전환 강제)은
    L3 에 적용된 적이 없다("단서 전환"은 L2 전용 전술이라 재협상이라는 다른 개입 의도의
    L3 에 재사용할 근거가 없다 — `escalation.py` 모듈 docstring의 스코프 경계 참고).
    """
    if escalation_level == "L3":
        return select_renegotiation_strategies(strategies)
    active = [s for s in strategies if s.is_active]
    if escalation_level in ("L1", "L2"):
        active = [s for s in active if s.strategy_type != DOWNSCOPE_DEFAULT_STRATEGY_TYPE]
    tag_set = set(failure_tags)
    park_default_triggered = (
        overwhelm_level is not None and overwhelm_level >= OVERWHELM_PARK_THRESHOLD
    )

    best_by_group: dict[str, tuple[int, RecoveryStrategyCatalog]] = {}
    for s in active:
        score = len(tag_set & set(s.primary_trigger_tags or []))
        if park_default_triggered and s.strategy_type == PARK_DEFAULT_STRATEGY_TYPE:
            score = max(score, 1)
        if score <= 0:
            continue
        current = best_by_group.get(s.option_group)
        if current is None or (score, -s.display_priority) > (
            current[0],
            -current[1].display_priority,
        ):
            best_by_group[s.option_group] = (score, s)

    cards = [
        s for _, s in sorted(best_by_group.values(), key=lambda t: (-t[0], t[1].display_priority))
    ]

    if len(cards) < min_cards:
        used_groups = {c.option_group for c in cards}
        for s in sorted(active, key=lambda x: x.display_priority):
            if len(cards) >= min_cards:
                break
            if s.option_group in used_groups:
                continue
            cards.append(s)
            used_groups.add(s.option_group)

    cards = cards[:max_cards]

    if escalation_level == "L2":
        environment_shift = next(
            (s for s in active if s.strategy_type == ENVIRONMENT_SHIFT_STRATEGY_TYPE), None
        )
        if environment_shift is not None:
            rest = [c for c in cards if c.option_group != environment_shift.option_group]
            cards = [environment_shift, *rest][:max_cards]

    return cards


def select_renegotiation_strategies(
    strategies: list[RecoveryStrategyCatalog],
) -> list[RecoveryStrategyCatalog]:
    """L3(재협상, 근거 대장 §5.2·#328) 전용 카드 선택 — 태그 매칭과 완전히 무관하다.

    "이번엔 어떤 실패 태그가 왔나"가 아니라 "계획 자체를 어떻게 조정할까"를 묻는 국면이라,
    `select_strategies` 의 점수·패딩 로직을 전혀 안 쓴다. `_RENEGOTIATION_GROUPS`
    (DOWNSCOPE→RESCHEDULE→PARK) 순서대로 각 그룹에서 `display_priority` 가 가장 낮은
    활성 전략 1장씩만 고른다 — 그룹당 항상 최대 1장이라 "동시 노출 1카드" 규칙과
    자연히 일치한다.

    `DOWNSCOPE_DEFAULT` 는 여기서도 뺀다 — 규칙 7(L1 축소→분해)과 같은 이유: "오늘은
    절반만" 류 모호한 비율 축소는 "목표 자체를 줄인다"는 재협상의 취지와 다르다.

    카탈로그에 어떤 그룹이 활성 전략을 하나도 안 갖고 있으면 그 자리는 그냥 빠진다
    (강제로 만들어내지 않는다 — `select_strategies` 규칙 4 의 패딩과 달리 여기는 정확히
    3방향이라는 계약이라 다른 그룹으로 대체하면 재협상의 의미가 깨진다). 정상 운영이라면
    카탈로그가 세 그룹 모두에 활성 전략을 갖고 있어야 완료 조건("정확히 3장")이 성립한다.
    """
    active = [
        s for s in strategies if s.is_active and s.strategy_type != DOWNSCOPE_DEFAULT_STRATEGY_TYPE
    ]
    cards = []
    for group in _RENEGOTIATION_GROUPS:
        candidates = [s for s in active if s.option_group == group]
        if not candidates:
            continue
        cards.append(min(candidates, key=lambda s: s.display_priority))
    return cards


def first_matching_tag(failure_tags: list[str], strategy: RecoveryStrategyCatalog) -> str | None:
    """카드의 trigger_tag 기록용 — 전략의 primary 태그 중 실제 매칭된 첫 태그."""
    primary = strategy.primary_trigger_tags or []
    for tag in failure_tags:
        if tag in primary:
            return tag
    return None


def recovery_target_date(
    decided_on: date, option_group: str, *, re_engagement_on: date | None = None
) -> date:
    """회복 카드를 언제 할 것인가 — 기본은 결정한 날, CARRY_OVER 만 '내일로 이어가기'.

    제품 규칙(UX 4 그룹)이라 HTTP 핸들러가 아니라 여기 산다. DOWNSCOPE 는 "지금 작게라도
    해보기"라 같은 날, CARRY_OVER 는 그룹 이름 그대로 하루 뒤다.

    `re_engagement_on` — 사용자가 CARRY_OVER 를 고르며 **직접 고른** 재관여 날짜(#327 앵커,
    KST 달력일). 있으면 카드도 그날로 간다: 화면은 "금요일에 다시 확인할게요"라고 약속했는데
    카드는 내일에 놓이고 알림은 금요일에 와서, 설정이 반영되지 않은 것처럼 보였다. 내일보다
    이르면(오늘 등) 내일로 둔다 — '이어가기'가 오늘 안으로 당겨지면 그룹의 뜻이 사라진다.
    """
    if option_group != _CARRY_OVER_GROUP:
        return decided_on
    tomorrow = decided_on + timedelta(days=1)
    if re_engagement_on is None:
        return tomorrow
    return max(re_engagement_on, tomorrow)


def re_engagement_anchor_at(option_group: str, decided_at: datetime) -> datetime | None:
    """언제 다시 찌를까 — PARK/CARRY_OVER 수락 시에만 채운다 (근거 대장 §3 S8).

    **PARK 는 새 카드를 안 만든다**(`_GROUP_TO_SOURCE` 에 없음 — DOWNSCOPE/CARRY_OVER 만
    있음) — 이 앵커가 없으면 "보류"가 곧 "영영 안 돌아옴"이 된다. 카탈로그 템플릿이
    이미 "다음 주 리뷰 때 다시 보는 건 어때요?"라고 약속하므로, 앵커도 그 약속 그대로
    **다음 주(오늘이 월요일이어도 이번 주가 아니라 다음 주) 월요일** 아침으로 잡는다
    (C5 프레시 스타트 — 새 주가 랜드마크).

    CARRY_OVER 는 이미 `recovery_target_date()` 로 내일 카드를 만들지만, 그 카드의
    실행 여부와 무관하게 "재관여를 다시 챙길 시점" 자체는 별도 필드로 명시적으로 남긴다
    (A3 — 이탈과 재관여는 별개 역량이라 같은 필드로 묶지 않는다).

    DOWNSCOPE/RESCHEDULE 은 오늘 안에 끝나거나 이미 재배치되어 새 접점이 필요 없다 —
    `None`.
    """
    if option_group == _CARRY_OVER_GROUP:
        anchor_date = decided_at.date() + timedelta(days=1)
    elif option_group == _PARK_GROUP:
        this_monday = decided_at.date() - timedelta(days=decided_at.weekday())
        anchor_date = this_monday + timedelta(days=7)
    else:
        return None
    return decided_at.replace(
        year=anchor_date.year,
        month=anchor_date.month,
        day=anchor_date.day,
        hour=RE_ENGAGEMENT_ANCHOR_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )


def recovery_unit_minutes(min_recovery_unit_minutes: int | None) -> int:
    """회복 카드 소요 시간의 **하한** — 전략의 최소 회복 단위, 없거나 더 짧으면 기본값.

    전략이 비활성이거나 카탈로그에서 사라진 경우 `None` 이 들어온다.

    예전에는 이 값이 곧 회복 카드의 소요 시간이었다. 지금은 `recovery_action_minutes` 의
    하한으로만 쓰인다 — 실제 길이는 원본 카드에서 파생한다.
    """
    if min_recovery_unit_minutes is None:
        return DEFAULT_RECOVERY_MINUTES
    return max(min_recovery_unit_minutes, DEFAULT_RECOVERY_MINUTES)


def _round_to_recovery_step(minutes: float) -> int:
    """회복 눈금(5분)으로 반올림 — 카탈로그의 최소 회복 단위가 전부 5분 배수라 결과도 맞춘다."""
    return int(round(minutes / RECOVERY_MINUTE_STEP) * RECOVERY_MINUTE_STEP)


def recovery_action_minutes(
    *,
    option_group: str,
    original_minutes: int | None,
    min_recovery_unit_minutes: int | None,
) -> int:
    """회복 카드의 소요 시간 — **원본 카드 길이에서 파생**한다.

    예전에는 그룹과 무관하게 `recovery_unit_minutes` (전략 카탈로그 상수, 5~30분)를 그대로
    썼다. 원본을 이미 로드해 두고 `category` 만 상속하던 자리다. 그 결과 두 방향으로 어긋났다.

    - **CARRY_OVER 가 길이를 잃었다.** 정의상 '내일로 그대로 옮기기' 인데 3시간짜리 카드가
      5분 카드가 됐다. 계획 길이가 균일할 때는 눈에 안 띄었지만, 길이가 내용에 따라 갈리면
      (ADR-0009) 곧바로 드러나는 결함이다. → **원본 그대로**.
    - **DOWNSCOPE 가 확대가 될 수 있었다.** 전략의 최소 회복 단위가 30분인데 원본이 15분이면
      "범위를 좁혀 다시" 가 카드를 두 배로 늘렸다. → 상한을 **원본**으로 묶는다.

    DOWNSCOPE 는 원본의 `DOWNSCOPE_RETAIN_RATIO` 만 남기되 `[하한, 원본]` 으로 클램프한다.
    하한은 `recovery_unit_minutes` (= `max(전략값, DEFAULT_RECOVERY_MINUTES)`) 다 —
    전략값만 쓰면 카탈로그에 0 인 전략이 있어 종전보다 짧은 카드가 나온다. 그 하한마저
    원본보다 크면 원본이 이긴다(위 '확대' 방지). 즉 "이미 충분히 작은 카드는 그대로 둔다".

    원본을 못 읽으면(카드가 사라진 경우) 종전대로 하한을 쓴다.

    `option_group` 이 여기 닿는 건 DOWNSCOPE/CARRY_OVER 뿐이다 — 새 ActionItem 을 만드는
    그룹이 그 둘뿐이라서다(api-contract §12). CARRY_OVER 가 아니면 축소로 본다.
    """
    floor = recovery_unit_minutes(min_recovery_unit_minutes)
    if original_minutes is None or original_minutes <= 0:
        return floor
    if option_group == _CARRY_OVER_GROUP:
        return original_minutes
    scaled = _round_to_recovery_step(original_minutes * DOWNSCOPE_RETAIN_RATIO)
    return max(min(floor, original_minutes), min(scaled, original_minutes))


def _ceil_to_quarter(dt: datetime) -> datetime:
    """15분 격자로 **올림** — 결과가 항상 `>= dt` 임을 보장한다.

    `first_plan._ceil_quarter` 와 이름은 비슷하지만 그쪽은 초를 먼저 버려 결과가 입력보다
    앞설 수 있고, 그 모듈은 repository·langgraph 를 import 해 순수 모듈에서 재사용할 수 없다
    (AGENTS.md §5 import 방향). 이름을 달리 둔 것은 의도적이며 공용화는 후속이다.
    """
    floored = dt.replace(minute=dt.minute - dt.minute % 15, second=0, microsecond=0)
    return floored if floored == dt else floored + timedelta(minutes=15)


def _at_wall_time(anchor: datetime, wall: time) -> datetime:
    """`anchor` 와 같은 날짜·시간대의 `wall` 시각 — 벽시계로만 옮긴다(일 단위 시프트와 같은 이유)."""
    return anchor.replace(hour=wall.hour, minute=wall.minute, second=0, microsecond=0)


def place_in_activity_window(
    earliest: datetime,
    *,
    estimated_minutes: int,
    window_start: time,
    window_end: time,
) -> datetime:
    """`earliest` 이후로 **사용자의 활동 시간대 안**에 들어가는 가장 이른 시각.

    지금 창 안이고 끝까지 들어가면 `earliest` 그대로, 아니면 **다음에 창이 열리는 시각**이다.
    창이 자정을 넘길 수 있다(`window_end <= window_start`, 예: 22:00~02:00) — 밤 사람의 하루는
    날짜로 끊기지 않으므로 새벽 00:30 은 '어제 저녁에 열린 창' 안이지 다음날 아침이 아니다.
    `window_start == window_end` 는 "하루 종일"로 읽는다(설정이 허용하는 값) — 미룰 이유가 없다.

    창이 열리는 시각은 15분 격자로 올린다 — 사용자가 08:07 같은 시각을 넣어도 회복 블록은
    주간 그리드·15분 편집기와 같은 눈금에 놓인다.
    """
    if window_start == window_end:
        return earliest

    opens_today = _at_wall_time(earliest, window_start)
    closes_today = _at_wall_time(earliest, window_end)
    ends_at = earliest + timedelta(minutes=estimated_minutes)

    if window_start < window_end:
        # 같은 날 안에서 열고 닫는 창 (예: 08:00~16:00).
        if earliest < opens_today:
            return _ceil_to_quarter(opens_today)
        if ends_at <= closes_today:
            return earliest
        return _ceil_to_quarter(opens_today + timedelta(days=1))

    # 자정을 넘기는 창 (예: 22:00~02:00).
    if earliest >= opens_today:  # 오늘 저녁에 열린 창 안 — 창은 내일 새벽에 닫힌다.
        if ends_at <= closes_today + timedelta(days=1):
            return earliest
        return _ceil_to_quarter(opens_today + timedelta(days=1))
    if earliest < closes_today:  # 어제 저녁에 열린 창의 새벽 꼬리.
        if ends_at <= closes_today:
            return earliest
        return _ceil_to_quarter(opens_today)  # 오늘 밤 다시 열릴 때
    # 창이 닫혀 있는 낮 — 오늘 저녁에 열릴 때.
    return _ceil_to_quarter(opens_today)


def shift_to_recovery_day(
    plan_start_at: datetime,
    *,
    original_target_date: date,
    recovery_target_date: date,
    estimated_minutes: int,
    now: datetime,
    activity_start: time | None = None,
    activity_end: time | None = None,
) -> tuple[datetime, datetime]:
    """회복 카드 제안 시각 — **날짜는 일(day) 단위 시프트가, 시각은 과거 배치 보정으로** 정한다.

    일 단위 시프트라 시간대(KST/UTC offset)는 그대로 보존된다 — KST 는 DST 가 없어 UTC
    인스턴트에 정수 일을 더하면 벽시계 시각이 유지된다. 룰 기반이라 freebusy·time_policies
    와는 무관하다 (api-contract §12, 명시적 비목표). 시프트 결과는 항상 15분 격자로 올림한다.

    **과거 배치 보정 (#174, 야간·익일 보정은 #258 도그푸딩 결함 수정)**: 시프트 결과가 이미
    지난 시각이면 `now + RECOVERY_MIN_LEAD_MINUTES` 를 15분 격자로 올린 시각(`earliest`)까지
    앞당긴 뒤, **사용자가 '이 시간대에 움직여요' 라고 답한 활동 시간대 안**에 놓는다
    (`place_in_activity_window`). `earliest` 가 그 창 안이고 블록이 창 안에서 끝나면 그대로,
    아니면 **다음에 그 창이 열리는 시각**이다 — "과거에 멈춰 있는 것"보다 "하루 늦게라도
    미래에 놓이는 것"이 낫다는 판단은 그대로 두되, 기준을 07시가 아니라 **그 사람의 창**으로
    옮겼다.

    `activity_start`/`activity_end` 를 모르면(둘 중 하나라도 없으면) 종전 07:00~23:00
    (`RECOVERY_DEFAULT_WINDOW_*`)을 쓴다 — 동작이 달라지지 않는다. 왜 창을 받는가: 예전엔
    07/23 을 박아 둬서, 08~16시에만 시간이 난다고 답한 사용자가 16:50 에 회복을 고르면
    블록이 **17:15**(이미 일과가 끝난 시각)에 잡혔고, 22~02시에 공부하는 사람의 00:30 회복은
    자는 시간인 **07:00** 으로 밀렸다. 둘 다 "사용자가 없다고 말한 시간"에 회복을 놓은 것이다.

    왜 창 밖으로는 안 미는가: 블록 생성 경로(`ScheduledBlockRepo.create_block`)는 시간 정책
    검사를 하지 않는데, 사용자가 직접 옮기는 S15 주간 편집기는 활동 시간대 밖을
    `POLICY_VIOLATION`(422)으로 거부한다(그 정책은 활동 시간대의 여집합 = 수면이다).
    서버가 사용자보다 느슨한 블록을 만들지 않기 위한 하한선 — 그래서 창 밖에서는 안 밀고,
    **다음 창**으로 넘긴다(포기하지 않는다).

    회복 카드의 `target_date` 는 이 함수가 건드리지 않는다(순수 함수) — 다음 창 경로는 블록이
    카드 날짜 다음날에 놓이므로, **승인 경로(`approve_replan`)가 카드 `target_date` 를 블록의
    KST 날짜로 맞춘다.** 예전엔 이 어긋남을 "주간 그리드 표기가 조금 어색할 뿐"이라며 그대로
    뒀는데, 오늘 화면은 `target_date` 로만 카드를 고른다 — 밤 10시에 고른 회복이 다음날 07시
    블록으로 잡히고도 다음날 오늘 화면에는 안 떠서, 회복을 골랐는데 사라진 것처럼 보였다.
    보정 자체를 포기하지 않는 이유는 그대로다: 예전엔 어긋남을 피하려고 보정을 포기했는데
    (같은 날 아니면 원본 시프트 결과를 그대로 씀), 도그푸딩 실측(#258 — `recovery_attempts`
    2건 중 완주 0건)에서 21시 이후 결정이나 다음날 뒤늦은 승인이 전부 "영원히 과거인 블록"이
    되어 `pre_card` 스윕도, 사용자 눈에 띌 기회도 영영 없었다 — 과거에 박힌 블록은
    **원리적으로 완주가 불가능**하다.

    왜 보정하는가: 회복 결정은 21시 일괄 회고에서만 일어나고(AGENTS.md §1) DOWNSCOPE 는
    day_delta 가 0 이라, 보정이 없으면 결과가 항상 **이미 지나간 원본 슬롯**이 된다. 과거
    블록은 pre_card 스윕 창(`[now+2m, now+7m)`)을 영영 만나지 못해 알림이 안 가고, 주간
    그리드에서는 실패한 원본 블록과 같은 좌표에 겹쳐 그려진다.

    `now` 는 **aware** 여야 한다(호출자는 `now_kst()`). `now.tzinfo` 를 그대로 쓰고 이
    모듈이 KST/schemas 를 import 하지 않는다 — 순수 함수 계약 유지.
    """
    day_delta = (recovery_target_date - original_target_date).days
    # 시프트 결과도 15분 격자에 맞춘다 — 계획 블록 없이 바로 시작한 카드는 `plan_start_at` 이
    # 클릭 시각(예: 11:35:22.808)이라, 그대로 옮기면 회복 블록이 주간 그리드·15분 편집기와
    # 어긋난 시각에 박혔다. 올림이라 결과가 입력보다 앞서지 않아 아래 과거 판정은 그대로다.
    start_at = _ceil_to_quarter(plan_start_at + timedelta(days=day_delta))

    earliest = _ceil_to_quarter(now + timedelta(minutes=RECOVERY_MIN_LEAD_MINUTES))
    if earliest > start_at:
        # 창을 반쪽만 아는 건 모르는 것으로 본다 — 사용자가 말한 시작에 기본 끝(23시)을
        # 섞으면 본인이 말한 적 없는 시간대가 만들어진다.
        if activity_start is None or activity_end is None:
            window_start, window_end = RECOVERY_DEFAULT_WINDOW_START, RECOVERY_DEFAULT_WINDOW_END
        else:
            window_start, window_end = activity_start, activity_end
        start_at = place_in_activity_window(
            earliest,
            estimated_minutes=estimated_minutes,
            window_start=window_start,
            window_end=window_end,
        )

    return start_at, start_at + timedelta(minutes=estimated_minutes)


# ── v3 코핑 플랜 보조 문장 검사 (recovery-12) ──────────────────────────────────
#
# obstacle/coping_clause/acknowledgment 는 선두 카드 아래 덧붙는 **짧은 한 문장**이다.
# 실측(미러, AVOIDANCE)에서 LLM 이 여기에 타임스탬프·다른 문자권 글자·내부 메타 문장을
# 흘렸다 — "…망설여져요ო2025-02-23T00:00:00Z", 같은 말을 되풀이한 200자 넘는 문단 등.
# Pydantic LLM 스키마에 max_length 를 걸면 위반 하나로 개인화 **전체**(if/then 포함)가
# 룰 폴백으로 버려지므로, 필드 단위로 검사해 그 필드만 비운다.
ACKNOWLEDGMENT_MAX_LENGTH = 60  # 프롬프트 요구 "25자 안팎"의 두 배 남짓
COPING_TEXT_MAX_LENGTH = 120  # obstacle/coping_clause — 프롬프트 예시는 20~40자

# 허용 문자: 한글(음절·자모), 영문·숫자, 공백, 문장에 흔한 문장부호. 그 밖(다른 문자권·이모지·
# 제어문자)이 하나라도 섞이면 생성이 깨진 것으로 본다.
_COPING_TEXT_ALLOWED = re.compile(
    r"[\uAC00-\uD7A3\u3131-\u318E0-9A-Za-z\s.,!?~'\"()\[\]·…\-:;%/+&‘’“”]*"
)
# 날짜·시각 흔적(2025-02-23, 00:00:00) — 사용자 문장에 올 이유가 없다.
_TIMESTAMP_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}:\d{2}")
# 세 글자 이상 영단어 — 카드 제목에 없는 영어는 추론 문장이 새어 나온 흔적이다
# ("wait", "let me" 등). 제목에 있는 영어("SQL", "GROUP BY")는 허용한다.
_ASCII_WORD = re.compile(r"[A-Za-z]{3,}")


def clean_coping_text(text: str | None, *, max_length: int, context_title: str) -> str | None:
    """LLM 이 만든 코핑 플랜 보조 문장 하나를 검사해, 쓸 수 없으면 `None`.

    `None` 이 되는 경우: 비었음 · `max_length` 초과 · 허용 밖 문자 · 날짜/시각 흔적 ·
    `context_title`(원본 카드 제목)에 없는 3글자 이상 영단어. if/then 문구는 건드리지 않는다
    — 이 필드들이 비어도 카드는 그대로 쓸 수 있다(FE 는 값이 있을 때만 그린다).
    """
    cleaned = " ".join((text or "").split())
    if not cleaned or len(cleaned) > max_length:
        return None
    if _COPING_TEXT_ALLOWED.fullmatch(cleaned) is None:
        return None
    if _TIMESTAMP_LIKE.search(cleaned):
        return None
    title_words = {w.lower() for w in _ASCII_WORD.findall(context_title)}
    if any(w.lower() not in title_words for w in _ASCII_WORD.findall(cleaned)):
        return None
    return cleaned
