"""First Plan 경계 어댑터 (ADR-0005 §7.4 규약).

`InterviewOutcome`(경계 계약) → First Plan 오케스트레이터가 쓰는 컨텍스트로 변환한다.
순수 함수 — LLM/DB 무관.

- `context_from_outcome`: LLM 분해 프롬프트(`planning/goal_decompose`) 변수 + 룰
  스케줄러(`goal_structuring.GoalStructuringInput`) 조립에 쓸 요약 dict.
- `time_policies_from_outcome` / `action_placements`: 룰 스케줄러
  (`goal_structuring.py`) 가 free/busy 계산·배치에 그대로 쓰는 구조적 입력으로 환원.
  ORM 없이 Protocol(TimePolicyLike/HabitLike)만 만족시키므로 LLM/DB 무관.
- 실제 DB 영속화(`db_apply_first_plan`)는 사용자 [수락] 후 라우터/SAVING 노드에서만
  수행 (AGENTS.md §1.4 자동 적용 금지) — 본 베이스라인에서는 시그니처만 정의.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import (
    ACTION_CATEGORY_VALUES,
    ActionItem,
)
from reaction_backend.db.models.goal import GOAL_CATEGORY_VALUES, GOAL_TIER_VALUES, Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.orchestrator.goal_structuring import (
    BusyBlock,
    DraftPlan,
    DraftScheduledBlock,
    HabitLike,
    PolicyViolationError,
    TimeInterval,
    TimePolicyLike,
    policy_guarded_transaction,
)
from reaction_backend.orchestrator.interview_adapter import is_placeholder_goal
from reaction_backend.orchestrator.plan_scheduler import PlanAction, PlanWindow
from reaction_backend.schemas.common import KST, now_kst, to_kst
from reaction_backend.schemas.interview import GoalCandidate, InterviewOutcome, TimeRange
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalDecomposition,
    GoalNodeDraft,
    MilestoneDraft,
    ScheduledBlockPreview,
)

_log = logging.getLogger(__name__)

# GoalNodeDraft.node_type(root/branch/leaf, LLM) → goal_nodes.node_type enum(core/subgoal/.../leaf).
_NODE_TYPE_MAP = {"root": "core", "branch": "subgoal", "leaf": "leaf"}

# 계획 분량(밀도) 프리셋 → decompose 프롬프트에 넘길 '주당 목표 세션 수' 하한.
# 사용자가 재생성 시 고른 density 를 여기서 구체 숫자로 환원한다(FE 는 라벨만 안다).
_DENSITY_SESSIONS_PER_WEEK: dict[str, int] = {"light": 3, "standard": 5, "intense": 8}
_DEFAULT_SESSIONS_PER_WEEK = 5

# 하루 집중 총량 상한(분)도 density 에 연동한다. 분해가 세션을 더 만들어도 캡이 그대로면
# 초과분이 뒷날로만 밀리므로(특히 scope="week"), 사용자가 고른 분량만큼 하루 밀도도 함께 올린다.
# standard=180 은 기존 기본값(DEFAULT_DAILY_FOCUS_CAP_MIN)과 동일 — 하위호환.
_DENSITY_DAILY_CAP_MIN: dict[str, int] = {"light": 120, "standard": 180, "intense": 240}

# 목표별 주당 가용 시간(goals.weekly_time)이 있으면 세션 수를 그 '실제 시간'으로 산정하고,
# density 는 그 위에서 밀어붙임/여유를 조절하는 가감 배율로 남긴다(둘 다 의미 유지).
_DENSITY_MULTIPLIER: dict[str, float] = {"light": 0.7, "standard": 1.0, "intense": 1.3}
# weekly_hours 를 세션 수로 나눌 때의 기본 세션 길이(분) — focus_duration 미입력 시.
_DEFAULT_SESSION_MIN = 50
# 산정 세션 수 범위 — 주 2회 미만은 계획 유지가 어렵고, 14(하루 2회)면 충분한 상한.
_MIN_SESSIONS_PER_WEEK = 2
_MAX_SESSIONS_PER_WEEK = 14
# 빈도로 주당 시간을 나눌 때 세션이 무의미하게 짧아지지 않도록 두는 하한(분).
# 예: 주 1시간 + 매일 → 8.6분이지만 10분으로 올려 '시작할 수 있는 크기'를 유지한다.
_MIN_PLANNED_SESSION_MIN = 10
# 주당 분량이 말한 값에 이 정도 못 미치는 건 반올림·배치 여유로 보고 경고하지 않는다(잔소리 방지).
_SHORTFALL_TOLERANCE_MIN = 30


# 참고 자료 원문을 프롬프트에 실을 때 최대 길이(자) — 붙여넣기가 길면 토큰 budget 을 먹으므로
# 앞부분만 싣는다. 대부분의 강의계획서·요구사항 요지는 앞쪽에 있다.
_MATERIALS_MAX_CHARS = 2000


def _clip(text: str) -> str:
    """자료 원문을 프롬프트용으로 앞부분만 자른다(길면 절단 표시)."""
    text = text.strip()
    if len(text) <= _MATERIALS_MAX_CHARS:
        return text
    return text[:_MATERIALS_MAX_CHARS] + " …(이하 생략)"


# 링크만 적힌 답을 걸러내기 위한 URL 패턴 (http(s):// 또는 www. 로 시작하는 토큰).
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def materials_is_link_only(note: str | None) -> bool:
    """참고 자료 답이 **링크뿐**이면 True — 즉 실제 내용이 없다는 뜻.

    슬롯은 "내용을 그대로 붙여넣어 주세요" 라고 묻지만 사용자는 링크를 넣기 쉽다. 우리는
    링크를 열어볼 수 없는데, 링크 문자열이 들어있다는 이유로 '자료 있음' 으로 취급하면
    분해 프롬프트의 `materials_referenced_but_missing` 가드가 발동하지 않는다. 그러면 LLM 이
    내용을 아는 척하며 지어낸다 — 실측: 링크만 준 강의 목표에서 존재 여부도 모르는 '20강'
    구성을 만들어냈고 policy_violations 는 비어 있었다.

    URL 을 걷어낸 뒤 남는 게 없으면(구두점·공백뿐) 링크뿐인 것으로 본다. 링크와 함께 설명을
    적었으면 그 설명은 실제 내용이므로 그대로 살린다.
    """
    if not note:
        return False
    return not _URL_RE.sub(" ", note).strip(" \t\r\n-–—·,.:;/|()[]")


def extract_urls(note: str | None) -> list[str]:
    """자료 답에 적힌 URL 들 (원문 순서). `www.` 로 시작하면 https 를 붙여 돌려준다.

    `materials_is_link_only` 와 **같은 패턴**을 써야 한다 — 링크뿐이라고 판정해 놓고
    정작 열 URL 을 못 찾으면 그 자료는 영영 `(없음)` 이다 (#226).
    """
    if not note:
        return []
    found = [m.rstrip(".,;:)]}>\"'") for m in _URL_RE.findall(note)]
    return [u if u.lower().startswith(("http://", "https://")) else f"https://{u}" for u in found]


# 자료 원문을 감싸는 울타리 — 프롬프트 인젝션 방어의 결정적 축.
#
# `materials` 에는 **우리가 통제하지 않는 텍스트**가 들어온다: 사용자 붙여넣기, 그리고
# #226 이후로는 **임의의 웹 페이지 본문**(`integrations/web_fetch`). 그 안에 "이전 지시를
# 무시하고 …" 같은 문장이 있으면 분해 프롬프트의 규칙을 덮어쓸 수 있다. 자료가 계획의
# 뼈대를 정하는 구조(#226 근거 3)라서, 오염되면 계획 전체가 공격자 의도대로 휘어진다.
#
# 프롬프트 규칙(2차)만으로는 부족하다 — 규칙은 순응에 걸려 있고, 자료가 울타리를 **먼저
# 닫아버리면** 규칙 밖으로 빠져나갈 수 있다. 그래서 원문 안의 울타리 흉내를 결정적으로
# 무력화한다(1차). 이게 이 방어에서 유일하게 100% 보장되는 부분이다.
_MATERIALS_FENCE_OPEN = "-----참고 자료 원문 시작-----"
_MATERIALS_FENCE_CLOSE = "-----참고 자료 원문 끝-----"


def _fence(text: str) -> str:
    """자료 원문을 울타리로 감싼다. 원문 안의 울타리 문자열은 깨뜨려 무력화한다."""
    for marker in (_MATERIALS_FENCE_OPEN, _MATERIALS_FENCE_CLOSE):
        text = text.replace(marker, marker.replace("-", "·"))
    return f"{_MATERIALS_FENCE_OPEN}\n{text}\n{_MATERIALS_FENCE_CLOSE}"


def materials_for_prompt(note: str | None, *, fetched: str | None = None) -> str:
    """분해 프롬프트에 실을 참고 자료 값. 링크뿐이거나 비면 '(없음)'.

    '(없음)' 으로 내려야 프롬프트의 '자료 미제공 flag' 규칙이 걸려, LLM 이 내용을 지어내는
    대신 `materials_referenced_but_missing` 를 남기고 사용자에게 원문을 되묻게 된다.

    `fetched` 는 링크를 열어 가져온 본문(#226). 실제 내용이므로 링크뿐이어도 '(없음)' 이
    아니다 — 못 가져왔으면 None 이 들어와 기존 동작 그대로다.

    내용이 있으면 **울타리로 감싸서** 돌려준다(`_fence`) — 자료는 지시가 아니라 데이터라는
    걸 프롬프트가 구분할 수 있게. '(없음)' 은 감싸지 않는다(프롬프트가 이 문자열을 그대로
    비교한다).
    """
    if fetched:
        return _fence(_clip(fetched))
    if not note or materials_is_link_only(note):
        return "(없음)"
    return _fence(_clip(note))


# 다른 목표를 문구에 몇 개까지 나열할지 — 그 이상은 "외 N개" 로 접는다.
_DEFERRED_GOALS_SHOWN = 3


def other_goals_deferred_notice(outcome: InterviewOutcome) -> str | None:
    """계획이 **가장 무거운 목표 하나에만** 집중했음을 알린다. 목표가 하나면 None.

    First Plan 은 설계상 heaviest 목표 하나만 분해·배치한다(`context_from_outcome` 부터
    `shape_action_plan` 까지 전부 heaviest 기준). 한 번에 하나씩 제대로 굴리는 게 이 제품의
    입장이라 그 자체는 의도된 것이다.

    문제는 **말해주지 않는다**는 것이었다. 실측: 목표 3개를 넣으면 계획엔 heaviest 것만
    16세션 들어가고 나머지 둘은 세션 0·블록 0인데 경고가 하나도 없었다. 승인하면 목표는
    3개 다 저장돼 목표 화면에 뜨므로, 사용자 눈엔 "3개 등록했는데 계획엔 하나뿐" 이고
    이유를 알 수 없다.

    placeholder(#88)는 실제 목표가 아니므로 세지 않는다.
    """
    real = [g for g in outcome.core_goals if not is_placeholder_goal(g)]
    if len(real) < 2:
        return None
    heaviest = next((g for g in real if g.is_heaviest), real[0])
    others = [g.title for g in real if g is not heaviest]
    shown = others[:_DEFERRED_GOALS_SHOWN]
    rest = len(others) - len(shown)
    listed = " · ".join(f"'{t}'" for t in shown) + (f" 외 {rest}개" if rest else "")
    return (
        f"이번 계획은 '{heaviest.title}' 한 가지에 집중했어요. "
        f"{listed}는 이번 계획에 넣지 않았어요 — 다음 계획에서 다룰 수 있어요."
    )


def materials_link_only_warning(
    outcome: InterviewOutcome, *, fetch_notice: str | None = None, fetched: bool = False
) -> str | None:
    """참고 자료를 링크로만 준 경우 사용자에게 되물을 문구. 아니면 None.

    프롬프트 가드는 LLM 이 따라줘야 발동하지만 이건 결정적이다 — 링크만 준 사실은 우리가
    확실히 알기 때문에, LLM 순응에 기대지 않고 직접 알린다(AGENTS §1 — 되묻기).

    #226 이후로는 링크를 **열어보고** 말한다:
    - 열었으면(`fetched`) 되물을 게 없다 → None. 이전처럼 "열어볼 수 없어요" 를 그대로
      내보내면 방금 자료를 반영해 놓고 안 했다고 말하는 거짓말이 된다.
    - 못 열었으면 그 사유를 담은 `fetch_notice` 를 쓴다("로그인이 필요한 페이지라…").
      사유를 못 받았을 때만 기존 문구로 폴백한다.
    """
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    if not materials_is_link_only(heaviest.materials_note):
        return None
    if fetched:
        return None
    if fetch_notice:
        return fetch_notice
    return (
        "참고 자료를 링크로 주셨는데 제가 링크를 열어볼 수는 없어요. "
        "그래서 이번 계획은 목표·완료 기준만으로 잡았어요 — "
        "강의 목차나 요구사항 같은 내용을 그대로 붙여넣어 주시면 그걸 뼈대로 다시 짜드릴게요."
    )


def sessions_per_week_for(density: str) -> int:
    """density 프리셋 → 주당 목표 세션 수. 미지원 값은 표준(5)으로 폴백."""
    return _DENSITY_SESSIONS_PER_WEEK.get(density, _DEFAULT_SESSIONS_PER_WEEK)


def session_min_for(outcome: InterviewOutcome, *, default: int = _DEFAULT_SESSION_MIN) -> int:
    """이 계획(heaviest 목표)의 한 세션 길이(분).

    우선순위: **목표별** goals.session_length(session_length_min) → 전역 energy.focus_duration
    → default. 목표마다 다른 집중 호흡을 반영하려고 목표별 값을 최우선으로 둔다(#per-goal).
    """
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    value = heaviest.session_length_min or outcome.preferences.focus_duration_min
    return value if value and value > 0 else default


def planned_session_min_for(outcome: InterviewOutcome) -> int:
    """실제로 배치할 한 세션의 길이(분) — **빈도와 주당 시간을 화해시킨 값**.

    `session_min_for` 는 '한 번에 집중 가능한 시간'(**용량**)이고, 이 함수는 '이번 계획에서
    한 세션을 몇 분으로 잡을지'(**배분**)다. 둘을 나눈 이유:

    빈도(goals.frequency)는 *며칠* 하느냐(케이던스)이고 주당 시간(goals.weekly_time)은
    *총 얼마나* 하느냐(볼륨)다. 서로 다른 축인데 예전에는 빈도가 있으면 주당 시간을 산술에서
    아예 무시해, 세션 수만 빈도로 잡고 길이는 집중 용량으로 고정했다. 그래서 '주 2시간 + 매일'이
    7×60=**주 7시간**으로 부풀고(사용자가 말한 것의 3.5배), '주 8시간 + 주 1회'는 30분으로
    쪼그라들었다. 볼륨을 빈도로 나눠 **세션 길이**로 흡수하면 두 답이 모두 살아난다.

    상한은 집중 용량(`session_min_for`) — 그보다 길게 잡으면 스케줄러가 `focus_chunk_min`
    으로 쪼개는데, 쪼갠 조각들은 stride 배치에서 **서로 다른 날**로 흩어져 케이던스가 깨진다.
    그래서 용량을 넘는 볼륨은 계획에 담지 않는다(과부하보다 과소가 안전 — 남는 시간은 주간
    재계획이 잇는다). 하한은 `_MIN_PLANNED_SESSION_MIN`.

    빈도·주당 시간 중 하나라도 없으면 화해할 게 없으므로 집중 용량을 그대로 쓴다.
    """
    capacity = session_min_for(outcome)
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    freq, hours = heaviest.frequency_per_week, heaviest.weekly_hours
    if not freq or freq <= 0 or not hours or hours <= 0:
        return capacity
    derived = round(hours * 60 / freq)
    return max(_MIN_PLANNED_SESSION_MIN, min(derived, capacity))


def volume_shortfall_warning(
    outcome: InterviewOutcome, *, planned_minutes: int, span_days: int
) -> str | None:
    """계획에 **실제로 담긴** 주당 분량이 사용자가 말한 주당 시간에 못 미치면 알릴 문구.

    입력(빈도·집중 용량)만으로 계산하면 두 가지를 놓친다:
    1) 분해가 적게 나온 경우 — LLM 이 세션을 목표치보다 적게 만들어도 규칙은 자르기만 하고
       채우지는 않는다. 그러면 안내 문구가 **과대 약속**한다(실측: 주 7시간이라 안내했는데
       실제 배치는 주 4시간).
    2) 마감이 가까워 배치 창이 좁아진 경우.
    그래서 배치 결과(`planned_minutes` / `span_days`)에서 역산해 실제 값으로 말한다.

    부족분을 조용히 삼키지 않고 conflict_report 로 올리는 이유는 그대로다 — 두 답이 서로 맞지
    않는다는 사실 자체가 사용자가 판단할 정보다(AGENTS §1).
    """
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    hours = heaviest.weekly_hours
    if not hours or hours <= 0 or planned_minutes <= 0 or span_days <= 0:
        return None
    stated_min = hours * 60
    actual_weekly_min = planned_minutes * 7 / span_days
    if actual_weekly_min >= stated_min - _SHORTFALL_TOLERANCE_MIN:
        return None

    # 원인이 '한 번에 집중 가능한 시간' 이면 그걸 짚어준다 — 사용자가 바꿀 수 있는 레버라서.
    freq, capacity = heaviest.frequency_per_week, session_min_for(outcome)
    reason = ""
    if freq and freq > 0 and round(stated_min / freq) > capacity:
        reason = (
            f" 주 {freq}회로 나누면 한 번에 {round(stated_min / freq)}분씩 해야 하는데 "
            f"한 번에 집중 가능한 시간을 {capacity}분이라고 하셨거든요 — "
            "횟수를 늘리거나 한 번에 하는 시간을 늘리면 더 담을 수 있어요."
        )
    else:
        reason = " 목표를 더 잘게 나누면 남은 시간도 채울 수 있어요."
    return (
        f"주 {hours}시간 쓸 수 있다고 하셨는데 이번 계획은 "
        f"주 {actual_weekly_min / 60:.1f}시간이에요.{reason}"
    )


# 세션 하한(분). 이보다 짧으면 체크인 단위로서 의미가 없어 하한으로 올린다(9분 garbage 방지).
_MIN_ACTION_MINUTES = 15


def normalize_action_minutes(
    outcome: InterviewOutcome, action_items: list[ActionItemDraft]
) -> list[ActionItemDraft]:
    """목표별 세션 길이(goals.session_length)가 있으면 각 leaf 의 estimated_minutes 를
    **[15분, 계획 세션 길이] 밴드로 클램프**한다(#per-goal 준수 + #225 문제 3).

    예전엔 전부 세션 길이로 **통일**했다 — 그래서 '비자 수령 확인' 같은 짧은 마무리 작업까지
    120분이 됐고, 예상 시간이 실제와 어긋나 주간 용량 계산이 같이 틀어졌다(#225 FE 실측).
    이제 상한(세션 길이)만 강제하고, LLM 이 성격에 맞게 짧게 잡은 값(15분 이상)은 존중한다:

    - 9분 같은 garbage → 하한(15분)으로 올림 (원래 이 함수의 존재 이유).
    - 세션 길이 초과 → 세션 길이로 잘라 'target 세션 × 세션 길이 ≤ 주당 시간' 상한 유지.
    - 그 사이 값 → 그대로 — 분량이 주당 시간에 못 미치면 volume_shortfall_warning 이
      정직하게 알린다(부풀린 예상 시간으로 맞는 척하는 것보다 낫다).

    목표별 세션 길이가 없으면(전역 fallback) 원본을 그대로 둬 기존 동작을 보존한다.
    """
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    if not heaviest.session_length_min or heaviest.session_length_min <= 0:
        return action_items
    session_len = planned_session_min_for(outcome)
    floor = min(_MIN_ACTION_MINUTES, session_len)  # 세션 길이가 15분보다 짧으면 그 값이 하한
    out: list[ActionItemDraft] = []
    for item in action_items:
        clamped = max(floor, min(session_len, item.estimated_minutes))
        out.append(
            item
            if item.estimated_minutes == clamped
            else item.model_copy(update={"estimated_minutes": clamped})
        )
    return out


# 마감이 아주 멀어도 한 번에 계획하는 최대 주 수 — 나머지는 주간 재계획이 이어간다.
#
# 4주(≈한 달)인 이유: 제품이 주간 리포트·재계획과 월간 리포트로 계획을 계속 다듬는다.
# 그보다 먼 구간을 지금 세워봐야 대부분 수정되므로 정밀도가 가짜다. 게다가 분해 LLM 은
# 한 번에 이만큼을 만들어야 하는데, 요구 분량이 커지면 20s(llm_planning_timeout_seconds)
# 안에 못 끝내고 룰 폴백으로 떨어져 **전 구간이 자리표시자**가 된다(실측: 16세션 51s 성공 /
# 20세션 타임아웃). 계획 지평을 한 달로 묶으면 그 벽에서 멀어진다.
_MAX_PLAN_WEEKS = 4

# 분해 LLM 한 번에 **요구할** 최대 세션 수. 계획 지평(_MAX_PLAN_WEEKS)과 별개다 —
# 지평은 '얼마나 멀리 배치하는가', 이건 '한 호출에 몇 개를 지어내라고 시키는가'.
#
# 구조적 이유: 20s 타임아웃(llm_planning_timeout_seconds) 안에 만들 수 있는 항목 수엔 한계가
# 있고, 그걸 넘기면 룰 폴백으로 떨어져 **전 구간이 자리표시자**가 된다 — 일부만 자리표시자인
# 것보다 훨씬 나쁘다. 실측(4주 캡 기준): 12·16·20세션 성공 / 28세션(매일) 폴백.
# 그래서 20 을 넘는 분량은 LLM 에 요구하지 않고, 초과분은 `extend_action_plan_to_horizon` 이
# '이어가기' 회차로 채운다. 앞부분은 항상 구체적인 내용이 남는다.
#
# thinking budget 을 0 으로 낮춰 속도를 버는 길은 막혀 있다 — Gemini 가 400 INVALID_ARGUMENT
# 로 거부한다(실측). 그래서 요구 분량 자체를 묶는 이 방식이 모델 사정에 덜 휘둘린다.
_MAX_LLM_SESSIONS = 20


def horizon_session_target(
    outcome: InterviewOutcome, density: str, *, target_date: date | None = None
) -> int:
    """마감까지(계획 지평 안에서) 필요한 총 세션 수 — 배치·보충이 목표로 삼는 값."""
    return target_sessions_per_week(outcome, density) * _horizon_weeks(target_date, outcome.horizon)


def llm_session_target(
    outcome: InterviewOutcome, density: str, *, target_date: date | None = None
) -> int:
    """분해 LLM 에 **요구할** 세션 수 — 지평 목표를 `_MAX_LLM_SESSIONS` 로 묶은 값."""
    return min(horizon_session_target(outcome, density, target_date=target_date), _MAX_LLM_SESSIONS)


def _horizon_weeks(target_date: date | None, horizon: str | None) -> int:
    """target_date~마감(horizon)이 몇 주인지 (최소 1, 최대 _MAX_PLAN_WEEKS).

    **마감이 없으면 지평 전체(_MAX_PLAN_WEEKS)** 로 본다. '마감 없음' 은 *짧다* 가 아니라
    *끝이 없다* 는 뜻인데, 예전엔 1주를 돌려줘 정반대로 해석했다 — 습관형 목표('매일 운동',
    '주 3회 러닝')가 3세션 / 7일짜리 계획을 받고 끝났다(실측: 주 3회 습관 → 블록 3개).
    마감 있는 목표는 4주를 받는데 습관만 1주라, 사용자에게는 계획이 안 만들어진 것으로 보인다.

    `target_date` 자체가 없으면 계산할 기준이 없으므로 1주(하위호환) — 이 경로는 호출자가
    날짜를 안 넘긴 단위 테스트용이다.

    **이미 지난 마감은 1주** (#231). '마감 없음'(4주)과 같이 취급하면 안 된다 — 마감 없음은
    *끝이 없다* 지만 지난 마감은 *늦었다* 라, 한 달치를 새로 벌이는 게 아니라 따라잡을 만큼만
    잡는 게 맞다. 예전에도 `max(days, 0)` 덕에 값은 1주였지만 그건 우연이라, 의도로 못 박는다.
    """
    if target_date is None:
        return 1
    if not horizon:
        return _MAX_PLAN_WEEKS
    try:
        days = (date.fromisoformat(horizon) - target_date).days
    except ValueError:
        return 1
    if days < 0:
        return 1
    return max(1, min(_MAX_PLAN_WEEKS, -(-days // 7)))


def is_overdue_deadline(horizon: str | None, start_day: date) -> bool:
    """마감이 계획 시작일보다 **이전** 인가 — 이미 지난 마감 (#231).

    인터뷰가 과거 날짜를 되묻지 못하고 그대로 받은 경우를 위한 결정적 백스톱 판정.
    마감이 없거나 파싱 불가면 False (기존 '마감 없음' 경로가 그대로 처리).
    """
    if not horizon:
        return False
    try:
        return date.fromisoformat(horizon) < start_day
    except ValueError:
        return False


def shape_action_plan(
    outcome: InterviewOutcome,
    density: str,
    goal_plan: GoalDecomposition,
    *,
    target_date: date | None = None,
) -> GoalDecomposition:
    """분해 결과를 목표별 세션 길이·주당 시간에 맞춰 결정적으로 다듬는다(#per-goal 준수 보장).

    1) 세션 길이 정규화 — 각 leaf estimated_minutes 를 세션 길이 밴드로(9분 등 방지).
    2) 세션 수 상한 — 주당 rate(weekly_hours 기반 **또는** frequency 기반)가 있으면 **마감까지
       주당 rate 로 담을 수 있는 만큼**(target/주 × 마감까지 주 수)으로 자른다. 주당 분량은
       유지하되 **마감까지 전 구간을 커버**한다(예: 20강 강의는 여러 주에 걸쳐 다 계획). '매일'처럼
       빈도만 준 경우도 LLM 과잉 생성분을 rate 로 잘라 케이던스를 지킨다. 주당 rate 자체는
       스케줄러가 weeks_needed 로 여러 날에 분산. target_date 미지정이면 1주치(하위호환).
       잘려나간 leaf 와 **자식이 하나도 안 남은 branch** 도 함께 제거(`_prune_to_leaves`).

    목표별 입력(session_length / weekly_hours / frequency)이 없으면 각 단계는 no-op → 기존 동작 보존.
    """
    items = normalize_action_minutes(outcome, list(goal_plan.action_items))
    nodes = list(goal_plan.goal_nodes)
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    has_rate = (heaviest.weekly_hours and heaviest.weekly_hours > 0) or (
        heaviest.frequency_per_week and heaviest.frequency_per_week > 0
    )
    if has_rate:
        max_sessions = horizon_session_target(outcome, density, target_date=target_date)
        if len(items) > max_sessions:
            items = items[:max_sessions]
            nodes = _prune_to_leaves(nodes, {a.node_id for a in items})
    return goal_plan.model_copy(update={"action_items": items, "goal_nodes": nodes})


# 사용자가 스스로 실행할 수 없는 '외부 대기' 단계의 강한 신호 (#225 결정적 백스톱).
# 보수적으로 '대기/기다리' 만 잡는다 — '수령'·'확인' 은 실행형 제목("택배 수령 후 조립",
# "오답 확인")에도 흔해 오탐이 더 나쁘다. 넓은 판별은 분해 프롬프트 규칙이 맡고,
# 여기는 프롬프트가 놓친 명백한 것만 걷어낸다.
_WAITING_TITLE_RE = re.compile(r"대기|기다리")


def drop_waiting_steps(goal_plan: GoalDecomposition) -> tuple[GoalDecomposition, list[str]]:
    """'외부 대기' 단계를 세션(action_item) 목록에서 뺀다 — 노드(큰 그림)는 남긴다 (#225).

    "입학허가서 대기" 같은 단계는 상대가 처리해줘야 끝나는 상태라, 세션 카드로 만들면
    사용자가 할 수 없는 일이 오늘 목록에 남고 **회복 제안이 계속 헛돈다**(FE 실측).
    leaf 노드 자체는 트리에 남겨 사용자가 여정의 전체 그림은 볼 수 있게 한다
    (FE 제안 1: "마일스톤(뼈대)에는 남겨").

    반환: (걸러진 계획, 걸러낸 제목 목록 — warnings 고지용).
    """
    dropped = [a.title for a in goal_plan.action_items if _WAITING_TITLE_RE.search(a.title)]
    if not dropped:
        return goal_plan, []
    kept = [a for a in goal_plan.action_items if not _WAITING_TITLE_RE.search(a.title)]
    return goal_plan.model_copy(update={"action_items": kept}), dropped


# 확정 마일스톤 제목 대조용 정규화 — 공백만 걷어낸다. LLM 이 "React 기초" → "React 기초 문법"
# 처럼 살짝 늘리는 경우까지 포함(containment)으로 흡수하려면 그 이상 손대면 안 된다.
_WS_RE = re.compile(r"\s+")


def _norm_title(text: str) -> str:
    return _WS_RE.sub("", text).strip()


def missing_milestone_titles(
    milestones: Sequence[MilestoneDraft] | None, goal_plan: GoalDecomposition
) -> list[str]:
    """사용자가 **확정한** 마일스톤 중 이번 계획에 자리가 없는 것들의 제목 (ADR-0007 §배경 ①).

    판정 기준은 **트리에 노드가 남아 있는가** 다. 두 경로로 노드가 없어진다:

    1. `shape_action_plan` 이 세션 수를 주당 rate 로 자르고 `_prune_to_leaves` 가 leaf 가
       하나도 안 남은 branch 를 버린다 — 재현(마감 4주·주 3회·마일스톤 5개×4세션)에서
       20세션이 12세션이 되며 뒤 두 마일스톤이 통째로 없어졌다.
    2. LLM 이 확정 목록을 무시하고 그 branch 를 아예 안 만든다(프롬프트의 "추가·삭제·병합·
       개명 금지" 불순응).

    사용자에게는 **둘이 같은 일**(내가 확인한 단계가 계획에 없다)이라, 노드 id 를 추적해
    1번만 잡는 대신 **확정 목록 ↔ 최종 트리**를 직접 대조한다.

    ⚠️ **leaf 없이 branch 만 남은 마일스톤은 대상이 아니다.** 그건 프롬프트가 시킨 정상
    동작이고("구간 밖에서야 가능한 뒷단계는 branch 로만 남기고 leaf 를 만들지 마라", #225),
    사용자는 그 단계를 트리에서 그대로 본다 — 이번 구간에 세션이 없다는 사실은
    `window_coverage`·`horizon_coverage_notice` 가 이미 말한다. 여기서 또 알리면 4주를
    넘는 거의 모든 계획에서 경고가 뜬다.

    판정은 보수적으로 — 공백 제거 후 **양방향 containment**. 제목이 조금 늘거나 줄어도
    같은 것으로 본다. 오탐(있는데 없다고 알림)이 미탐보다 나쁘기 때문이다
    (`_WAITING_TITLE_RE`·`_GOAL_GLOSS_RE` 와 같은 원칙).

    `_prune_to_leaves` 가 자식 없는 branch 를 이미 제거하므로, 트리에 남은 노드 = 세션이
    딸린 노드다. 따라서 제목 매칭만으로 '자리가 있다' 를 판정할 수 있다.
    """
    if not milestones:
        return []
    node_titles = [t for t in (_norm_title(n.title) for n in goal_plan.goal_nodes) if t]
    missing: list[str] = []
    for m in milestones:
        key = _norm_title(m.title)
        if not key:
            continue
        if any(key in t or t in key for t in node_titles):
            continue
        missing.append(m.title)
    if missing:
        _log.info(
            "milestones_missing_from_plan",
            extra={"missing": len(missing), "confirmed": len(milestones)},
        )
    return missing


def missing_milestones_notice(missing: list[str], *, confirmed: int) -> str | None:
    """확정 마일스톤이 이번 계획에 안 들어갔음을 알리는 문구. 전부 들어갔으면 None.

    이 레포는 다른 모든 축소(대기 단계 제거·회차 보충·하루 상한 초과·케이던스 미달)를
    `warnings` 로 고지한다. **사용자가 직접 확인한 뼈대가 빠지는 것**만 침묵하고 있었다.

    "사라졌다" 가 아니라 "다음 계획이 이어받는다" 로 말한다 — 한 번에 4주까지만 세우는 건
    의도된 설계이고(`_MAX_PLAN_WEEKS`), 사용자가 할 수 있는 조정(분량↑·마일스톤 줄이기)을
    함께 준다. 금지어 필터(DevBaseline §4.2)를 통과하는 표현만 쓴다.

    **제목 뒤에 조사를 붙이지 않는다.** 은/는·이/가는 받침에 따라 달라져 마일스톤 제목마다
    맞고 틀리는데(`interview_catalog._PLAN_DEFAULT_QUESTIONS` 가 같은 이유로 쉼표 문형을
    쓴다), 목록은 사용자가 지은 제목이라 받침을 알 수 없다. 제목을 절 끝에 두어 조사가
    필요 없는 문형으로 적는다.
    """
    if not missing:
        return None
    listed = " · ".join(f"'{t}'" for t in missing[:3])
    more = f" 외 {len(missing) - 3}개" if len(missing) > 3 else ""
    return (
        f"확정하신 중간 목표 {confirmed}개 중 이번 계획에 아직 넣지 않은 게 있어요 — "
        f"{listed}{more}. 한 번에 4주까지만 세우고 나머지는 다음 계획에서 이어받거든요. "
        "지금 다 담고 싶으면 계획 분량을 늘리거나 중간 목표를 더 굵게 묶어보세요."
    )


def waiting_steps_notice(dropped: list[str]) -> str | None:
    """대기 단계를 세션으로 만들지 않았음을 알리는 문구 — 조용히 빼지 않는다."""
    if not dropped:
        return None
    listed = " · ".join(f"'{t}'" for t in dropped[:3])
    more = f" 외 {len(dropped) - 3}개" if len(dropped) > 3 else ""
    return (
        f"{listed}{more}는 상대의 처리를 기다리는 단계라 오늘 할 일로 만들지 않았어요 — "
        "계획의 큰 그림에는 남아 있고, 때가 되면 재계획에서 이어받아요."
    )


# 분해가 목표치의 이 비율에 못 미치면 마감까지 이어가는 회차 세션으로 보충한다.
# 1.0 으로 두면 반올림 차이마다 보충이 붙으므로 여유를 준다.
_COVERAGE_FLOOR_RATIO = 0.9
# LLM 이 '이 목표는 유한해서 더 못 채운다' 고 스스로 밝히는 사유 코드(프롬프트와 동기화).
_VOLUME_BELOW_HORIZON = "goal_volume_below_horizon"


def extend_action_plan_to_horizon(
    outcome: InterviewOutcome,
    density: str,
    goal_plan: GoalDecomposition,
    *,
    target_date: date | None = None,
) -> GoalDecomposition:
    """분해가 마감까지 못 미치면 **이어가는 회차 세션**으로 채운다.

    실측 문제: 마감이 두 달 뒤(9/30)인데 LLM 이 9세션(=일주일치)만 만들어, 계획이 8/5 에서
    끝났다. 규칙은 자르기만 하고 채우지 않아 그대로 나갔다. 사용자에겐 '두 달짜리 목표인데
    일주일 계획만 나왔다' 로 보인다. 프롬프트에 총 세션 수를 명시(#total_sessions)해 1차로
    막지만, LLM 순응에 계획 커버리지를 걸 수는 없어 결정적 안전망을 둔다.

    보충은 **사용자가 케이던스를 명시한 목표**(frequency_per_week)에만 한다. '매일/주 N회' 는
    마감까지 그 리듬으로 계속하겠다는 약속이라 '이어서 N회차' 가 의미상 맞다. 빈도를 안 준
    목표('몰아서')는 반복이 자연스럽지 않아 건드리지 않는다.

    **마감이 없어도 보충한다** — 마감 없음은 지평 전체(`_MAX_PLAN_WEEKS`)로 계획된다
    (v1.41, `_horizon_weeks`). 예전엔 여기서 horizon 유무로 건너뛰어, '매일' 습관이
    지평은 4주인데 세션은 LLM 상한(`_MAX_LLM_SESSIONS`=20)에서 끊겨 **마지막 8일이
    조용히 비었다**(실측: 매일 30분 달리기 → 20블록/20일, 경고 없음). 성공 기준이
    "한 달 하루도 안 빼먹기"인 사용자에게 계획 자체가 8일 결번이었다.

    LLM 이 `goal_volume_below_horizon` 을 남겼으면 '이 목표는 유한해서 더 못 채운다' 는 판단을
    스스로 밝힌 것이므로 존중하고 보충하지 않는다(프롬프트가 그렇게 지시한다). 억지로 채우면
    항목 수가 정해진 과제에 의미 없는 회차가 붙는다.
    """
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    freq = heaviest.frequency_per_week
    if not freq or freq <= 0:
        return goal_plan
    if any(v.reason == _VOLUME_BELOW_HORIZON for v in goal_plan.policy_violations):
        return goal_plan

    target_total = horizon_session_target(outcome, density, target_date=target_date)
    have = len(goal_plan.action_items)
    if have >= target_total * _COVERAGE_FLOOR_RATIO:
        return goal_plan

    minutes = planned_session_min_for(outcome)
    nodes = list(goal_plan.goal_nodes)
    items = list(goal_plan.action_items)
    root = next((n for n in nodes if n.parent_id is None), None)
    branch_id = "tmp-continue"
    nodes.append(
        GoalNodeDraft(
            node_id=branch_id,
            parent_id=root.node_id if root else None,
            title=f"{heaviest.title} 이어가기",
            node_type="branch",
            order_index=len(nodes),
            is_leaf=False,
        )
    )
    for i in range(target_total - have):
        leaf_id = f"tmp-continue-{i}"
        label = f"{heaviest.title} {have + i + 1}회차"
        nodes.append(
            GoalNodeDraft(
                node_id=leaf_id,
                parent_id=branch_id,
                title=label,
                node_type="leaf",
                order_index=i,
                is_leaf=True,
            )
        )
        items.append(
            ActionItemDraft(
                node_id=leaf_id,
                title=label,
                estimated_minutes=minutes,
                category=heaviest.category,
                first_step="지난 회차에서 이어서 5분만 시작하기",
            )
        )
    return goal_plan.model_copy(update={"goal_nodes": nodes, "action_items": items})


# 계획 마지막 날이 마감보다 이 정도 안쪽이면 '마감까지 덮었다' 로 본다(주말·반올림 여유).
_HORIZON_COVERED_SLACK_DAYS = 3


def horizon_coverage_notice(
    outcome: InterviewOutcome, *, last_planned_day: date | None, target_date: date
) -> str | None:
    """계획이 마감까지 닿지 않을 때 **왜 그런지** 알려주는 문구. 닿으면 None.

    이게 없으면 사용자는 마감이 9/30 인데 계획이 9/21 에서 끝난 걸 보고 **버그로 읽는다**.
    한 번에 계획하는 기간에 상한(`_MAX_PLAN_WEEKS`)을 둔 건 의도된 설계이므로 — 먼 미래를
    자리표시자로 채우는 대신 매주 재계획이 이어간다 — 그 의도를 말해줘야 한다.

    두 가지 이유를 구분한다:
    1) 상한에 걸림(마감까지 8주 초과) — "이번엔 N주치까지, 나머지는 매주 이어서".
    2) 목표 분량이 거기까지 — 유한한 목표라 마감 전에 할 일이 끝나는 정상 상황.
    """
    if not outcome.horizon or last_planned_day is None:
        return None
    try:
        deadline = date.fromisoformat(outcome.horizon)
    except ValueError:
        return None
    if (deadline - last_planned_day).days <= _HORIZON_COVERED_SLACK_DAYS:
        return None  # 사실상 마감까지 덮음 — 말할 것 없음

    days_to_deadline = max((deadline - target_date).days, 0)
    # 캡 판정은 올림(그 주 수만큼 '필요' 하므로), 사용자에게 보여줄 숫자는 반올림
    # (64일을 '약 10주' 라고 하면 과장이라 '약 9주' 로 읽히게).
    weeks_to_deadline = max(1, -(-days_to_deadline // 7))
    if weeks_to_deadline > _MAX_PLAN_WEEKS:
        return (
            f"마감({outcome.horizon})까지는 약 {round(days_to_deadline / 7)}주인데, "
            "한 번에 세우는 계획은 "
            f"{_MAX_PLAN_WEEKS}주까지만 잡아요. 그래서 이번 계획은 {last_planned_day} 까지고, "
            "그 뒤는 매주 재계획에서 진행 상황을 보고 이어서 채웁니다 — 빠뜨린 게 아니에요."
        )
    return (
        f"이번 계획은 {last_planned_day} 까지예요 — 이 목표를 나눈 분량이 거기까지라서요. "
        f"마감({outcome.horizon})까지 남은 기간은 매주 재계획에서 이어집니다. "
        "지금 더 촘촘히 하고 싶으면 계획 분량을 올려서 다시 만들어 보세요."
    )


def overdue_deadline_notice(
    horizon: str | None, *, start_day: date, last_planned_day: date | None
) -> str | None:
    """마감이 이미 지나 있을 때 계획을 어떻게 잡았는지 밝히는 문구 (#231). 아니면 None.

    인터뷰가 지난 마감을 되묻는 게 1차 방어지만, 순응에 걸 수는 없어 마지막에 한 번 더
    말한다. 조용히 넘어가면 사용자는 (1) 왜 이 기간으로 계획이 나왔는지, (2) 지난 날짜가
    아직 목표의 마감으로 남아 있다는 사실을 알 길이 없다. 늦은 걸 지적하지 않고
    ("on your side, not on your case") 새 마감을 정하도록만 이끈다.
    """
    if not is_overdue_deadline(horizon, start_day):
        return None
    span = (
        f" 오늘부터 {(last_planned_day - start_day).days + 1}일에 걸쳐"
        if last_planned_day is not None and last_planned_day >= start_day
        else ""
    )
    return (
        f"적어주신 마감({horizon})이 이미 지난 날짜라, 그 날짜에 맞추면 오늘 하루에 전부 "
        f"몰아넣게 돼요. 대신{span} 따라잡는 흐름으로 잡았어요. "
        "언제까지 끝내고 싶은지 새로 정해주시면 그 기준으로 다시 세울게요."
    )


def coverage_extended_warning(added: int, horizon: str | None) -> str | None:
    """회차 세션으로 보충했음을 알리는 문구 — 내용까지 지어낸 게 아님을 분명히 한다.

    마감 없는 습관형도 보충 대상이라 horizon 이 없을 수 있다 — 그때 "마감까지" 라고 쓰면
    없는 마감을 지어내는 셈이라, 계획 지평(4주) 기준으로 말한다.
    """
    if added <= 0:
        return None
    until = f"{horizon}" if horizon else f"이번 계획 구간({_MAX_PLAN_WEEKS}주)"
    return (
        f"{until}까지 채우려고 '이어가기' 회차 {added}개를 덧붙였어요. "
        "회차의 구체적인 내용은 매주 재계획에서 그때 진행 상황에 맞춰 채워집니다 — "
        "지금 더 구체적으로 짜고 싶으면 참고 자료 내용이나 진행 순서를 알려주세요."
    )


def _prune_to_leaves(nodes: list[GoalNodeDraft], kept_leaves: set[str]) -> list[GoalNodeDraft]:
    """살아남은 leaf 와 그 **조상만** 남긴다.

    비-leaf 를 무조건 살리면 leaf 가 전부 잘려나간 branch 가 자식 없는 껍데기로 남아 화면에
    빈 섹션으로 뜬다(예: 20강 강의가 `_MAX_PLAN_WEEKS` 로 뒤가 잘릴 때). `parent_id` 를 타고
    올라가며 실제로 쓰이는 조상만 표시하고 나머지 branch 는 버린다. root 는 자식이 하나라도
    남으면 조상으로 함께 살아난다.
    """
    by_id = {n.node_id: n for n in nodes}
    alive: set[str] = set()
    for leaf_id in kept_leaves:
        cursor: str | None = leaf_id
        # parent 체인을 따라 올라가며 표시. 순환(LLM 오류)에도 멈추도록 방문 노드는 건너뛴다.
        while cursor is not None and cursor not in alive:
            alive.add(cursor)
            node = by_id.get(cursor)
            cursor = node.parent_id if node else None
    return [n for n in nodes if n.node_id in alive]


def target_sessions_per_week(outcome: InterviewOutcome, density: str) -> int:
    """분해에 넘길 주당 목표 세션 수.

    우선순위:
    1) **빈도(frequency_per_week)** 가 있으면 그 값을 그대로 주당 세션 수로 쓴다 — 사용자가
       케이던스를 명시한 것이므로('매일'=7, '주 3회'=3) density 로 가감하지 않고 존중한다.
       '주 1회' 같은 명시 저빈도도 그대로 살리려 하한(2)을 적용하지 않는다(상한 14만).
    2) 빈도가 없고 주당 가용 시간(weekly_hours)이 있으면 **그 시간을 세션 길이로 나눠** 현실적인
       세션 수를 뽑고 density 배율(light 0.7 / standard 1.0 / intense 1.3)로 가감한다.
    3) 둘 다 없으면 density 프리셋(3/5/8)으로 폴백. 세션 길이는 목표별(session_length) 우선.
    """
    goals = outcome.core_goals
    heaviest = next((g for g in goals if g.is_heaviest), goals[0])
    freq = heaviest.frequency_per_week
    if freq and freq > 0:
        return max(1, min(freq, _MAX_SESSIONS_PER_WEEK))
    hours = heaviest.weekly_hours
    if not hours or hours <= 0:
        return sessions_per_week_for(density)
    capacity = hours * 60 / session_min_for(outcome)
    scaled = round(capacity * _DENSITY_MULTIPLIER.get(density, 1.0))
    return max(_MIN_SESSIONS_PER_WEEK, min(scaled, _MAX_SESSIONS_PER_WEEK))


def daily_cap_for(density: str) -> int:
    """density 프리셋 → 하루 집중 총량 상한(분). 미지원 값은 표준(180)으로 폴백."""
    return _DENSITY_DAILY_CAP_MIN.get(density, DEFAULT_DAILY_FOCUS_CAP_MIN)


def daily_cap_for_plan(outcome: InterviewOutcome, density: str) -> int:
    """이 계획의 하루 집중 상한(분) — density 프리셋과 **세션 길이 중 큰 쪽**.

    프리셋만 쓰면 **세션 하나가 이미 상한을 넘는** 조합에서 상한이 무의미해진다. 실측:
    사용자가 `goals.session_length` 를 "4시간 이상"(240분)으로 답했는데 standard 상한은
    180분이라, 1차 배치의 상한 검사(`used>0 and used+240>180`)가 **이미 뭔가 잡혀 있는
    모든 날에서 탈락**했다. 그러면 세션 대부분이 상한을 무시하는 2차 패스로 넘어가
    사용자가 고른 케이던스('매일')가 무너지고 하루 8시간짜리 날이 생긴다.

    사용자가 "한 번에 4시간" 이라고 답한 이상 **하루 한 세션은 정상**으로 봐야 한다.
    상한을 세션 길이까지 올리면 상한이 다시 '하루에 몇 세션까지'라는 원래 뜻을 갖는다.
    세션이 프리셋보다 짧으면 프리셋이 그대로 이긴다(기존 동작 보존).
    """
    return max(daily_cap_for(density), session_min_for(outcome))


def committed_minutes_by_day(
    existing_busy: Mapping[date, Sequence[BusyBlock]],
) -> dict[date, int]:
    """날짜 → 이미 확정된 집중 시간(분). 하루 상한을 여기서 이어 세게 한다(#190).

    `existing_busy` 는 **승인된 `scheduled_blocks`** 만 담는다(수면·고정일정은 다른 경로).
    즉 여기 있는 시간은 전부 사용자가 하기로 한 집중 작업이라 상한에 포함하는 게 맞다.

    예전엔 상한을 항상 0에서 시작해, 목표가 늘면 각 계획이 저마다 상한을 지켜도 합계는
    아무도 지키지 않았다 — 실측(목표 3개)에서 하루 240분(상한 180분)이 나왔다.
    """
    totals: dict[date, int] = {}
    for day, blocks in existing_busy.items():
        total = sum(max(0, int(b.interval.duration_minutes)) for b in blocks)
        if total:
            totals[day] = total
    return totals


def daily_overload_notice(
    placed: Sequence[DraftScheduledBlock],
    *,
    committed_min_by_day: Mapping[date, int],
    cap_min: int,
    horizon: str | None = None,
) -> str | None:
    """하루 집중 총량이 상한을 넘긴 날이 있으면 그 사실을 알리는 문구. 없으면 None.

    상한은 1차 배치에서만 강제된다 — 마감이 임박하거나 일이 많으면 2차가 '배치할 수 있으면
    배치'로 넘긴다(#fill-available). 그건 세션을 떨어뜨리지 않으려는 의도된 선택이지만,
    말해주지 않으면 사용자는 4시간짜리 하루를 이유 없이 마주친다.

    기존 확정분(다른 목표의 승인된 계획)까지 합쳐서 센다 — 사용자가 그날 실제로 마주할
    총량이 그것이기 때문이다. 가장 무거운 하루 하나만 짚는다(날마다 늘어놓으면 안 읽힌다).

    **마감이 없으면 마감을 이유로 대지 않는다.** 예전엔 `horizon` 을 보지 않고 항상
    "마감까지 담으려면 이만큼이 필요해서예요" 를 붙여, **마감을 입력하지 않은 사용자에게
    없는 마감을 지어냈다**(FE 실측: `goals.deadlines` 를 빈 값으로 두고 계획을 만들었는데
    이 문장이 그대로 나감). 아는 것만 말한다 — #224 와 같은 규칙이다.
    """
    if cap_min <= 0:
        return None
    totals: dict[date, int] = dict(committed_min_by_day)
    for b in placed:
        day = b.interval.start.date()
        totals[day] = totals.get(day, 0) + round(b.interval.duration_minutes)
    worst = max((d for d in totals if totals[d] > cap_min), key=lambda d: totals[d], default=None)
    if worst is None:
        return None
    over = [d for d in totals if totals[d] > cap_min]
    hours = totals[worst] / 60
    tail = f" (이런 날이 {len(over)}일 있어요)" if len(over) > 1 else ""
    reason = (
        f"마감({horizon})까지 담으려면 이만큼이 필요해서예요"
        if horizon
        else "이번 계획 분량을 담으려면 이만큼이 필요해서예요"
    )
    return (
        f"{worst.month}월 {worst.day}일은 집중 시간이 약 {hours:.1f}시간으로 평소 기준"
        f"({cap_min // 60}시간)보다 많아요{tail}. {reason} — "
        f"부담되면 일부를 다음으로 미뤄도 괜찮아요."
    )


# 요청 케이던스의 이 비율에 못 미치면 고지한다. 1.0 으로 두면 반올림·주말 한 칸 차이마다
# 경고가 붙어 잡음이 되므로 여유를 준다.
_CADENCE_OK_RATIO = 0.8


def cadence_shortfall_notice(
    outcome: InterviewOutcome,
    placed: Sequence[DraftScheduledBlock],
    *,
    start_day: date,
    committed_min_by_day: Mapping[date, int],
) -> str | None:
    """사용자가 고른 빈도('매일'·'주 3회')를 못 지켰으면 그 사실과 **이유**를 알린다.

    실측(FE): '매일' 이라고 답했는데 계획은 초반 격일 / 후반 하루 2세션(8시간)으로 나왔고,
    **그 사실을 아무도 말해주지 않았다.** 사용자는 자기가 고른 케이던스가 조용히 반토막
    난 걸 화면에서 직접 세어봐야 알 수 있었다.

    원인은 빈도 처리 자체가 아니라 **이미 승인된 다른 계획**이었다 — 같은 입력을 빈 계정에
    넣으면 28세션이 28일에 정확히 매일 배치된다. 다른 목표의 계획이 하루 69~309분을 쓰고
    있으면 1차 배치가 그 날들을 상한으로 걸러내고, 남은 세션이 2차 패스에서 몰린다.
    그래서 이유까지 함께 말한다 — 원인을 알아야 사용자가 무엇을 바꿀지 정할 수 있다.

    빈도를 안 고른 목표('몰아서')는 지킬 케이던스가 없으므로 None.
    """
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    freq = heaviest.frequency_per_week
    if not freq or freq <= 0 or not placed:
        return None

    days = {b.interval.start.date() for b in placed}
    last = max(days)
    span = max((last - start_day).days + 1, 1)
    actual_per_week = len(days) / span * 7
    if actual_per_week >= freq * _CADENCE_OK_RATIO:
        return None

    wanted = "매일" if freq >= 7 else f"주 {freq}회"
    busy_days = sum(1 for d, m in committed_min_by_day.items() if m > 0 and start_day <= d <= last)
    because = (
        f" 이미 승인된 다른 계획이 그 기간 {busy_days}일을 쓰고 있어서예요."
        if busy_days
        else " 활동 시간대 안에 그만큼의 자리가 나오지 않아서예요."
    )
    return (
        f"'{wanted}' 로 하고 싶다고 하셨는데, 이번 계획은 {span}일 중 {len(days)}일에 잡혔어요."
        f"{because} 케이던스를 지키고 싶으면 계획 분량을 낮춰 다시 만들거나, "
        "기존 계획을 먼저 정리해 주세요."
    )


def pad_busy(blocks: Sequence[BusyBlock], margin_min: int) -> list[BusyBlock]:
    """busy 구간 앞뒤로 `margin_min` 여백을 덧댄 사본 — 1차 배치가 딱 붙지 않게(#191).

    계획 **안에서는** 카드 사이에 휴식을 두면서 다른 목표의 계획과는 0분으로 붙던 문제를
    막는다. 자정을 넘기지 않게 그날 안으로 자른다 — 스케줄러의 free 계산이 하루 단위라
    넘어간 구간은 어차피 버려지고, 앞뒤 날에 잘못 새는 것보다 낫다.
    """
    if margin_min <= 0:
        return list(blocks)
    margin = timedelta(minutes=margin_min)
    padded: list[BusyBlock] = []
    for b in blocks:
        day_start = datetime.combine(b.interval.start.date(), time(0, 0), tzinfo=KST)
        day_end = day_start + timedelta(days=1)
        start = max(b.interval.start - margin, day_start)
        end = min(b.interval.end + margin, day_end)
        if end > start:
            padded.append(BusyBlock(TimeInterval(start, end), b.source, b.label))
    return padded


def context_from_outcome(
    outcome: InterviewOutcome,
    *,
    density: str = "standard",
    target_date: date | None = None,
    fetched_materials: str | None = None,
) -> dict[str, Any]:
    """InterviewOutcome → First Plan 컨텍스트 dict.

    `fetched_materials` 는 링크를 열어 가져온 자료 본문(#226) — I/O 는 호출자
    (`first_plan.validate_inputs`)가 하고 여기는 값만 받는다. 이 파일의 "순수 함수" 계약을
    지키기 위해서다.

    LLM 프롬프트 변수는 모두 문자열로 평탄화한다(`prompts.registry` 의 {{var}} 치환 계약).
    availability / preferences 원본 객체도 함께 실어 룰 스케줄러 어댑터가 재사용.
    `density` 는 생성 요청에서 온 계획 분량 프리셋 — '주당 세션 수' 하한으로 프롬프트에 전개.
    `target_date` 는 계획 시작일 — 마감까지 남은 주 수·총 세션 수를 계산해 프롬프트에 싣는다
    (미지정이면 1주치로 본다).
    """
    goals = outcome.core_goals
    heaviest = next((g for g in goals if g.is_heaviest), goals[0])
    per_week = target_sessions_per_week(outcome, density)
    horizon_weeks = _horizon_weeks(target_date, outcome.horizon)
    window_coverage = _window_coverage(target_date, outcome.horizon, horizon_weeks)

    # 시간 배치·일정 충돌은 룰 스케줄러(schedule_blocks)가 전담하므로 decompose 프롬프트에
    # freebusy 를 싣지 않는다 (과거 "" 빈 값이라 LLM 에 무의미했다). review_feedback 은
    # 재분해(replan) 시 first_plan.decompose_goal 이 직전 리뷰 피드백으로 채운다.
    prompt_vars: dict[str, str] = {
        "goal_title": heaviest.title,
        "why_now": heaviest.why_now or "",
        # 완료 기준(DoD) — 인터뷰가 goals.success_image 로 이미 수집하나 그동안 decompose 에
        # 안 실려 버려졌다. 분해가 '무엇을 달성하면 끝인지' 를 알아야 leaf 가 목표에 정렬된다(#B).
        "success_image": heaviest.success_image or "(미입력)",
        # 현재 수준(baseline) — 이미 한 단계를 다시 시키지 않도록 분해가 여기서부터 시작한다(#B).
        # 미응답은 success_image 와 같은 '(미입력)' 센티넬로 — 슬롯 신설(#B) 이전 세션과 [충분해요]
        # 조기 종료는 이 슬롯이 비는데, "처음 시작" 으로 채우면 '모름' 이 '입문자' 라는 단정으로
        # 바뀌어 이미 진도 나간 사용자에게 입문 단계를 다시 시킨다.
        "current_level": heaviest.current_level or "(미입력)",
        "category": heaviest.category,
        "horizon": outcome.horizon or "",
        # 이 목표에 주당 투입 가능한 시간 — 분해가 분량을 사용자의 실제 시간에 맞추게 한다(#weekly).
        "weekly_hours": f"{heaviest.weekly_hours}시간" if heaviest.weekly_hours else "(미입력)",
        # 이 계획에서 한 세션을 몇 분으로 잡을지 — 각 세션(leaf) 길이를 이에 맞춘다.
        # 집중 용량이 아니라 **빈도로 주당 시간을 나눈** 값이라, 프롬프트가 만드는 세션 길이가
        # 그대로 주당 시간과 맞아떨어진다(#per-goal session length).
        "session_length": f"{planned_session_min_for(outcome)}분",
        # 사용자가 밝힌 접근 방식 — 분해가 일반적 방식이 아니라 이 방향을 따르게 하는 grounding
        # (#approach). 미입력이면 '(없음)'.
        "approach_note": heaviest.approach_note or "(없음)",
        # 참고 자료 **원문** — 분해가 그 실제 내용(기능·목차·요구사항)을 뼈대로 삼게 한다(#materials).
        # 길면 앞부분만(토큰 budget). 링크뿐인데 **열지도 못했으면** '(없음)' 으로 내려서
        # 프롬프트의 미제공 flag 규칙이 걸리게 한다(지어내기 방지) — materials_for_prompt 참고.
        "materials": materials_for_prompt(heaviest.materials_note, fetched=fetched_materials),
        "behavioral_summary": _behavioral_summary(outcome),
        "time_policy_summary": _time_policy_summary(outcome),
        "sessions_per_week": str(per_week),
        # 마감까지 남은 기간과 총 세션 수를 **미리 계산해서** 넘긴다. 예전엔 마감 날짜만 주고
        # "남은 주 수에 비례해 만들라" 고 시켰는데, 프롬프트에 **오늘 날짜가 없어** LLM 이 그
        # 계산을 할 수 없었다. 그래서 마감이 두 달 뒤여도 한 주치만 만들곤 했다(실측).
        "target_date": target_date.isoformat() if target_date else "(오늘)",
        "horizon_weeks": str(horizon_weeks),
        # 이번 구간이 마감까지 전체를 덮는지, 앞부분만 덮는지 (#225). 예전엔 이 정보가 없어
        # "마감까지 전 구간을 덮어라" 규칙이 몇 달짜리 여정 전체를 4주치 세션으로 압축했다 —
        # 지금 할 수 있는 일과 다섯 달 뒤에나 가능한 일이 같은 창에 들어왔다(FE 실측).
        "window_coverage": window_coverage,
        # 한 호출에 요구하는 양은 _MAX_LLM_SESSIONS 로 묶는다. 넘기면 타임아웃 → 룰 폴백 →
        # 전 구간 자리표시자가 되기 때문. 초과분은 배치 단계에서 '이어가기' 회차로 채운다.
        "total_sessions": str(llm_session_target(outcome, density, target_date=target_date)),
    }

    return {
        "prompt_vars": prompt_vars,
        "core_goals": [g.model_dump() for g in goals],
        "availability": outcome.availability.model_dump(),
        "preferences": outcome.preferences.model_dump(),
        "horizon": outcome.horizon,
        "unresolved_slots": list(outcome.unresolved_slots),
    }


def _window_coverage(target_date: date | None, horizon: str | None, horizon_weeks: int) -> str:
    """이번 계획 구간이 마감 대비 어디까지 덮는지 — decompose 프롬프트의 분량 판단 근거 (#225).

    LLM 은 날짜 산술을 못 하므로(오늘·마감 날짜만 주면 몇 주짜리인지 모른다) 문장으로
    미리 계산해 준다. 세 경우:
    - 마감이 구간 안 → "전부 덮는다": 마감까지 전 단계를 세션화해도 된다.
    - 마감이 구간 밖 → "앞부분만 덮는다": 구간 안에서 진행 가능한 앞 단계만 세션화.
    - 마감 없음(습관형) → 계속되는 리듬의 첫 구간.
    """
    if not horizon or target_date is None:
        return f"마감이 없다 — 이번 구간({horizon_weeks}주)은 계속 이어질 리듬의 첫 구간이다."
    try:
        deadline = date.fromisoformat(horizon)
    except ValueError:
        return f"이번 구간({horizon_weeks}주)이 계획 범위다."
    total_weeks = max(1, -(-max((deadline - target_date).days, 0) // 7))
    if total_weeks > horizon_weeks:
        return (
            f"마감까지 약 {total_weeks}주 중 이번 구간은 **앞 {horizon_weeks}주만** 덮는다 — "
            "구간 안에서 실제로 진행 가능한 앞 단계만 세션으로 만들어라. 뒷단계는 매주 "
            "재계획이 이어받는다."
        )
    return f"이번 구간({horizon_weeks}주)이 마감까지 전부를 덮는다."


def _behavioral_summary(outcome: InterviewOutcome) -> str:
    p = outcome.preferences
    parts = [f"회복 톤: {p.recovery_tone}", f"휴식 제안 수용: {p.rest_ok}"]
    if p.focus_duration_min:
        parts.append(f"집중 지속: {p.focus_duration_min}분")
    if p.weekly_energy:
        parts.append(f"이번 주 컨디션: {p.weekly_energy}")
    return " / ".join(parts)


def _time_policy_summary(outcome: InterviewOutcome) -> str:
    a = outcome.availability
    parts = [f"활동: {a.activity_window.start}~{a.activity_window.end}"]
    if a.peak_window:
        parts.append(f"피크: {', '.join(a.peak_window)}")
    if a.no_touch_windows:
        parts.append(f"노터치: {len(a.no_touch_windows)}건")
    return " / ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# 룰 스케줄러 입력 어댑터 (schedule_blocks 노드용, LLM 0회)
#
# goal_structuring.py 의 free/busy 계산·배치 알고리즘은 ORM 모델이 아니라 구조적 타입
# (Protocol) 만 요구한다. InterviewOutcome 의 가용 시간/선호를 그 Protocol 을 만족하는
# 경량 dataclass 로 환원해 룰 스케줄러를 그대로 재사용한다 (ADR-0005 §1.2).
# ─────────────────────────────────────────────────────────────────────────────


# NOTE: TimePolicyLike/HabitLike Protocol 은 settable 속성을 요구하므로(ORM 모델이 만족하는
# 형태) frozen 으로 두지 않는다. 어댑터가 만든 뒤 변형하지 않으므로 사실상 불변으로 쓴다.
@dataclass(slots=True)
class _RuleTimePolicy:
    """`TimePolicyLike` 구조적 만족 — outcome 가용 시간을 busy 계산용 정책으로 환원."""

    policy_type: str
    payload: Mapping[str, Any]
    is_active: bool = True


@dataclass(slots=True)
class _ActionPlacement:
    """`HabitLike` 구조적 만족 — action_item 을 룰 스케줄러의 배치 단위로 환원.

    `reserve_habit_sessions` 가 priority_level 오름차순 + time_preference 윈도우로
    배치하므로, 분해 순서를 priority_level 로, estimated_minutes 를 세션 길이로 매핑한다.
    """

    id: uuid.UUID
    title: str
    category: str
    minutes_per_session: int
    time_preference: str
    priority_level: int
    # HabitLike 는 위 6개 필드만 요구. 배치 후 node_id 복원용 메타.
    node_id: str = field(default="", compare=False)


def _hhmm_to_min(value: str, *, as_end: bool = False) -> int:
    """'HH:MM' → 자정 기준 분. 윈도우 끝의 '00:00'/'24:00' 은 하루 끝(1440)."""
    hh, mm = value.split(":")
    total = int(hh) * 60 + int(mm)
    return 1440 if as_end and total == 0 else total


def _min_to_hhmm(minutes: int) -> str:
    return "24:00" if minutes >= 1440 else f"{minutes // 60:02d}:{minutes % 60:02d}"


def _activity_awake_min(activity: TimeRange) -> list[tuple[int, int]]:
    """활동창을 자정 기준 분 구간으로. 자정 넘김(예: 22:00~06:00)은 두 구간으로 쪼갠다."""
    start = _hhmm_to_min(activity.start)
    end = _hhmm_to_min(activity.end, as_end=True)
    if end > start:
        return [(start, end)]
    out = [(start, 1440)]
    if end > 0:
        out.append((0, end))
    return out


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _complement_min(awake: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[0,1440] 에서 awake 의 여집합(수면 구간)."""
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for s, e in awake:
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < 1440:
        gaps.append((cursor, 1440))
    return gaps


def _preferred_extension_span(outcome: InterviewOutcome) -> tuple[int, int] | None:
    """활동창과 **전혀 안 겹치는** 선호 창의 분 구간 — 겹치면(확장 불필요) None.

    선호 창이 활동창과 겹치면 확장하지 않는다. 활동창 질문 자체가 "이 시간 밖엔 일정을
    안 잡아요" 라는 계약이라 겹침이 있는 한 활동창이 이기고, `_earliest_fit` 의 free∩선호
    교차가 선호 창을 활동창 안으로 자연히 좁힌다(실측: 활동 09~22 + 선호 '오전' 인데
    무조건 확장해 매 블록이 06:00 에 배치되던 문제). 교집합이 0일 때만 — 그대로 두면
    선호가 아예 무의미해지므로 — 예외로 그 시간대를 가용에 포함한다
    (#per-goal-time-availability, '아침 운동': 활동창이 저녁뿐인 사용자).
    """
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    pref = _PEAK_CHIP_WINDOWS.get((heaviest.preferred_time or "").strip())
    if pref is None:
        return None
    span = (pref[0].hour * 60 + pref[0].minute, pref[1].hour * 60 + pref[1].minute)
    awake = _activity_awake_min(outcome.availability.activity_window)
    if any(max(s, span[0]) < min(e, span[1]) for s, e in awake):
        return None
    return span


def preferred_time_extension_warning(outcome: InterviewOutcome) -> str | None:
    """선호 시간이 활동창 밖에 통째로 있어 창 밖에 배치될 때 그 사실을 알린다.

    확장 자체는 의도된 동작(#per-goal-time-availability)이지만, 말해주지 않으면
    "이 시간 밖엔 일정을 안 잡아요" 라고 답한 사용자가 창 밖 블록을 버그로 읽는다.
    """
    if _preferred_extension_span(outcome) is None:
        return None
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    a = outcome.availability
    return (
        f"이 목표는 선호하신 '{heaviest.preferred_time}' 시간대에 잡았어요 — 활동 가능 시간"
        f"({a.activity_window.start}~{a.activity_window.end})과 겹치지 않아서 이 목표만 "
        "예외로 그 시간대를 썼어요. 원치 않으면 활동 시간을 넓히거나 선호 시간을 바꿔서 "
        "다시 만들어 주세요."
    )


# 선호 시간대에 이 비율 미만으로 잡히면 고지한다. 몇 개쯤 빗겨나는 건 정상이므로
# (그날 그 시간이 이미 차 있을 수 있다) 여유를 둔다.
_PREFERRED_OK_RATIO = 0.5


def _window_minutes(w: PlanWindow) -> int:
    return (w.end.hour * 60 + w.end.minute) - (w.start.hour * 60 + w.start.minute)


def _hours_label(minutes: int) -> str:
    """분 → 사람이 읽는 시간 표기. 정수 나눗셈은 쓰지 않는다.

    `119 // 60 = 1` 이라 '심야'(22:00~23:59) 창을 "약 1시간" 이라고 말하던 버그가 있었다 —
    2시간에 가까운 구간을 1시간이라고 하면 사용자가 우리 계산을 못 믿는다.
    """
    if minutes % 60 == 0:
        return f"{minutes // 60}시간"
    return f"{minutes / 60:.1f}시간"


def preferred_window_missed_notice(
    outcome: InterviewOutcome, placed: Sequence[DraftScheduledBlock]
) -> str | None:
    """고른 시간대에 못 넣었으면 그 사실과 **이유**를 알린다. 지켰으면 None.

    실측(FE): 전역 집중 시간대를 '심야'(22:00~23:59 = 119분)로, 세션 길이를 '4시간 이상'
    (240분)으로 답한 사용자의 계획이 **전부 09:00** 에 잡혔다. 240분이 119분 창에 들어갈
    수 없어 `_earliest_fit` 의 선호 창 탐색이 **구조적으로 매번 실패**하고 활동창 폴백으로
    떨어진 것이다. 그런데 **아무 고지도 없어서**, 심야라고 답한 사용자는 왜 아침에 잡혔는지
    알 방법이 없었다.

    `_earliest_fit` 이 '창 안에서 시작' 까지 허용하게 바뀌었어도(2단계), 창이 짧고 그 뒤가
    바로 활동창 끝이면 여전히 못 넣는다 — 그때 이 문구가 이유를 말한다. 선호 시간대를
    아예 안 골랐으면(`peak_windows_for_plan` 이 빈 리스트) 지킬 약속이 없으므로 None.
    """
    windows = peak_windows_for_plan(outcome)
    if not windows or not placed:
        return None
    in_window = sum(
        1 for b in placed if any(w.start <= b.interval.start.time() < w.end for w in windows)
    )
    if in_window >= len(placed) * _PREFERRED_OK_RATIO:
        return None

    session_min = session_min_for(outcome)
    widest = max(_window_minutes(w) for w in windows)
    chosen = " · ".join(f"{w.start:%H:%M}~{w.end:%H:%M}" for w in windows[:2])
    if session_min > widest:
        why = (
            f"고르신 시간대({chosen})는 가장 넓은 구간이 약 {round(widest / 60)}시간인데 "
            f"한 번에 {_hours_label(session_min)}씩 하고 싶다고 하셔서, "
            "그 안에 다 넣을 수 없었어요."
        )
        how = "그 시간대를 지키고 싶으면 한 번에 하는 시간을 줄이거나, 활동 시간을 넓혀 주세요."
    else:
        why = f"고르신 시간대({chosen})에 그만큼의 빈자리가 남지 않았어요."
        how = "그 시간대를 지키고 싶으면 그 시간의 다른 일정을 비우고 다시 만들어 주세요."
    return f"이번 계획은 {len(placed)}개 중 {in_window}개만 그 시간대에 잡혔어요. {why} {how}"


def split_activity_window_notice(outcome: InterviewOutcome) -> str | None:
    """활동창이 자정을 넘어 하루가 두 조각으로 갈리고, 세션이 **어느 조각에도** 안 들어갈 때 (#252).

    실측: 활동 시간대 22:00~02:00 + 세션 3시간 → **블록 0개**, "배치할 가용 시간을 찾지
    못했어요" 가 12줄. 배치는 달력 날짜 단위로 도는데 자정을 넘는 활동창은 같은 날
    `[00:00~02:00]` 과 `[22:00~24:00]` 두 조각이 되어, 22:00→02:00 로 이어지는 연속 4시간이
    만들어지지 않는다. 그래서 2시간을 넘는 세션은 **구조적으로 100% 실패**한다.

    '심야에 집중이 잘 된다' + '한 번에 3~4시간' 은 야행성 사용자에게 자연스러운 조합인데,
    지금은 계획이 통째로 비고 사용자는 같은 문장 12줄만 본다 — 무엇을 바꿔야 하는지도
    안 알려준다. 근본 해결(날짜 경계를 넘는 free 병합)은 스케줄러 전반을 건드리므로 별도이고,
    여기서는 **원인과 다음 행동**을 말한다.

    자정을 안 넘거나 세션이 조각 안에 들어가면 None.
    """
    awake = _activity_awake_min(outcome.availability.activity_window)
    if len(awake) < 2:
        return None
    widest = max(e - s for s, e in awake)
    session_min = session_min_for(outcome)
    if session_min <= widest:
        return None
    a = outcome.availability.activity_window
    parts = " · ".join(f"{_min_to_hhmm(s)}~{_min_to_hhmm(e)}" for s, e in sorted(awake))
    return (
        f"활동 시간대({a.start}~{a.end})가 자정을 넘어서, 하루 안에서는 {parts} 두 조각으로 "
        f"나뉘어요(가장 긴 쪽 약 {round(widest / 60)}시간). 한 번에 {_hours_label(session_min)}씩 "
        "하고 싶다고 하셔서 어느 쪽에도 들어가지 않아, 이번엔 배치하지 못했어요. "
        f"한 번에 하는 시간을 {round(widest / 60)}시간 이하로 줄이거나, 활동 시간을 "
        "자정 앞뒤로 넉넉히 넓혀서 다시 만들어 주세요."
    )


def time_policies_from_outcome(outcome: InterviewOutcome) -> list[TimePolicyLike]:
    """outcome 가용 시간 → 룰 스케줄러 busy 계산용 시간 정책 목록.

    - 활동창을 '깨어있음' 으로 보고, 그 여집합을 수면(sleep, busy)으로 환원한다.
      목표 선호 시간(preferred_time)이 활동창과 **전혀 안 겹칠 때만** 그 시간대를 가용에
      포함한다 — 겹치면 활동창이 이긴다(`_preferred_extension_span` 참고).
    - no_touch 윈도우는 그대로 no_touch 정책으로 전개(요일 제한 포함).
    """
    a = outcome.availability
    awake = _activity_awake_min(a.activity_window)
    extension = _preferred_extension_span(outcome)
    if extension is not None:
        awake.append(extension)
    policies: list[TimePolicyLike] = [
        _RuleTimePolicy(
            policy_type="sleep",
            payload={"start_time": _min_to_hhmm(s), "end_time": _min_to_hhmm(e)},
        )
        for s, e in _complement_min(_merge_intervals(awake))
    ]
    for nt in a.no_touch_windows:
        policies.append(
            _RuleTimePolicy(
                policy_type="no_touch",
                payload={
                    "start_time": nt.window.start,
                    "end_time": nt.window.end,
                    "days_of_week": list(nt.days_of_week),
                },
            )
        )
    return policies


def action_placements(action_items: list[ActionItemDraft]) -> list[HabitLike]:
    """분해된 action_item → 룰 스케줄러 배치 단위(`HabitLike`).

    분해 목록 순서를 priority_level(1=최우선)로, estimated_minutes 를 세션 길이로 매핑한다.
    배치 결과 블록의 `origin_id` 로 다시 node_id 를 복원할 수 있도록 `node_id` 를 싣는다.
    """
    placements: list[HabitLike] = []
    for index, item in enumerate(action_items):
        placements.append(
            _ActionPlacement(
                id=uuid.uuid4(),
                title=item.title,
                category=item.category,
                minutes_per_session=item.estimated_minutes,
                time_preference="anytime",
                priority_level=index + 1,
                node_id=item.node_id,
            )
        )
    return placements


# ─────────────────────────────────────────────────────────────────────────────
# 다일(multi-day) 스케줄러 입력 환원 (`orchestrator/plan_scheduler.py`)
# ─────────────────────────────────────────────────────────────────────────────

# 하루에 배치할 집중 작업 총량 상한(분). 이 상한을 채우면 스케줄러가 다음 날로 넘어간다.
# 활동창(수 시간)을 통째로 한 목표로 채우지 않고 삶의 여백을 남기기 위한 기본값.
DEFAULT_DAILY_FOCUS_CAP_MIN = 180

# time.peak_window chip → 하루 선호 윈도우. '변동' 은 선호 없음(폴백)으로 처리.
_PEAK_CHIP_WINDOWS: dict[str, tuple[time, time]] = {
    "오전": (time(6, 0), time(12, 0)),
    "오후": (time(12, 0), time(18, 0)),
    "저녁": (time(18, 0), time(23, 0)),
    "심야": (time(22, 0), time(23, 59)),
}

# energy.break_pattern chip → 카드 사이 최소 휴식(분).
_BREAK_PATTERN_MIN: dict[str, int] = {
    "짧게 자주": 10,
    "길게 가끔": 20,
    "거의 안 쉼": 5,
}
_DEFAULT_BREAK_MIN = 10

# focus_duration 이 없을 때의 세션 분할 기준(분) — 분해 규칙상 leaf 는 대개 60분 이내.
_DEFAULT_FOCUS_CHUNK_MIN = 60


def plan_actions_from_decomposition(action_items: list[ActionItemDraft]) -> list[PlanAction]:
    """분해된 action_item → 다일 스케줄러 배치 단위(`PlanAction`).

    분해 순서(= 의도된 진행 순서)를 유지한다. `id` 는 배치 블록의 `origin_id` 로 실려
    호출자가 node_id 를 복원하는 키다(중복 세션도 같은 node_id 로 매핑).
    """
    return [
        PlanAction(
            id=uuid.uuid4(),
            node_id=item.node_id,
            title=item.title,
            category=item.category,
            estimated_minutes=item.estimated_minutes,
        )
        for item in action_items
    ]


def peak_windows_from_outcome(outcome: InterviewOutcome) -> list[PlanWindow]:
    """피크 시간대 chip → 선호 윈도우. '변동'만 있거나 비면 선호 없음([])."""
    windows: list[PlanWindow] = []
    for chip in outcome.availability.peak_window:
        bounds = _PEAK_CHIP_WINDOWS.get(chip.strip())
        if bounds is not None:
            windows.append(PlanWindow(start=bounds[0], end=bounds[1]))
    return windows


def peak_windows_for_plan(outcome: InterviewOutcome) -> list[PlanWindow]:
    """이 계획(heaviest 목표)을 배치할 선호 시간창 — **우선순위 순서**로.

    1순위: 목표별 선호 시간(goals.preferred_time) — '아침 운동'은 전역 저녁 peak 이 아니라
           오전에(#per-goal-time).
    2순위: 전역 집중 시간대(time.peak_window) — 목표별 창이 이미 차 있거나 지나갔을 때.

    예전엔 목표별이 있으면 전역을 **버렸다**. 그러면 목표별 창이 막혔을 때 곧바로 활동창
    전체 폴백으로 떨어져, '심야에 집중이 잘 된다' 고 답해 놓고 엉뚱한 시각에 잡히곤 했다
    (실측: 오후 창이 지난 저녁에 계획을 만들자 22:15 에 배치). 전역을 2순위로 남기면 그
    답이 실제로 쓰인다. 두 시간대를 각각 묻는 이유도 이거다 — 목표별이 우선, 전역이 기본값.

    `_earliest_fit` 이 이 리스트를 **순서대로** 시도하므로 순서 자체가 우선순위다.
    """
    heaviest = next((g for g in outcome.core_goals if g.is_heaviest), outcome.core_goals[0])
    globals_ = peak_windows_from_outcome(outcome)
    bounds = _PEAK_CHIP_WINDOWS.get((heaviest.preferred_time or "").strip())
    if bounds is None:
        return globals_
    goal_window = PlanWindow(start=bounds[0], end=bounds[1])
    return [goal_window, *(w for w in globals_ if w != goal_window)]


def focus_chunk_min_from_outcome(outcome: InterviewOutcome) -> int:
    """한 세션 최대 길이(분) — 목표별 goals.session_length 우선, 없으면 전역 focus_duration/기본값."""
    return session_min_for(outcome, default=_DEFAULT_FOCUS_CHUNK_MIN)


def break_min_from_outcome(outcome: InterviewOutcome) -> int:
    """카드 사이 최소 휴식(분) — energy.break_pattern, 없으면 기본값."""
    pattern = outcome.preferences.break_pattern
    if pattern is None:
        return _DEFAULT_BREAK_MIN
    return _BREAK_PATTERN_MIN.get(pattern.strip(), _DEFAULT_BREAK_MIN)


# ─────────────────────────────────────────────────────────────────────────────
# SAVING — 사용자 [수락] 후 단일 가드 트랜잭션 영속화 (ADR-0005 §2.5.1 / AGENTS §1.4)
#
# HITL [수락] 이후에만 호출되는 단 하나의 영속화 경로. PR #30 의
# `policy_guarded_transaction` 을 재사용해 절대 시간 정책 위반 시 즉시 롤백한다.
# #62: goal/goal_node 트리(temp_uuid → 실 UUID) + action_item 링크 + scheduled_blocks 까지
# 단일 트랜잭션 영속화 + 3회 재시도. ⚠️ dependency_links 는 GoalDecomposition 에 소스 데이터가
# 없어 후속 분리(이슈 #62 제외 범위).
# ─────────────────────────────────────────────────────────────────────────────

MAX_SAVE_RETRIES = 3  # ADR-0005 §2.5.1 — DB Agent 최대 3회 재시도 후 PLAN_SAVE_FAILED.


@dataclass(frozen=True, slots=True)
class FirstPlanSaveResult:
    """SAVING 영속화 결과 카운트."""

    goals: int
    goal_nodes: int
    action_items: int
    scheduled_blocks: int


def _replaceable_action(
    action: ActionItem,
    goal_id: uuid.UUID,
    *,
    mandala_node_ids: frozenset[uuid.UUID] = frozenset(),
) -> bool:
    """이전 AI 계획 산출물 중 '사용자가 손대지 않은' 교체 대상인지.

    source='goal'(계획 분해 산출) + status='planned'(시작/체크인 이력 없음) + 미보관 +
    **같은 goal** 만 교체한다. 시작·완료·실패 카드와 inbox/manual/recovery
    카드는 이력·사용자 의도 보존을 위해 남긴다 (AGENTS §2 원본 status 불변 원칙과 일관).

    `goal_id` 조건이 왜 필요한가: 교체는 **재생성**(같은 목표를 다시 뽑음) 중복을 막으려는
    장치인데, 목표가 다르면 교체가 아니라 **공존**이 맞다(#187 한 번에 한 목표에 집중 +
    나머지는 다음 계획 안내와도 일관). 실측: 같은 날 'MVP 만들기' 계획을 승인한 뒤 '토익'
    계획을 승인하자 MVP 4주치(카드 12개)가 전부 보관됐다.

    target_date 는 키가 **아니다** (#222): 카드 날짜가 자기 블록 날짜를 따라가면서(4주 계획
    = 4주치 날짜) 날짜 키로는 이전 계획의 뒷날짜 카드가 교체에서 빠져 재승인마다 누적된다.
    교체 단위는 '그 목표의 이전 AI 계획 전체'다 — 노드층(`_archive_goal_nodes`)이 이미
    날짜 없이 goal 단위로 보관하는 것과도 정합.

    `mandala_node_ids` — 이 카드의 `goal_node_id` 가 만다라 셀이면 절대 교체하지 않는다
    (W2, `1ee508b967ba`). 만다라 셀에서 승격된 카드(`source='goal'` 로 저장될 수 있다)가
    같은 goal 아래 계획 카드와 섞여 있어도, 계획 재생성이 그 만다라 유래 카드까지 쓸어가면
    안 된다. 기본값은 빈 집합 — 호출부가 안 넘기면 기존 동작과 완전히 같다.
    """
    return (
        action.source == "goal"
        and action.status == "planned"
        and action.archived_at is None
        and action.goal_id == goal_id
        and action.goal_node_id not in mandala_node_ids
    )


async def _mandala_node_ids_among(
    session: AsyncSession, node_ids: set[uuid.UUID]
) -> frozenset[uuid.UUID]:
    """주어진 goal_node id 중 `tree_kind='mandala'` 인 것만. 빈 입력이면 쿼리 없이 빈 집합

    (`test_plan_supersede_sql.py` 의 "후보 없으면 SELECT 1회" 계약을 깨지 않기 위함 — 대다수
    호출에서 후보 카드에 `goal_node_id` 가 아예 없다).
    """
    if not node_ids:
        return frozenset()
    stmt = select(GoalNode).where(GoalNode.id.in_(node_ids))
    rows = (await session.execute(stmt)).scalars().all()
    return frozenset(n.id for n in rows if n.tree_kind == "mandala")


def protected_card_ids(live_blocks: Sequence[ScheduledBlock]) -> set[uuid.UUID]:
    """user_edit 블록(S15 직접 이동)을 가진 카드 id — 교체 대상에서 제외(보존).

    supersede_previous_plan(취소)·superseded_card_ids(재생성 busy 제외)·주간 forward
    재계획 승인(`api/routes/planning.approve_replan`, #117)이 공유하는 블록층 보호 규칙 —
    한 곳에서만 정의해 여러 경로가 어긋나지 않게 한다.

    **카드(action) 단위**로 보존한다: 카드의 블록 중 user_edit 이 하나라도 있으면 그 카드는
    통째로 보존 — 사용자가 시간을 옮긴 계획을 승인이 지우면 안 된다.
    """
    return {b.action_item_id for b in live_blocks if b.source == "user_edit"}


async def superseded_card_ids(
    session: AsyncSession, *, user_id: uuid.UUID, goal_id: uuid.UUID | None
) -> set[uuid.UUID]:
    """approve 시 supersede_previous_plan 이 '교체'할 카드 id 집합 (read-only).

    supersede 와 **완전히 같은 규칙**(카드층 `_replaceable_action` + 블록층
    `protected_card_ids`)을 쓰되 아무것도 변형하지 않고 FOR UPDATE 도 걸지 않는다.
    generate(재생성)가 '곧 자기 승인으로 비워질' **같은 목표** 이전 계획의 블록을
    busy 에서 제외하는 데 쓴다(#118) — 재생성 계획이 그 슬롯을 피해 나쁘게 배치되지 않도록.
    첫 계획(교체 대상 없음)이면 빈 집합이라 busy 제외가 no-op.

    `goal_id=None`(목표가 아직 영속되지 않음)이면 빈 집합 — 아무것도 제외하지 않는다.
    "무엇이 지워질지 모르면 전부 피한다"가 안전한 기본값이다: 잘못 제외하면 남의 계획 위에
    겹쳐 배치되지만, 잘못 피하면 배치가 조금 뒤로 밀릴 뿐이다.
    """
    if goal_id is None:
        return set()
    stmt = select(ActionItem).where(
        ActionItem.user_id == user_id,
        ActionItem.goal_id == goal_id,
        ActionItem.source == "goal",
        ActionItem.status == "planned",
        ActionItem.archived_at.is_(None),
    )
    rows = (await session.execute(stmt)).scalars().all()
    node_ids = {a.goal_node_id for a in rows if a.goal_node_id is not None}
    mandala_node_ids = await _mandala_node_ids_among(session, node_ids)
    candidates = [
        a for a in rows if _replaceable_action(a, goal_id, mandala_node_ids=mandala_node_ids)
    ]
    if not candidates:
        return set()
    candidate_ids = {a.id for a in candidates}
    block_stmt = select(ScheduledBlock).where(
        ScheduledBlock.user_id == user_id,
        ScheduledBlock.action_item_id.in_(candidate_ids),
        ScheduledBlock.block_status != "cancelled",
    )
    live_blocks = [
        b
        for b in (await session.execute(block_stmt)).scalars().all()
        if b.action_item_id in candidate_ids and b.block_status != "cancelled"
    ]
    return candidate_ids - protected_card_ids(live_blocks)


async def supersede_previous_plan(
    session: AsyncSession, *, user_id: uuid.UUID, goal_id: uuid.UUID
) -> int:
    """**같은 목표**의 이전 First Plan 산출물을 정리(soft) — 승인 = "이 계획으로 교체".

    generate 는 기존 블록을 busy 로 보지 않고(후속: 스케줄러 DB busy 통합 이슈) approve 는
    무조건 INSERT 만 해서, 재생성→재승인을 반복하면 카드/블록이 계속 누적됐다
    (같은 제목 ×5, 같은 시각 4중첩). 승인 시점에 같은 goal 의 이전 AI 계획
    산출물 중 사용자가 손대지 않은 것만 정리해 "마지막 승인 = 그 목표의 계획"이 되게 한다.
    날짜는 키가 아니다(#222, `_replaceable_action` 참고) — 카드 날짜가 블록 날짜를 따라가므로
    날짜 키로는 이전 계획의 뒷날짜 카드가 빠져 재승인마다 누적된다.

    **다른 목표의 계획은 건드리지 않는다** — 교체가 아니라 공존이 맞다. 겹치는 시간대는
    지우는 게 아니라 `_existing_busy_by_day` 가 busy 로 넘겨 스케줄러가 뒤로 밀어 피한다.
    자세한 근거는 `_replaceable_action` 참고.

    "손대지 않은" 판정은 두 층이다:
    - 카드 층: `_replaceable_action` (source=goal · status=planned · 미보관). ⚠️ **날짜는
      조건이 아니다** — #223 이후 카드마다 블록 날짜가 4주에 흩어지므로, 날짜로 좁히면
      뒷날짜 카드가 교체에서 빠져 재승인마다 누적된다(교체 단위는 goal 전체).
    - 블록 층: 카드의 블록 중 `source='user_edit'`(S15 직접 이동)가 하나라도 있으면
      그 카드는 **통째로 보존** — 사용자가 시간을 옮긴 계획을 승인이 지우면 안 된다.

    hard delete 금지(AGENTS §2) — action_item 은 archived_at(soft delete),
    scheduled_block 은 block_status='cancelled' 로 마킹한다. 반환값은 교체된 카드 수.

    카드 SELECT 는 FOR UPDATE — 같은 카드를 동시에 [시작]하는 요청(today/start)이
    status 를 in_progress 로 바꾸는 것과 교차해 '보관됐는데 실행 중'인 유령 카드가
    생기지 않게 행 잠금으로 직렬화한다. SQL WHERE 로 좁히고 파이썬 술어로 한 번 더
    거른다 — WHERE 를 평가하지 않는 구조적 fake session(테스트)에서도 규칙 유지.
    """
    stmt = (
        select(ActionItem)
        .where(
            ActionItem.user_id == user_id,
            ActionItem.goal_id == goal_id,
            ActionItem.source == "goal",
            ActionItem.status == "planned",
            ActionItem.archived_at.is_(None),
        )
        .with_for_update()
    )
    rows = (await session.execute(stmt)).scalars().all()
    node_ids = {a.goal_node_id for a in rows if a.goal_node_id is not None}
    mandala_node_ids = await _mandala_node_ids_among(session, node_ids)
    candidates = [
        a for a in rows if _replaceable_action(a, goal_id, mandala_node_ids=mandala_node_ids)
    ]
    if not candidates:
        return 0

    candidate_ids = {a.id for a in candidates}
    block_stmt = select(ScheduledBlock).where(
        ScheduledBlock.user_id == user_id,
        ScheduledBlock.action_item_id.in_(candidate_ids),
        ScheduledBlock.block_status != "cancelled",
    )
    fetched = (await session.execute(block_stmt)).scalars().all()
    live_blocks = [
        b for b in fetched if b.action_item_id in candidate_ids and b.block_status != "cancelled"
    ]
    # 사용자가 직접 옮긴(user_edit) 블록을 가진 카드는 교체 대상에서 제외 (superseded_card_ids 와 공유).
    protected_ids = protected_card_ids(live_blocks)
    stale = [a for a in candidates if a.id not in protected_ids]
    if not stale:
        return 0

    archived_at = now_kst()
    stale_ids = {a.id for a in stale}
    for action in stale:
        action.archived_at = archived_at
    for block in live_blocks:
        if block.action_item_id in stale_ids:
            block.block_status = "cancelled"
    return len(stale)


async def _persist_milestones_if_new(
    session: AsyncSession, *, goal_id: uuid.UUID, milestones: Sequence[MilestoneDraft]
) -> list[GoalNode]:
    """확정된 마일스톤(#milestones Stage B)을 `node_type='milestone'` 로 영속(ADR-0007 PR-2).

    **한 번만 만든다.** 이미 이 goal 에 활성 마일스톤이 있으면(재승인·재계획) 손대지 않고
    빈 리스트를 반환한다 — 매 승인마다 `_archive_goal_nodes` 로 통째로 갈아치우는
    core/subgoal/leaf 층과 달리, 마일스톤은 "마감까지의 뼈대"라 주기를 넘어 살아남아야
    한다(ADR-0007 §1). 두 번째 승인이 같은 목록을 다시 넣으려 하면(사용자가 재편집 없이
    그냥 다시 승인) 조용히 무시 — 재편집(HITL 재조정)은 ADR-0007 PR-6, 이 함수의 범위
    밖이다.

    LLM 분해가 만든 branch 노드에서 역추적하지 않고 **사용자가 확인·편집한 원본
    `MilestoneDraft` 를 그대로** 쓴다 — 분해는 세션 수 상한(`_MAX_LLM_SESSIONS`)에 잘리거나
    마일스톤을 통째로 스킵할 수 있어(`missing_milestone_titles` 가 잡는 바로 그 함정),
    LLM 출력에서 역산하면 사용자가 확정한 마일스톤 자체가 조용히 사라질 수 있다.

    `parent_node_id=None` · `depth=1` — 매 주기 교체되는 core/subgoal/leaf 트리와
    부모-자식으로 얽지 않는다(그 트리가 archive 될 때 같이 끌려가면 안 된다). subgoal 도
    depth=1 이라 같은 depth 를 공유하지만 `node_type` 으로 구분된다(만다라가
    `tree_kind` 로, 이건 `node_type` 으로 나누는 것과 같은 원리) — `GET /goals/{id}/nodes`
    가 아직 이 둘을 섞어 반환한다는 뜻이라 FE 가 `nodeType` 으로 걸러야 한다.
    leaf 가 어느 마일스톤에 속하는지 잇는 것(진척 롤업·주기 전환)은 이 함수의 범위 밖 —
    ADR-0007 PR-3 이후.
    """
    if not milestones:
        return []
    existing_stmt = select(GoalNode).where(
        GoalNode.goal_id == goal_id,
        GoalNode.tree_kind == "plan",
        GoalNode.node_type == "milestone",
        GoalNode.archived_at.is_(None),
    )
    existing_rows = (await session.execute(existing_stmt)).scalars().all()
    already_persisted = any(
        n.goal_id == goal_id
        and n.tree_kind == "plan"
        and n.node_type == "milestone"
        and n.archived_at is None
        for n in existing_rows
    )
    if already_persisted:
        return []
    rows: list[GoalNode] = []
    for i, m in enumerate(milestones):
        n = GoalNode()
        n.goal_id = goal_id
        n.parent_node_id = None
        n.title = m.title
        n.node_type = "milestone"
        n.depth = 1
        n.order_index = i
        n.is_leaf = False
        n.tree_kind = "plan"
        n.why_text = m.summary or None
        session.add(n)
        rows.append(n)
    await session.flush()
    return rows


async def _archive_goal_nodes(session: AsyncSession, *, goal_id: uuid.UUID) -> int:
    """goal 의 기존 활성 **계획 분해** 트리를 보관 — 새 승인 트리가 '현재 트리'가 되게.

    매 승인이 heaviest goal 아래에 goal_nodes 트리를 새로 INSERT 하므로, 이전 트리를
    archived_at 으로 보관하지 않으면 승인 반복 시 동일 트리가 무한 누적된다(카드/블록과
    같은 뿌리의 세 번째 테이블). 보관된 노드를 가리키는 기존 action_item 의
    goal_node_id 는 계보(lineage)로 유지된다. 반환값은 보관한 노드 수.

    `tree_kind == "plan"` 으로 좁힌다(W1, `1ee508b967ba`) — 안 그러면 §3.4-b 의 제목
    충돌(궁극목표와 계획 core_goals 제목이 겹침)이 성립하는 순간 만다라 73칸이 계획
    승인 한 번에 통째로 archived 된다. W3(`_mandala_owned_goal_ids`)가 그 충돌 자체를
    막지만, 이 필터는 그 방어가 뚫리거나 아직 적용되기 전 상태에서도 남는 두 번째 방어선.

    `node_type != "milestone"` 도 뺀다(ADR-0007 PR-2) — 마일스톤은 주기를 넘어 살아남는
    층이라, 매 승인이 갈아치우는 core/subgoal/leaf 와 같이 archive 되면 안 된다
    (`_persist_milestones_if_new` 의 "한 번만 만든다"가 이 제외를 전제로 성립한다).
    """
    stmt = select(GoalNode).where(
        GoalNode.goal_id == goal_id,
        GoalNode.archived_at.is_(None),
        GoalNode.tree_kind == "plan",
        GoalNode.node_type != "milestone",
    )
    rows = (await session.execute(stmt)).scalars().all()
    stale = [
        n
        for n in rows
        if n.goal_id == goal_id
        and n.archived_at is None
        and n.tree_kind == "plan"
        and n.node_type != "milestone"
    ]
    archived_at = now_kst()
    for node in stale:
        node.archived_at = archived_at
    if stale:
        await session.flush()
    return len(stale)


def _normalize_category(raw: str) -> str:
    """ActionItem.category enum 으로 정규화 — 미지원 카테고리는 'other'."""
    return raw if raw in ACTION_CATEGORY_VALUES else "other"


def _normalize_goal_category(raw: str) -> str:
    return raw if raw in GOAL_CATEGORY_VALUES else "other"


def _derive_goal_category(action_categories: Sequence[str]) -> str | None:
    """액션 카테고리 다수결로 목표 카테고리 파생 — 전부 'other'거나 비면 None.

    인터뷰는 목표 카테고리를 분류하지 않아 'other' 로 저장되므로
    (interview_adapter._build_goals), 분해된 액션들의 실카테고리에서 역산한다.
    ACTION_CATEGORY_VALUES ⊆ GOAL_CATEGORY_VALUES (동일 enum) 이라 그대로 대입 가능.
    """
    counts = Counter(c for c in action_categories if c != "other")
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _normalize_goal_tier(raw: str) -> str:
    return raw if raw in GOAL_TIER_VALUES else "maintain"


def _node_depths(goal_nodes: Sequence[GoalNodeDraft]) -> dict[str, int]:
    """temp node_id → depth (parent_id 체인 hop 수). root = 0."""
    parent_of = {n.node_id: n.parent_id for n in goal_nodes}
    depths: dict[str, int] = {}
    for node in goal_nodes:
        depth = 0
        cursor = node.parent_id
        seen: set[str] = set()
        while cursor is not None and cursor in parent_of and cursor not in seen:
            seen.add(cursor)
            depth += 1
            cursor = parent_of[cursor]
        depths[node.node_id] = depth
    return depths


async def _mandala_owned_goal_ids(session: AsyncSession) -> frozenset[uuid.UUID]:
    """만다라 트리(`tree_kind='mandala'`)를 소유한 goal 의 id 집합 (W3, `1ee508b967ba`).

    user_id 로 좁히지 않는다 — 호출자가 이미 user 범위 목표 목록에서 멤버십만 확인하므로
    (`_active_goals`), goal_id 는 어차피 한 user 소속이라 cross-user 유출이 없다.

    이 집합에 들어간 goal 은 `_active_goals`(→ `materialize_goals`/`heaviest_goal_id`
    제목 매칭)에서 제외된다 — 궁극목표(§3.2, `status='active'`) 제목이 계획 인터뷰
    `core_goals` 제목과 우연히 겹치면 그 goal 이 heaviest 로 오인돼, 계획 승인 한 번에
    만다라 73칸이 `_archive_goal_nodes`/`supersede_previous_plan` 에 통째로 삼켜진다
    (W1/W2 가 막는 사고의 **성립 조건** 자체를 여기서 끊는다).
    """
    stmt = select(GoalNode).where(GoalNode.archived_at.is_(None))
    rows = (await session.execute(stmt)).scalars().all()
    return frozenset(n.goal_id for n in rows if n.tree_kind == "mandala")


async def _active_goals(session: AsyncSession, user_id: uuid.UUID) -> list[Goal]:
    """user 의 활성 목표 — **만다라 트리를 소유한 목표는 제외**(W3). 제목 매칭/잠정 정리가

    이 목록을 쓰는 모든 곳(`heaviest_goal_id`/`materialize_goals`/`supersede_proposed_goals`)
    이 궁극목표를 절대 후보로 보지 않게 하는 단일 지점.
    """
    stmt = select(Goal).where(Goal.user_id == user_id, Goal.archived_at.is_(None))
    rows = (await session.execute(stmt)).scalars().all()
    mandala_owner_ids = await _mandala_owned_goal_ids(session)
    return [g for g in rows if g.id not in mandala_owner_ids]


async def heaviest_goal_id(
    session: AsyncSession, *, user_id: uuid.UUID, outcome: InterviewOutcome
) -> uuid.UUID | None:
    """이번 계획이 소속될 heaviest 목표의 **영속 id** — 아직 저장 전이면 None.

    generate 단계에서 "무엇이 곧 교체될 계획인가"를 알아내는 데 쓴다(`superseded_card_ids`).
    승인 경로의 `materialize_goals` 와 **같은 선택 규칙**(placeholder 제외 → is_heaviest →
    없으면 첫 실제 목표)을 쓰고, 매칭은 같은 키(제목)로 한다 — 두 경로가 어긋나면 generate
    가 피하지 않은 슬롯을 approve 가 지우거나 그 반대가 된다.

    인터뷰 완료가 목표를 proposed 로 먼저 저장하므로(#96) 보통은 찾을 수 있다. 못 찾으면
    None → 아무것도 busy 에서 빼지 않는다(전부 회피).
    """
    heaviest_title: str | None = None
    for gc in outcome.core_goals:
        if is_placeholder_goal(gc):
            continue
        if heaviest_title is None:
            heaviest_title = gc.title  # 폴백: 첫 실제 목표
        if gc.is_heaviest:
            heaviest_title = gc.title
            break
    if heaviest_title is None:
        return None
    for g in await _active_goals(session, user_id):
        if g.title == heaviest_title:
            return g.id
    return None


async def materialize_goals(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    core_goals: Sequence[GoalCandidate],
    status: str = "proposed",
) -> tuple[list[Goal], Goal | None]:
    """core_goals → 영속 Goal 목록 + heaviest. 이미 있는 제목은 재사용(중복 생성 방지).

    딥 인터뷰 완료(#96)와 계획 승인(#62)이 공유한다: 인터뷰가 먼저 목표를 저장해
    분류 화면(GET /goals)에 노출·재분류할 수 있게 하고, 이후 계획 승인은 같은 목표를
    **재사용**(신규 생성 X)해 중복을 막는다. 미입력 placeholder(#88)는 제외.

    `status` 로 두 경로를 구분한다:
    - 인터뷰 완료 → `"proposed"`(기본) — 계획이 아직 승인되지 않은 **잠정** 목표.
    - 계획 승인 → `"active"` — 이때 기존 proposed 행도 함께 승격한다.

    예전엔 인터뷰만 마쳐도 곧바로 `active` 로 저장돼, 계획을 승인하지 않고 나간 목표가
    진짜 목표와 구분 없이 쌓였다(실측: 67개 중 43개가 계획 없는 active).
    """
    existing = {g.title: g for g in await _active_goals(session, user_id)}
    goal_rows: list[Goal] = []
    heaviest: Goal | None = None
    for gc in core_goals:
        if is_placeholder_goal(gc):
            continue
        g = existing.get(gc.title)
        if g is None:
            g = Goal()
            g.user_id = user_id
            g.title = gc.title
            g.category = _normalize_goal_category(gc.category)
            g.goal_tier = _normalize_goal_tier(gc.tentative_tier)
            g.deadline = date.fromisoformat(gc.deadline) if gc.deadline else None
            g.status = status
            g.why_now = gc.why_now
            session.add(g)
            existing[gc.title] = g
        elif status == "active" and g.status == "proposed":
            # 승인 = 이 목표를 실제로 하겠다는 결정 → 잠정에서 승격.
            g.status = "active"
        goal_rows.append(g)
        if gc.is_heaviest and heaviest is None:
            heaviest = g
    if heaviest is None and goal_rows:
        heaviest = goal_rows[0]
    await session.flush()
    return goal_rows, heaviest


async def supersede_proposed_goals(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    keep: Sequence[Goal],
) -> int:
    """이번 인터뷰가 살린 것 말고 남은 **잠정(proposed)** 목표를 보관 처리. 반환: 정리한 개수.

    인터뷰 세션은 이미 restart-wins 로 이전 세션을 `abandoned` 로 닫는다. 목표에도 같은 규칙을
    적용해, 지난 인터뷰에서 나왔지만 계획으로 이어지지 않은 잠정 목표가 계속 쌓이지 않게 한다.

    `active`/`completed` 는 건드리지 않는다 — 이미 사용자가 계획을 승인했거나 직접 만든
    진짜 목표라서. 보관(soft)이라 데이터는 남고 화면에서만 사라진다(hard delete 금지, AGENTS §2).
    """
    keep_ids = {g.id for g in keep if g.id is not None}
    stale = [
        g
        for g in await _active_goals(session, user_id)
        if g.status == "proposed" and g.id not in keep_ids
    ]
    for g in stale:
        g.archived_at = datetime.now(UTC)
        g.status = "archived"
    if stale:
        await session.flush()
    return len(stale)


async def _apply_once(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    target_date: date,
    outcome: InterviewOutcome,
    goal_nodes: Sequence[GoalNodeDraft],
    action_items: Sequence[ActionItemDraft],
    blocks: Sequence[ScheduledBlockPreview],
    time_policies: Sequence[TimePolicyLike],
    milestones: Sequence[MilestoneDraft] = (),
    on_success: Callable[[], Awaitable[None]] | None = None,
) -> FirstPlanSaveResult:
    """단일 가드 트랜잭션 1회 시도 — goals → goal_nodes → action_items → scheduled_blocks.

    `on_success` 는 영속화 직후 **같은 가드 트랜잭션 안**에서 호출된다 — 호출자가
    Draft 상태 전이 등 부수 기록을 계획 영속화와 원자적으로(단일 commit) 묶을 수 있게.
    실패 시 롤백에 함께 쓸려 나간다.
    """
    guard_plan = DraftPlan(
        target_date=target_date,
        blocks=tuple(
            DraftScheduledBlock(
                interval=TimeInterval(b.start, b.end),
                origin=b.origin,
                origin_id=None,
                title=b.title,
                category=b.category,
            )
            for b in blocks
        ),
        free_blocks=(),
        busy_blocks=(),
        warnings=(),
        generated_at=now_kst(),
    )

    async with policy_guarded_transaction(session, guard_plan, time_policies):
        # 1) goals — 인터뷰 완료 시 이미 저장된 목표를 재사용(중복 방지, #96), placeholder 제외(#88).
        #    heaviest 가 분해 트리의 소속 goal. 승인은 '이 목표를 실제로 하겠다' 는 결정이므로
        #    잠정(proposed) 목표를 여기서 active 로 승격한다.
        goal_rows, heaviest = await materialize_goals(
            session, user_id=user_id, core_goals=outcome.core_goals, status="active"
        )

        # 실제 목표가 없으면(=goals.list 미입력) 트리/액션도 만들지 않는다: placeholder 로부터
        # 분해된 노드는 소속시킬 goal 이 없고(GoalNode.goal_id 는 NOT NULL) 의미도 없다.
        node_by_temp: dict[str, GoalNode] = {}
        action_by_node: dict[str, ActionItem] = {}
        block_count = 0
        if heaviest is None:
            # 빈 계획도 승인 자체는 성립 — 부수 기록(Draft 승인 등)은 같은 트랜잭션으로.
            if on_success is not None:
                await on_success()
            return FirstPlanSaveResult(goals=0, goal_nodes=0, action_items=0, scheduled_blocks=0)

        # 1.5) 교체(supersede) — **같은 목표**의 이전 AI 계획 산출물(미시작 카드+블록)을
        #      soft 정리하고 이 계획으로 대체. 재생성→재승인 반복 시 카드/블록이 겹겹이
        #      누적되던 문제를 막는다. 다른 목표의 계획은 그대로 둔다(공존) — 겹침은 배치
        #      단계의 busy 회피가 처리한다. 빈 계획(heaviest 없음)은 위에서 이미 반환 →
        #      아무것도 지우지 않는다.
        await supersede_previous_plan(session, user_id=user_id, goal_id=heaviest.id)
        # 1.6) heaviest goal 의 기존 분해 트리 보관 — 노드도 카드/블록처럼 승인마다
        #      새로 INSERT 되므로, 보관하지 않으면 같은 트리가 무한 누적된다. 마일스톤은
        #      이 보관 대상에서 빠진다(ADR-0007 PR-2) — 아래에서 별도로, 없을 때만 만든다.
        await _archive_goal_nodes(session, goal_id=heaviest.id)
        milestone_nodes = await _persist_milestones_if_new(
            session, goal_id=heaviest.id, milestones=milestones
        )

        # 2) goal_nodes — heaviest goal 트리. temp node_id → GoalNode (parent 는 relationship).
        depths = _node_depths(goal_nodes)
        for nd in goal_nodes:
            n = GoalNode()
            n.goal_id = heaviest.id
            n.title = nd.title
            n.node_type = _NODE_TYPE_MAP.get(nd.node_type, "subgoal")
            n.depth = depths.get(nd.node_id, 0)
            n.order_index = nd.order_index
            n.is_leaf = nd.is_leaf
            session.add(n)
            node_by_temp[nd.node_id] = n
        for nd in goal_nodes:
            if nd.parent_id is not None and nd.parent_id in node_by_temp:
                node_by_temp[nd.node_id].parent = node_by_temp[nd.parent_id]
        await session.flush()  # goal_node.id 확보 (action_item FK)

        # 3) action_items — goal_id + goal_node_id 링크 (#62)
        # 카드의 target_date 는 **자기 블록(가장 이른 것)의 KST 날짜** (#222). 예전엔 전부
        # 계획 시작일이라, 4주 계획을 승인하면 오늘 아젠다에 28장이 통째로 떴고(아젠다는
        # target_date 로 조회), 미래 블록 카드를 오늘 시작하면 리뷰 지표가 계획 시각 기준으로
        # 왜곡됐다(실측: avgDelayMinutes -3778, peakWindow 가 실제 아닌 계획 슬롯). 블록이
        # 없는 카드만 계획 시작일 폴백.
        block_date_by_node: dict[str, date] = {}
        for b in blocks:
            if b.origin_id is None:
                continue
            block_day = to_kst(b.start).date()
            prev = block_date_by_node.get(b.origin_id)
            if prev is None or block_day < prev:
                block_date_by_node[b.origin_id] = block_day
        for item in action_items:
            row = ActionItem()
            row.user_id = user_id
            row.title = item.title
            row.target_date = block_date_by_node.get(item.node_id, target_date)
            row.estimated_minutes = item.estimated_minutes
            row.category = _normalize_category(item.category)
            row.status = "planned"  # 신규 카드 — 원본 status 변경 아님(AGENTS §2)
            row.source = "goal"
            row.first_step = item.first_step
            if heaviest is not None:
                row.goal_id = heaviest.id
            node = node_by_temp.get(item.node_id)
            if node is not None:
                row.goal_node_id = node.id
            session.add(row)
            action_by_node[item.node_id] = row
        await session.flush()  # action_item.id 확보 (block FK)

        # 3.5) heaviest goal 카테고리 보정 — 'other'(인터뷰 미분류) 일 때만 액션 다수결로
        #      파생. 사용자가 이미 실카테고리를 설정했다면 덮어쓰지 않는다.
        if heaviest.category == "other":
            derived = _derive_goal_category([a.category for a in action_by_node.values()])
            if derived is not None:
                heaviest.category = derived

        # 4) scheduled_blocks — action_item 에 연결
        block_count = 0
        for b in blocks:
            action = action_by_node.get(b.origin_id or "")
            if action is None:
                continue  # node 에 매달리지 않은 block 은 영속 대상 아님(habit 등은 별도 경로)
            sb = ScheduledBlock()
            sb.user_id = user_id
            sb.action_item_id = action.id
            sb.start_at = b.start
            sb.end_at = b.end
            sb.source = "ai_plan"
            sb.block_status = "scheduled"
            session.add(sb)
            block_count += 1
        # dependency_links: GoalDecomposition 에 의존성 소스 데이터 없음 → 후속(#62 제외 범위).

        # 5) 호출자 부수 기록(Draft 승인 마킹·온보딩 전이 등) — 같은 트랜잭션, 같은 commit.
        #    가드 트랜잭션의 commit 이 advisory lock(트랜잭션 스코프)을 해제하므로, 부수
        #    기록을 트랜잭션 밖(별도 commit)으로 빼면 그 사이가 무락 구간이 된다.
        if on_success is not None:
            await on_success()

    return FirstPlanSaveResult(
        goals=len(goal_rows),
        goal_nodes=len(goal_nodes) + len(milestone_nodes),
        action_items=len(action_by_node),
        scheduled_blocks=block_count,
    )


async def db_apply_first_plan(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    target_date: date,
    outcome: InterviewOutcome,
    goal_nodes: Sequence[GoalNodeDraft],
    action_items: Sequence[ActionItemDraft],
    blocks: Sequence[ScheduledBlockPreview],
    time_policies: Sequence[TimePolicyLike],
    milestones: Sequence[MilestoneDraft] = (),
    max_retries: int = MAX_SAVE_RETRIES,
    on_success: Callable[[], Awaitable[None]] | None = None,
) -> FirstPlanSaveResult:
    """승인된 Draft 를 goal 트리까지 단일 트랜잭션 영속화 + 최대 `max_retries` 회 재시도.

    정책 위반(`PolicyViolationError`)은 결정적이라 재시도하지 않고 즉시 전파한다. 그 외
    영속화 예외(IntegrityError 등)는 가드 트랜잭션이 롤백 후 재시도하고, 마지막 실패는
    원 예외를 전파한다(라우터가 `PLAN_SAVE_FAILED` 로 매핑).

    ⚠️ 가드 트랜잭션의 commit/rollback 은 트랜잭션 스코프 advisory lock 을 해제한다.
    호출자가 lock 으로 임계 구역을 보호한다면 **시도(attempt)당 lock 을 다시 잡아야**
    하므로, 재시도 루프는 라우터가 소유하고 여기엔 `max_retries=1` 을 넘기는 것을
    권장한다 (ADR-0005 §2.5.1 의 3회 재시도는 라우터 루프가 담당). `on_success` 는
    영속화와 같은 트랜잭션(같은 commit)으로 실행할 부수 기록 훅.

    Raises:
        PolicyViolationError: block 이 절대 시간 정책(수면/노터치 등)을 침범한 경우.
        Exception: max_retries 회 모두 실패한 경우 마지막 예외.
    """
    last_exc: Exception | None = None
    for _attempt in range(max_retries):
        try:
            return await _apply_once(
                session,
                user_id=user_id,
                target_date=target_date,
                outcome=outcome,
                goal_nodes=goal_nodes,
                action_items=action_items,
                blocks=blocks,
                time_policies=time_policies,
                milestones=milestones,
                on_success=on_success,
            )
        except PolicyViolationError:
            raise  # 결정적 — 재시도 무의미
        except Exception as exc:  # noqa: BLE001 — 롤백은 가드 트랜잭션이 보장, 재시도 후 전파
            last_exc = exc
    assert last_exc is not None
    raise last_exc
