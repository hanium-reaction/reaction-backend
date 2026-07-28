"""Recovery 후보 선택 룰 엔진 — Orchestrator 2 의 DETECTING→DIAGNOSING rule 경로 (S19).

LLM 0회: `recovery_strategy_catalog.primary_trigger_tags` ↔ 실패 태그 매칭과
`display_priority` 만으로 UX 4 그룹 × 최대 1카드, 총 2~4장을 결정한다 (api-contract §12).
LLM(Recovery Coach)은 선두 카드의 if-then 문구 personalize 에만 쓰이고,
실패 시 본 룰 결과(카탈로그 템플릿)가 그대로 노출된다 (PRD §9 — 8초 fallback).

순수 함수로 유지 — DB/프레임워크 의존 없음 (단위 테스트 대상).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime

    from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog

MIN_CARDS = 2
MAX_CARDS = 4

# 회복 카드가 '내일'로 넘어가는 그룹 — CARRY_OVER 만 하루 뒤다 (UX 4 그룹 중 1).
_CARRY_OVER_GROUP = "CARRY_OVER"

# 카탈로그에 전략이 없을 때(비활성 등) 회복 카드의 기본 소요 시간 — 최소 회복 단위.
DEFAULT_RECOVERY_MINUTES = 5


class _SafeFormatDict(dict[str, str]):
    """템플릿 변수 누락 시 빈 문자열 치환 — `{first_step}` 등."""

    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""


def render_template(template: str, variables: dict[str, str] | None = None) -> str:
    """카탈로그 `if_then_template` 의 `{변수}` 를 치환. 누락 변수는 빈 문자열 + 공백 정리."""
    rendered = template.format_map(_SafeFormatDict(variables or {}))
    return " ".join(rendered.split())


def select_strategies(
    failure_tags: list[str],
    strategies: list[RecoveryStrategyCatalog],
    *,
    min_cards: int = MIN_CARDS,
    max_cards: int = MAX_CARDS,
) -> list[RecoveryStrategyCatalog]:
    """실패 태그 → 전략 카드 선택.

    규칙 (DB 설계서 §6.10 + api-contract §12):
    1. `primary_trigger_tags` 와 실패 태그의 교집합 크기로 점수화.
    2. 같은 option_group 은 최고 점수 1개만 (동점은 display_priority 낮은 쪽).
    3. 점수 내림차순 → display_priority 오름차순으로 최대 `max_cards`.
    4. 매칭이 `min_cards` 미만이면, 아직 없는 그룹에서 display_priority 순으로 패딩
       (태그가 없거나 모호해도 항상 선택지를 보여준다 — "Be on your side").
    """
    active = [s for s in strategies if s.is_active]
    tag_set = set(failure_tags)

    best_by_group: dict[str, tuple[int, RecoveryStrategyCatalog]] = {}
    for s in active:
        score = len(tag_set & set(s.primary_trigger_tags or []))
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

    return cards[:max_cards]


def first_matching_tag(failure_tags: list[str], strategy: RecoveryStrategyCatalog) -> str | None:
    """카드의 trigger_tag 기록용 — 전략의 primary 태그 중 실제 매칭된 첫 태그."""
    primary = strategy.primary_trigger_tags or []
    for tag in failure_tags:
        if tag in primary:
            return tag
    return None


def recovery_target_date(decided_on: date, option_group: str) -> date:
    """회복 카드를 언제 할 것인가 — 기본은 결정한 날, CARRY_OVER 만 '내일로 이어가기'.

    제품 규칙(UX 4 그룹)이라 HTTP 핸들러가 아니라 여기 산다. DOWNSCOPE 는 "지금 작게라도
    해보기"라 같은 날, CARRY_OVER 는 그룹 이름 그대로 하루 뒤다.
    """
    return decided_on + timedelta(days=1) if option_group == _CARRY_OVER_GROUP else decided_on


def recovery_unit_minutes(min_recovery_unit_minutes: int | None) -> int:
    """회복 카드의 소요 시간 — 전략의 최소 회복 단위, 없거나 더 짧으면 기본값.

    전략이 비활성이거나 카탈로그에서 사라진 경우 `None` 이 들어온다.
    """
    if min_recovery_unit_minutes is None:
        return DEFAULT_RECOVERY_MINUTES
    return max(min_recovery_unit_minutes, DEFAULT_RECOVERY_MINUTES)


def shift_to_recovery_day(
    plan_start_at: datetime,
    *,
    original_target_date: date,
    recovery_target_date: date,
    estimated_minutes: int,
) -> tuple[datetime, datetime]:
    """회복 카드 제안 시각 — 원본 계획 시각대를 회복 날짜로 그대로 이동 (api-contract §12).

    일(day) 단위 시프트라 시간대(KST/UTC offset)는 그대로 보존된다 — KST 는 DST 가 없어
    UTC 인스턴트에 정수 일을 더하면 벽시계 시각이 유지된다. 룰 기반이라 freebusy 와 무관.

    ⚠️ 알려진 한계 — DOWNSCOPE 는 `recovery_target_date == 결정한 날` 이라 day_delta 가 0 이
    되고, 회복 결정은 21시 회고에서 일어나므로 결과 시각이 **이미 지나간 원본 슬롯**이 된다.
    계약(api-contract.md §12 "일 단위 시프트")이 이 계산을 명시하고 있어 여기서 임의로
    보정하지 않는다 — 시각 보정은 계약 개정과 함께 가야 한다 (AGENTS.md §8).
    """
    day_delta = (recovery_target_date - original_target_date).days
    start_at = plan_start_at + timedelta(days=day_delta)
    return start_at, start_at + timedelta(minutes=estimated_minutes)
