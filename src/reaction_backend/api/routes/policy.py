"""Policy Snapshot — 학습 루프의 산출물.

구성 (PolicySnapshot 4 영역):
- behavioral_profile     — attention_span, energy_cycle, time_chunk_preference, success_buffer
- execution_constraints  — daily_max_load, buffer_ratio, no_touch_zones
- interaction_style      — suggestion_style, recovery_tone, explanation_depth, reminder_frequency
- recovery_policy        — default_strategy_per_tag, min_recovery_step_minutes

규칙:
- 버전 보존(valid_from/valid_to). 롤백 가능
- 주간 KPI가 전주 대비 10%↓이면 자동 롤백 후보
- 새 버전 생성은 사용자 명시 [적용] (Verifier diff 표시) 후

DB: policy_snapshots (버전 이력), behavioral_profiles, interaction_styles

endpoint (#168 로 전부 구현됨 — 그전엔 `current` 하나뿐이라 계약 문서와 어긋나 있었다):
- GET  /policy-snapshot/current             — 현재 활성 정책
- GET  /policy-snapshot/history             — 버전 이력
- POST /policy-snapshot/preview-update      — 다음 버전 후보 (Draft, 저장 안 함)
- POST /policy-snapshot/apply               — 사용자 승인 후 INSERT
- POST /policy-snapshot/rollback/{version}  — 이전 버전 값을 새 버전으로 되살림

후보 산출은 **룰**(`orchestrator/policy_update.py`)이다 — LLM 이 아니다. 근거는 그 모듈
docstring 참고(승인 화면이 "왜 이 값이 됐나" 를 숫자로 보여줘야 하고, 정책은 이후 모든
계획 생성의 입력이라 비결정적이면 추적이 끊긴다). `source` 컬럼이 rule/llm/user_manual 을
전부 허용하므로 나중에 LLM 판단을 같은 자리에 끼울 수 있다.
"""

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.policy_snapshot import PolicySnapshot
from reaction_backend.db.session import get_db
from reaction_backend.orchestrator import policy_update
from reaction_backend.repositories.policy_snapshot_repo import (
    PolicySnapshotRepo,
    get_policy_snapshot_repo,
)
from reaction_backend.repositories.profile_repo import ProfileRepo, get_profile_repo
from reaction_backend.repositories.review_repo import ReviewRepo, get_review_repo
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.policy import (
    PolicyApplyRequest,
    PolicyChangeItem,
    PolicyHistoryItem,
    PolicyHistoryResponse,
    PolicyPreviewResponse,
    PolicySnapshotResponse,
)

router = APIRouter(prefix="/policy-snapshot", tags=["policy"])

