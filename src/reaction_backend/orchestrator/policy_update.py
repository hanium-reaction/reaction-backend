"""Policy Update — 주간 KPI 로 다음 정책 후보를 만든다 (#168, api-contract §14).

## 왜 이 모듈이 생겼나

`policy_snapshots` 테이블·모델·`GET /current` 라우트는 오래전부터 있었는데 **행을 만드는
코드가 레포 전체에 0곳**이었다. 그래서 그 endpoint 는 "없으면 404" 가 아니라 **항상 404**
였고, FE 주간 리뷰의 '다음 주 정책 자동 보정' 은 카운트-only 폴백을 영구히 유지하고
있었다(#168). 라우트를 고칠 문제가 아니라 생산 경로가 통째로 없던 문제다.

## 룰 기반인 이유 (LLM 아님)

`policy.py` 의 옛 docstring 은 구현 위치를 `agents/policy_update_agent.py` 로 적어 뒀지만,
정책 보정은 **KPI 숫자 → 파라미터 숫자** 의 결정적 변환이라 LLM 이 필요 없다. 오히려
룰이어야 하는 이유가 있다:

- 사용자가 승인 화면에서 "왜 이 값이 됐나" 를 물으면 근거를 숫자로 보여줄 수 있어야 한다
  (`PolicyChange.why`). LLM 은 매번 다른 문장을 만들고 재현이 안 된다.
- 정책은 이후 **모든 계획 생성의 입력**이다. 비결정적 산출물을 그 자리에 두면 계획이
  왜 달라졌는지 추적이 끊긴다.

`source="rule"` 로 기록되므로, 나중에 LLM 판단을 얹더라도 같은 인터페이스에 `source="llm"`
로 갈아끼우면 된다(모델의 `POLICY_SOURCE_VALUES` 가 이미 셋 다 허용).

## HITL — 이 모듈은 아무것도 저장하지 않는다

AGENTS §1 (자동 적용 금지). 여기서 만든 후보는 `POST /policy-snapshot/preview-update` 가
`isDraft=true` 로 내려주고, 사용자가 `POST /policy-snapshot/apply` 를 눌러야 INSERT 된다.
"주간 KPI 가 전주 대비 10%↓ 이면 자동 롤백" 같은 **자동 적용 규칙은 넣지 않았다** — 잠금
결정과 정면으로 부딪히므로 팀 결정(AGENTS §8) 대상이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PolicyCandidate",
    "PolicyChange",
    "PolicyInputs",
    "baseline_policy",
    "build_candidate",
]

# 활성 스냅샷이 없는 사용자의 v1 기준값. `first_plan_adapter` 의 standard 프리셋(180분)과
# 맞춘다 — 정책이 계획 생성의 입력이므로 두 기본값이 어긋나면 v1 을 적용하는 순간 계획
# 분량이 이유 없이 바뀐다.
_DEFAULT_DAILY_MAX_LOAD_MIN = 180
_DEFAULT_BUFFER_RATIO = 0.2
_DEFAULT_MIN_RECOVERY_STEP_MIN = 10

# 보정 한계 — 룰이 폭주해 극단값으로 가지 않게 양쪽을 막는다.
_MIN_DAILY_LOAD_MIN = 60
_MAX_DAILY_LOAD_MIN = 480
_MIN_RECOVERY_STEP_FLOOR_MIN = 5

# 판정 임계값. 근거가 되는 KPI 는 `period_summaries` 컬럼 그대로다.
_LOW_ADHERENCE = 0.6
_HIGH_ADHERENCE = 0.9
_LOW_RESILIENCE = 0.5
_HIGH_DELAY_MIN = 20.0


@dataclass(frozen=True, slots=True)
class PolicyChange:
    """사용자가 승인 화면에서 보는 변경 한 줄 — **근거를 숫자로** 담는다."""

    area: str
    """behavioral_profile | execution_constraints | interaction_style | recovery_policy"""
    field_name: str
    before: Any
    after: Any
    why: str


@dataclass(frozen=True, slots=True)
class PolicyInputs:
    """후보 산출에 쓰는 주간 KPI — `period_summaries` 에서 뽑은 값만.

    전부 optional 인 이유: 주간 집계는 데이터가 모자라면 컬럼을 `None` 으로 둔다
    (`weekly_review.py`). None 인 지표는 그 규칙을 **건너뛴다** — 0 으로 읽으면
    "한 번도 안 지켰다" 로 오해해 엉뚱한 보정을 한다.
    """

    adherence_rate: float | None = None
    resilience_rate: float | None = None
    avg_delay_minutes: float | None = None
    drain_point_window: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyCandidate:
    """다음 버전 후보 — 4 영역 + 변경 목록 + 사유 한 줄."""

    behavioral_profile: dict[str, Any]
    execution_constraints: dict[str, Any]
    interaction_style: dict[str, Any]
    recovery_policy: dict[str, Any]
    changes: list[PolicyChange] = field(default_factory=list)
    reason_for_update: str | None = None


def baseline_policy(
    *,
    behavioral: Any | None = None,
    interaction: Any | None = None,
) -> PolicyCandidate:
    """활성 스냅샷이 없을 때의 v1 기준값 — 프로필 테이블에서 끌어온다.

    지금 **모든 사용자가 스냅샷 0개**라(#168) 이 경로가 사실상 첫 진입점이다. 인터뷰가
    이미 채워 둔 `behavioral_profiles`/`interaction_styles` 를 그대로 v1 로 삼으면,
    사용자는 자기가 답한 값이 정책으로 승격되는 걸 보게 된다 — 낯선 기본값이 아니라.
    """
    return PolicyCandidate(
        behavioral_profile={
            "attention_span": getattr(behavioral, "attention_span", None) or 30,
            "energy_cycle": getattr(behavioral, "energy_cycle", None) or "varies",
            "time_chunk_preference": getattr(behavioral, "time_chunk_preference", None) or "30",
            "success_buffer": float(getattr(behavioral, "success_buffer", None) or 0.0),
        },
        execution_constraints={
            "daily_max_load": _DEFAULT_DAILY_MAX_LOAD_MIN,
            "buffer_ratio": _DEFAULT_BUFFER_RATIO,
            "no_touch_zones": [],
        },
        interaction_style={
            "suggestion_style": getattr(interaction, "suggestion_style", None) or "neutral",
            "recovery_tone": getattr(interaction, "recovery_tone", None) or "normal",
            "explanation_depth": getattr(interaction, "explanation_depth", None) or "normal",
            "reminder_frequency": getattr(interaction, "reminder_frequency", None) or "standard",
        },
        recovery_policy={
            "default_strategy_per_tag": {},
            "min_recovery_step_minutes": _DEFAULT_MIN_RECOVERY_STEP_MIN,
        },
    )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def build_candidate(current: PolicyCandidate, kpi: PolicyInputs) -> PolicyCandidate:
    """현재 정책 + 주간 KPI → 다음 버전 후보. **순수 함수** (DB·LLM 접근 없음).

    변경할 게 없으면 `changes=[]` 인 후보를 그대로 돌려준다 — 라우터가 그걸 보고 "이번 주는
    바꿀 게 없다" 를 알린다. 억지로 뭔가 바꾸지 않는다.
    """
    behavioral = dict(current.behavioral_profile)
    execution = dict(current.execution_constraints)
    interaction = dict(current.interaction_style)
    recovery = dict(current.recovery_policy)
    changes: list[PolicyChange] = []

    # ① 계획을 자꾸 못 지키면 하루 부하를 줄인다 — 실패의 가장 흔한 원인이 '너무 많이 담기'다.
    #    반대로 잘 지키고 있으면 조금 올린다(양방향이라야 학습 루프다).
    load = int(execution.get("daily_max_load") or _DEFAULT_DAILY_MAX_LOAD_MIN)
    if kpi.adherence_rate is not None and kpi.adherence_rate < _LOW_ADHERENCE:
        new_load = _clamp(round(load * 0.8), _MIN_DAILY_LOAD_MIN, _MAX_DAILY_LOAD_MIN)
        if new_load != load:
            execution["daily_max_load"] = new_load
            changes.append(
                PolicyChange(
                    area="execution_constraints",
                    field_name="daily_max_load",
                    before=load,
                    after=new_load,
                    why=(
                        f"지난주 계획 이행률이 {kpi.adherence_rate:.0%} 였어요. "
                        "하루에 담는 양을 줄여 지킬 수 있는 계획으로 맞춰요."
                    ),
                )
            )
    elif kpi.adherence_rate is not None and kpi.adherence_rate > _HIGH_ADHERENCE:
        new_load = _clamp(round(load * 1.1), _MIN_DAILY_LOAD_MIN, _MAX_DAILY_LOAD_MIN)
        if new_load != load:
            execution["daily_max_load"] = new_load
            changes.append(
                PolicyChange(
                    area="execution_constraints",
                    field_name="daily_max_load",
                    before=load,
                    after=new_load,
                    why=(
                        f"지난주 계획 이행률이 {kpi.adherence_rate:.0%} 였어요. "
                        "여유가 있어 보여 하루 분량을 조금 늘려요."
                    ),
                )
            )

    # ② 회복(다시 시작)이 잘 안 되면 첫 걸음을 더 작게 — 재시작 문턱을 낮춘다.
    step = int(recovery.get("min_recovery_step_minutes") or _DEFAULT_MIN_RECOVERY_STEP_MIN)
    if kpi.resilience_rate is not None and kpi.resilience_rate < _LOW_RESILIENCE:
        new_step = max(_MIN_RECOVERY_STEP_FLOOR_MIN, round(step * 0.5))
        if new_step != step:
            recovery["min_recovery_step_minutes"] = new_step
            changes.append(
                PolicyChange(
                    area="recovery_policy",
                    field_name="min_recovery_step_minutes",
                    before=step,
                    after=new_step,
                    why=(
                        f"지난주 회복률이 {kpi.resilience_rate:.0%} 였어요. "
                        "다시 시작하는 첫 걸음을 더 작게 잡아요."
                    ),
                )
            )

    # ③ 시작이 자꾸 밀리면 여유(success_buffer)를 키운다 — 계획 시각과 실제 시작의 간극.
    if kpi.avg_delay_minutes is not None and kpi.avg_delay_minutes > _HIGH_DELAY_MIN:
        before = float(behavioral.get("success_buffer") or 0.0)
        after = round(min(before + 0.1, 0.5), 2)
        if after != before:
            behavioral["success_buffer"] = after
            changes.append(
                PolicyChange(
                    area="behavioral_profile",
                    field_name="success_buffer",
                    before=before,
                    after=after,
                    why=(
                        f"시작이 평균 {kpi.avg_delay_minutes:.0f}분 밀렸어요. "
                        "계획에 여유를 조금 더 둬요."
                    ),
                )
            )

    # ④ 에너지가 빠지는 시간대가 잡히면 '건드리지 않는 구간' 후보로 올린다.
    #    자동 적용이 아니라 **승인 화면에 올리는 것**이므로 사용자가 거절할 수 있다.
    zones = list(execution.get("no_touch_zones") or [])
    if kpi.drain_point_window and kpi.drain_point_window not in zones:
        after_zones = [*zones, kpi.drain_point_window]
        execution["no_touch_zones"] = after_zones
        changes.append(
            PolicyChange(
                area="execution_constraints",
                field_name="no_touch_zones",
                before=zones,
                after=after_zones,
                why=(
                    f"'{kpi.drain_point_window}' 에 집중이 자주 무너졌어요. "
                    "그 시간대는 비워 둘까요?"
                ),
            )
        )

    return PolicyCandidate(
        behavioral_profile=behavioral,
        execution_constraints=execution,
        interaction_style=interaction,
        recovery_policy=recovery,
        changes=changes,
        reason_for_update=_summarize(changes),
    )


def _summarize(changes: list[PolicyChange]) -> str | None:
    """`policy_snapshots.reason_for_update`(VARCHAR(200)) 한 줄 요약."""
    if not changes:
        return None
    head = " · ".join(f"{c.field_name} {c.before}→{c.after}" for c in changes[:3])
    more = f" 외 {len(changes) - 3}건" if len(changes) > 3 else ""
    return f"주간 KPI 반영: {head}{more}"[:200]
