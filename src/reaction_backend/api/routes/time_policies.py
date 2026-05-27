"""Time Policies — 시간 정책 (S07, api-contract §5).

Issue #17 실구현:
- CRUD 실 DB (`time_policies` 테이블)
- `prefill-from-interview` — InterviewSlotAnswer 룰 매칭 + default 후보 (DB 미저장)
- 첫 POST 시 onboarding_state 전이: POLICIES → FIRST_PLAN
- soft delete (`archived_at` + `is_active=false`)
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.db.models.interview_session import InterviewSession
from reaction_backend.db.models.interview_slot_answer import InterviewSlotAnswer
from reaction_backend.db.models.time_policy import TimePolicy as TimePolicyModel
from reaction_backend.db.session import get_db
from reaction_backend.repositories.time_policy_repo import (
    TimePolicyRepo,
    get_time_policy_repo,
)
from reaction_backend.repositories.user_repo import UserRepo, get_user_repo
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.time_policies import (
    TimePolicy,
    TimePolicyCreateRequest,
    TimePolicyUpdateRequest,
)

router = APIRouter(prefix="/time-policies", tags=["time-policies"])

_ID_PREFIX = "policy_"


def _to_schema(policy: TimePolicyModel) -> TimePolicy:
    return TimePolicy(
        policy_id=f"{_ID_PREFIX}{policy.id}",
        policy_type=policy.policy_type,
        payload=dict(policy.payload),
        is_active=policy.is_active,
    )


def _parse_policy_id(policy_id: str) -> UUID:
    if not policy_id.startswith(_ID_PREFIX):
        raise _not_found()
    try:
        return UUID(policy_id[len(_ID_PREFIX) :])
    except ValueError as e:
        raise _not_found() from e


def _not_found() -> ApiError:
    return ApiError(
        ErrorCode.POLICY_NOT_FOUND,
        "해당 시간 정책을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


RepoDep = Annotated[TimePolicyRepo, Depends(get_time_policy_repo)]
UserRepoDep = Annotated[UserRepo, Depends(get_user_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


# ───── prefill 룰 ─────


async def _fetch_user_slot_answers(
    session: AsyncSession, user_id: UUID
) -> dict[str, dict[str, Any]]:
    """사용자의 모든 인터뷰 세션의 슬롯 답변. `slot_key → value` 형태로 평탄화.

    여러 세션이 있으면 가장 최근 세션의 값이 우선 (created_at desc 로 정렬 후 첫 매칭 유지).
    """
    stmt = (
        select(InterviewSlotAnswer.slot_key, InterviewSlotAnswer.value)
        .join(InterviewSession, InterviewSession.id == InterviewSlotAnswer.session_id)
        .where(InterviewSession.user_id == user_id)
        .order_by(InterviewSlotAnswer.created_at.desc())
    )
    result = await session.execute(stmt)
    flat: dict[str, dict[str, Any]] = {}
    for slot_key, value in result.all():
        if slot_key not in flat and isinstance(value, dict):
            flat[slot_key] = value
    return flat


def _range_payload(value: dict[str, Any], default_start: str, default_end: str) -> dict[str, str]:
    if value.get("type") == "range":
        start = value.get("start")
        end = value.get("end")
        return {
            "start_time": str(start) if isinstance(start, str) else default_start,
            "end_time": str(end) if isinstance(end, str) else default_end,
        }
    return {"start_time": default_start, "end_time": default_end}


def _build_prefill_candidates(
    answers: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """슬롯 답변 → (policy_type, payload) 후보 리스트.

    규칙 (Issue #17, prefill 단순화):
    - time.sleep_window → sleep (필수 1개)
    - time.lunch       → lunch
    - time.peak_hours  → no_touch (평일 peak 외 보호)
    - 항상 추가        → break_min 15 / late_night_block 22:00~
    """
    out: list[tuple[str, dict[str, Any]]] = []

    sleep = answers.get("time.sleep_window")
    out.append(
        (
            "sleep",
            _range_payload(sleep, "23:00", "07:00")
            if sleep
            else {
                "start_time": "23:00",
                "end_time": "07:00",
            },
        )
    )

    lunch = answers.get("time.lunch")
    if lunch:
        out.append(("lunch", _range_payload(lunch, "12:00", "13:00")))

    peak = answers.get("time.peak_hours")
    if peak:
        out.append(
            (
                "no_touch",
                {
                    **_range_payload(peak, "09:00", "18:00"),
                    "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
                },
            )
        )

    out.append(("break_min", {"min_minutes": 15}))
    out.append(
        (
            "late_night_block",
            {"start_time": "22:00", "blocked_categories": []},
        )
    )
    return out


# ───── endpoints ─────


@router.get("")
async def list_policies(user: CurrentUser, repo: RepoDep) -> list[TimePolicy]:
    """내 활성 시간 정책 전체."""
    items = await repo.list_active(user.id)
    return [_to_schema(p) for p in items]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: TimePolicyCreateRequest,
    user: CurrentUser,
    repo: RepoDep,
    user_repo: UserRepoDep,
    session: SessionDep,
) -> TimePolicy:
    """신규 시간 정책 추가.

    부수 효과: `ONBOARDING_POLICIES` → `ONBOARDING_FIRST_PLAN` 으로 전이 (멱등).
    """
    policy = await repo.create(user.id, body.policy_type, dict(body.payload))
    await user_repo.advance_onboarding(
        user,
        expected_from="ONBOARDING_POLICIES",
        to="ONBOARDING_FIRST_PLAN",
    )
    await session.commit()
    await session.refresh(policy)
    return _to_schema(policy)


@router.post("/prefill-from-interview")
async def prefill_from_interview(user: CurrentUser, session: SessionDep) -> list[TimePolicy]:
    """인터뷰 답 기반 정책 후보 (DB 미저장).

    응답의 `policyId` 는 prefill 임시 식별자 — FE 는 사용자 선택 후 POST `/time-policies` 로 실제 저장.
    """
    answers = await _fetch_user_slot_answers(session, user.id)
    candidates = _build_prefill_candidates(answers)
    return [
        TimePolicy(
            policy_id=f"{_ID_PREFIX}prefill_{i}",
            policy_type=ptype,
            payload=payload,
            is_active=True,
        )
        for i, (ptype, payload) in enumerate(candidates)
    ]


@router.patch("/{policy_id}")
async def update_policy(
    policy_id: str,
    body: TimePolicyUpdateRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> TimePolicy:
    """시간 정책 부분 수정 (`payload`/`is_active`)."""
    policy = await repo.get_by_id(user.id, _parse_policy_id(policy_id))
    if policy is None:
        raise _not_found()
    updated = await repo.update(
        policy,
        payload=dict(body.payload) if body.payload is not None else None,
        is_active=body.is_active,
    )
    await session.commit()
    await session.refresh(updated)
    return _to_schema(updated)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: str,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> None:
    """시간 정책 soft delete (`archived_at` + `is_active=false`)."""
    policy = await repo.get_by_id(user.id, _parse_policy_id(policy_id))
    if policy is None:
        raise _not_found()
    await repo.soft_delete(policy)
    await session.commit()
    return None