PolicySnapshotRepoDep = Annotated[PolicySnapshotRepo, Depends(get_policy_snapshot_repo)]
ReviewRepoDep = Annotated[ReviewRepo, Depends(get_review_repo)]
ProfileRepoDep = Annotated[ProfileRepo, Depends(get_profile_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


def _as_float(value: object) -> float | None:
    """Numeric 컬럼은 Decimal 로 온다 — 룰 산술에 넣기 전에 float 로. None 은 그대로."""
    return None if value is None else float(value)  # type: ignore[arg-type]


def _to_response(snapshot: PolicySnapshot) -> PolicySnapshotResponse:
    return PolicySnapshotResponse(
        version=snapshot.version,
        source=snapshot.source,
        behavioral_profile=snapshot.behavioral_profile,
        execution_constraints=snapshot.execution_constraints,
        interaction_style=snapshot.interaction_style,
        recovery_policy=snapshot.recovery_policy,
        reason_for_update=snapshot.reason_for_update,
        valid_from=snapshot.valid_from,
    )


@router.get("/current")
async def get_current_policy(
    user: CurrentUser,
    repo: PolicySnapshotRepoDep,
) -> PolicySnapshotResponse:
    """현재 활성 PolicySnapshot (#83) — 없으면 404 (FE 는 카운트-only 폴백 유지)."""
    snapshot = await repo.get_active(user.id)
    if snapshot is None:
        raise ApiError(
            ErrorCode.POLICY_NOT_FOUND,
            "아직 활성 정책 스냅샷이 없어요.",
            http_status=HTTPStatus.NOT_FOUND,
        )
    return _to_response(snapshot)


@router.get("/history")
async def get_policy_history(
    user: CurrentUser,
    repo: PolicySnapshotRepoDep,
) -> PolicyHistoryResponse:
    """버전 이력 — 최신이 앞. 비어 있으면 `items: []` (404 아님).

    `current` 와 달리 404 를 내지 않는다: "아직 정책이 없다" 는 이력 화면에서 **정상 상태**
    이고, 빈 목록으로 표현하는 게 클라이언트에 더 쉽다.
    """
    rows = await repo.list_history(user.id)
    return PolicyHistoryResponse(
        items=[
            PolicyHistoryItem(
                version=r.version,
                source=r.source,
                is_active=r.is_active,
                reason_for_update=r.reason_for_update,
                valid_from=r.valid_from,
                valid_to=r.valid_to,
            )
            for r in rows
        ]
    )


async def _current_or_baseline(
    user_id: uuid.UUID,
    *,
    repo: PolicySnapshotRepo,
    profile_repo: ProfileRepo,
) -> tuple[policy_update.PolicyCandidate, int | None]:
    """현재 정책(4 영역) + 활성 버전. 스냅샷이 없으면 프로필에서 v1 기준값을 만든다.

    지금 사실상 **모든 사용자가 스냅샷 0개**라(#168) 이 폴백이 첫 진입 경로다.
    """
    active = await repo.get_active(user_id)
    if active is not None:
        return (
            policy_update.PolicyCandidate(
                behavioral_profile=dict(active.behavioral_profile),
                execution_constraints=dict(active.execution_constraints),
                interaction_style=dict(active.interaction_style),
                recovery_policy=dict(active.recovery_policy),
            ),
            active.version,
        )
    return (
        policy_update.baseline_policy(
            behavioral=await profile_repo.get_behavioral(user_id),
            interaction=await profile_repo.get_interaction(user_id),
        ),
        None,
    )


@router.post("/preview-update")
async def preview_policy_update(
    user: CurrentUser,
    repo: PolicySnapshotRepoDep,
    review_repo: ReviewRepoDep,
    profile_repo: ProfileRepoDep,
) -> PolicyPreviewResponse:
    """다음 버전 후보 — **저장하지 않는다** (AGENTS §1 자동 적용 금지).

    입력은 가장 최근 주간 요약(`period_summaries`). 아직 한 주도 집계 안 됐으면 KPI 없이
    현재 값을 그대로 후보로 돌려준다(`changes: []`) — 그 자체가 "바꿀 근거가 아직 없다"다.
    """
    current, base_version = await _current_or_baseline(
        user.id, repo=repo, profile_repo=profile_repo
    )
    summary = await review_repo.get_latest_weekly(user.id)
    kpi = policy_update.PolicyInputs(
        adherence_rate=_as_float(getattr(summary, "adherence_rate", None)),
        resilience_rate=_as_float(getattr(summary, "resilience_rate", None)),
        avg_delay_minutes=_as_float(getattr(summary, "avg_delay_minutes", None)),
        drain_point_window=getattr(summary, "drain_point_window", None),
    )
    candidate = policy_update.build_candidate(current, kpi)
    return PolicyPreviewResponse(
        is_draft=True,
        ai_source="rule",
        base_version=base_version,
        next_version=await repo.next_version(user.id),
        changes=[
            PolicyChangeItem(
                area=c.area, field=c.field_name, before=c.before, after=c.after, why=c.why
            )
            for c in candidate.changes
        ],
        reason_for_update=candidate.reason_for_update,
        behavioral_profile=candidate.behavioral_profile,
        execution_constraints=candidate.execution_constraints,
        interaction_style=candidate.interaction_style,
        recovery_policy=candidate.recovery_policy,
    )


@router.post("/apply", status_code=HTTPStatus.CREATED)
async def apply_policy_update(
    body: PolicyApplyRequest,
    user: CurrentUser,
    repo: PolicySnapshotRepoDep,
    session: SessionDep,
) -> PolicySnapshotResponse:
    """사용자 승인 후 새 버전 INSERT — HITL 게이트가 여기다.

    본문은 `preview-update` 응답을 그대로(=룰 그대로) 또는 사용자가 고친 값이다. 서버가
    후보를 다시 계산해 덮어쓰지 않는다 — 그러면 사용자가 화면에서 본 값과 저장된 값이
    달라질 수 있다(미리보기와 적용 사이에 KPI 가 갱신되면). **본 것이 저장된다.**
    """
    snapshot = await repo.create_active(
        user.id,
        behavioral_profile=body.behavioral_profile,
        execution_constraints=body.execution_constraints,
        interaction_style=body.interaction_style,
        recovery_policy=body.recovery_policy,
        source=body.source,
        reason_for_update=body.reason_for_update,
        now=now_kst(),
    )
    await session.commit()
    return _to_response(snapshot)


@router.post("/rollback/{version}", status_code=HTTPStatus.CREATED)
async def rollback_policy(
    version: int,
    user: CurrentUser,
    repo: PolicySnapshotRepoDep,
    session: SessionDep,
) -> PolicySnapshotResponse:
    """지난 버전의 값을 **새 버전으로** 되살린다.

    옛 행의 `is_active` 를 다시 켜지 않는 이유: 그러면 그 행의 `valid_from` 이 최초 활성화
    시각 그대로라 **언제 롤백했는지가 이력에서 사라진다.** 정책 이력은 감사 기록이므로
    (ADR-0001 §3.2 append-only) 값을 복사한 새 행을 만들어 타임라인을 온전히 남긴다.
    버전 번호는 계속 늘어난다 — v5 에서 v2 로 롤백하면 v2 의 값을 가진 v6 이 생긴다.

    없는 버전은 404, 이미 활성인 버전은 409(같은 값을 한 번 더 쌓을 이유가 없다).
    """
    target = await repo.get_by_version(user.id, version)
    if target is None:
        raise ApiError(
            ErrorCode.POLICY_NOT_FOUND,
            f"v{version} 정책 스냅샷을 찾을 수 없어요.",
            http_status=HTTPStatus.NOT_FOUND,
        )
    if target.is_active:
        raise ApiError(
            ErrorCode.POLICY_ALREADY_ACTIVE,
            f"v{version} 은 이미 활성 정책이에요.",
            http_status=HTTPStatus.CONFLICT,
        )

    snapshot = await repo.create_active(
        user.id,
        behavioral_profile=dict(target.behavioral_profile),
        execution_constraints=dict(target.execution_constraints),
        interaction_style=dict(target.interaction_style),
        recovery_policy=dict(target.recovery_policy),
        source="user_manual",
        reason_for_update=f"v{version} 으로 롤백",
        now=now_kst(),
    )
    await session.commit()
    return _to_response(snapshot)
