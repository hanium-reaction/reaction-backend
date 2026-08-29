"""Inbox — Life Inbox (S24) + Triage (S25) (api-contract §18).

Issue #22-B 실구현:
- `POST /inbox` — `aiClient.run("inbox/classify")` Sequential Parser + 룰 fallback + 암호화 저장
- `GET /inbox?status=` — list (raw_text 복호화 응답)
- `PATCH /inbox/{id}` — user_category override / status 변경
- `POST /inbox/{id}/convert-to-goal` — Goal 생성 + Maintain 한도 enforce + inbox.status=promoted
- `POST /inbox/{id}/convert-to-action` — ActionItem(`source=inbox`) 생성 + inbox.status=promoted
- `POST /inbox/{id}/archive` — soft delete (status=archived)

raw_text 는 `safety.encryption` AES-256-GCM (`raw_text_encrypted` 컬럼). 응답엔 복호화 평문.
LLM 호출은 `llm.aiClient` Tool Executor 경유 (AGENTS.md §2, ADR-0003 / ADR-0005 §4 단계 5 Sequential).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.deps import CurrentUser
from reaction_backend.content import registry
from reaction_backend.content.registry import ContentNotFound
from reaction_backend.db.models.inbox_item import InboxItem as InboxItemModel
from reaction_backend.db.session import get_db
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import inbox_resources
from reaction_backend.orchestrator.inbox_coaching import build_coaching_advice
from reaction_backend.repositories.action_item_repo import (
    ActionItemRepo,
    get_action_item_repo,
)
from reaction_backend.repositories.goal_repo import GoalRepo, get_goal_repo
from reaction_backend.repositories.habit_repo import HabitRepo, get_habit_repo
from reaction_backend.repositories.inbox_repo import InboxRepo, get_inbox_repo
from reaction_backend.safety.encryption import decrypt_inbox_text, encrypt_inbox_text
from reaction_backend.schemas.common import KST
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.inbox import (
    InboxAdoptedStep,
    InboxAdoptStepRequest,
    InboxCategory,
    InboxClassification,
    InboxCoachingAdvice,
    InboxCreateRequest,
    InboxItem,
    InboxResourceDetail,
    InboxUpdateRequest,
)

router = APIRouter(prefix="/inbox", tags=["inbox"])

_ID_PREFIX = "inbox_"
_MAINTAIN_LIMIT = 5  # convert-to-goal 기본 tier=maintain 한도 (DevBaseline §1.4)

# 룰 fallback 키워드 매칭 — LLM 실패 시 사용 (Tool Executor 가 자동 분기).
_KEYWORD_MAP: tuple[tuple[str, InboxCategory], ...] = (
    ("공부", "study"),
    ("강의", "study"),
    ("토익", "study"),
    ("시험", "study"),
    ("학습", "study"),
    ("프로젝트", "project"),
    ("캡스톤", "project"),
    ("과제", "project"),
    ("코딩", "project"),
    ("운동", "health"),
    ("헬스", "health"),
    ("달리기", "health"),
    ("다이어트", "health"),
    ("약속", "schedule"),
    ("미팅", "schedule"),
    ("회의", "schedule"),
    ("습관", "routine"),
    ("루틴", "routine"),
    ("매일", "routine"),
)


def _rule_fallback_classify(raw_text: str) -> InboxClassification:
    """LLM 실패 시 키워드 매칭. confidence=0 → 항상 사용자 override 권장."""
    text_lower = raw_text.lower()
    category: InboxCategory = "other"
    for kw, cat in _KEYWORD_MAP:
        if kw in text_lower:
            category = cat
            break
    return InboxClassification(
        ai_category_guess=category,
        confidence=0.0,
        suggested_title=raw_text[:10],
        needs_user_override=True,
    )


def _to_schema(item: InboxItemModel) -> InboxItem:
    # 승격 대상 파생 — promoted 인데 goal_id 있으면 goal, 없으면 action(convert-to-action 경로).
    promoted_to: Literal["goal", "action"] | None = None
    if item.status == "promoted":
        promoted_to = "goal" if item.promoted_goal_id is not None else "action"
    return InboxItem(
        inbox_id=f"{_ID_PREFIX}{item.id}",
        raw_text=decrypt_inbox_text(item.raw_text_encrypted),
        ai_category_guess=item.ai_category_guess,
        user_category=item.user_category,
        status=item.status,
        promoted_goal_id=(
            f"goal_{item.promoted_goal_id}" if item.promoted_goal_id is not None else None
        ),
        promoted_to=promoted_to,
        # DB enum 은 2값뿐이지만 server_default 가 flush 전 ORM 인스턴스에는 적용되지
        # 않아 None 일 수 있다 — 'system' 이 아니면 사용자 캡처로 본다.
        source="system" if item.source == "system" else "user",
        resource_slug=item.resource_slug,
    )


def _parse_inbox_id(inbox_id: str) -> UUID:
    if not inbox_id.startswith(_ID_PREFIX):
        raise _not_found()
    try:
        return UUID(inbox_id[len(_ID_PREFIX) :])
    except ValueError as e:
        raise _not_found() from e


def _not_found() -> ApiError:
    return ApiError(
        ErrorCode.INBOX_NOT_FOUND,
        "해당 Inbox 항목을 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _resource_not_found() -> ApiError:
    """자료 없음 — `INBOX_NOT_FOUND` 를 쓰면 FE 가 '항목이 삭제됨' 으로 오해한다."""
    return ApiError(
        ErrorCode.COMMON_NOT_FOUND,
        "요청한 자료를 찾을 수 없어요.",
        http_status=HTTPStatus.NOT_FOUND,
    )


def _reject_system_item(item: InboxItemModel) -> None:
    """추천 자료는 목표·실행으로 승격할 수 없다 (#171 5단계).

    FE 는 자료 카드에서 승격 버튼을 숨기지만, 서버 가드가 없으면 직접 호출로 뚫린다.
    가드가 없으면 자료 → 목표 → 새 자료 로 이어지는 재귀도 성립한다.
    """
    if item.source == "system":
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "추천 자료는 목표나 할 일로 바꿀 수 없어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="inboxId",
        )


def _today_kst() -> datetime:
    return datetime.now(KST)


RepoDep = Annotated[InboxRepo, Depends(get_inbox_repo)]
GoalRepoDep = Annotated[GoalRepo, Depends(get_goal_repo)]
HabitRepoDep = Annotated[HabitRepo, Depends(get_habit_repo)]
ActionRepoDep = Annotated[ActionItemRepo, Depends(get_action_item_repo)]
SessionDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("")
async def list_inbox(
    user: CurrentUser,
    repo: RepoDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[InboxItem]:
    """내 inbox 항목. `?status=captured|classified|promoted|archived` 필터.

    `status` 미지정 시 활성 항목(archived 제외)만. `status=archived` 는 보관함(soft-deleted)
    조회 — `POST /inbox/{id}/restore` 로 되살릴 수 있다.
    """
    items = await repo.list_by_status(user.id, status_filter)
    return [_to_schema(i) for i in items]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_inbox(
    body: InboxCreateRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> InboxItem:
    """1줄 캡처 + AI category 추정 (LLM 1회, 8s timeout, 실패 시 룰 fallback)."""
    # 1) AI category 추정 — Tool Executor 가 prompt 렌더·budget·safety·fallback 일괄 처리.
    result = await aiClient.run(
        module="inbox",
        schema=InboxClassification,
        prompt_id="inbox/classify",
        fallback=lambda: _rule_fallback_classify(body.raw_text),
        timeout=8.0,
        variables={"raw_text": body.raw_text},
        user_id=user.id,
        session=session,
        tone_mode=user.tone_mode,
    )
    classification = result.value

    # 2) 암호화 저장 — raw_text_encrypted = AES-256-GCM(b"reaction:inbox-text").
    item = await repo.create(
        user_id=user.id,
        raw_text_encrypted=encrypt_inbox_text(body.raw_text),
        ai_category_guess=classification.ai_category_guess,
        status="classified",  # ai_category_guess 채워졌으므로 자동 classified
    )
    await session.commit()
    await session.refresh(item)
    return _to_schema(item)


@router.get("/resources/{slug}")
async def get_inbox_resource(slug: str) -> InboxResourceDetail:
    """시스템 항목이 가리키는 정적 자료 본문 (#171).

    **인증만 필요하고 소유권 검사는 하지 않는다** — 자료는 사용자별 개인 데이터가 아니라
    레포에 커밋된 정적 공용 콘텐츠라 소유권 개념이 없다. 인증은 라우터 단위로 걸려 있다.

    `slug` 는 레지스트리의 dict 키 조회에만 쓰이고 경로와 결합되지 않으므로 경로 순회가
    성립하지 않는다. 규격을 벗어난 slug 는 그냥 404 로 떨어진다.
    """
    try:
        doc = registry.get(slug)
    except ContentNotFound as e:
        # `ContentNotFound` 는 KeyError 상속이라 그대로 새면 404 가 아니라 500 이 된다.
        raise _resource_not_found() from e
    # `ContentDoc` 을 그대로 반환하면 `path`(서버 절대경로)가 응답에 실린다 — 명시 매핑.
    return InboxResourceDetail(
        slug=doc.slug, title=doc.title, markdown=doc.body, steps=list(doc.steps)
    )


@router.get("/coaching-advice")
async def get_coaching_advice(
    user: CurrentUser,
    goal_repo: GoalRepoDep,
    habit_repo: HabitRepoDep,
    action_repo: ActionRepoDep,
) -> list[InboxCoachingAdvice]:
    """사용자 목표·습관·실행 기록에서 최대 3개의 근거 기반 조언을 만든다."""
    generated_at = _today_kst()
    today = generated_at.date()
    goals = await goal_repo.list_active(user.id)
    habits = await habit_repo.list_active(user.id)
    today_actions = await action_repo.list_by_date(user.id, today)
    yesterday_actions = await action_repo.list_by_date(user.id, today - timedelta(days=1))
    return build_coaching_advice(
        goals=goals,
        habits=habits,
        today_actions=today_actions,
        yesterday_actions=yesterday_actions,
        today=today,
        generated_at=generated_at,
    )


@router.patch("/{inbox_id}")
async def update_inbox(
    inbox_id: str,
    body: InboxUpdateRequest,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> InboxItem:
    """userCategory override 또는 status 변경."""
    item = await repo.get_by_id(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()
    updated = await repo.update(
        item,
        user_category=body.user_category,
        status=body.status,
    )
    await session.commit()
    await session.refresh(updated)
    return _to_schema(updated)


@router.post("/{inbox_id}/convert-to-goal")
async def convert_to_goal(
    inbox_id: str,
    user: CurrentUser,
    repo: RepoDep,
    goal_repo: GoalRepoDep,
    session: SessionDep,
) -> InboxItem:
    """Inbox → Goal 변환 (tier=maintain default, 한도 enforce). inbox.status=promoted."""
    item = await repo.get_by_id(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()
    _reject_system_item(item)

    # Maintain ≤ 5 enforce (Parked 였다면 별도 — 기본 maintain 으로 진입)
    current = await goal_repo.count_by_tier(user.id, "maintain")
    if current + 1 > _MAINTAIN_LIMIT:
        raise ApiError(
            ErrorCode.GOAL_TIER_LIMIT_EXCEEDED,
            f"Maintain 목표는 최대 {_MAINTAIN_LIMIT}개까지 가질 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="goalTier",
        )

    raw_text = decrypt_inbox_text(item.raw_text_encrypted)
    category = item.user_category or item.ai_category_guess or "other"
    goal = await goal_repo.create(
        user_id=user.id,
        title=raw_text,
        category=category,
        goal_tier="maintain",
        priority_level=3,
    )
    await repo.mark_promoted_to_goal(item, goal.id)
    # 승격된 목표 카테고리의 추천 자료를 인박스에 (#171). best-effort.
    await inbox_resources.ensure_resources_best_effort(
        session, repo, user_id=user.id, goal_categories=[goal.category]
    )
    await session.commit()
    await session.refresh(item)
    return _to_schema(item)


@router.post("/{inbox_id}/convert-to-action")
async def convert_to_action(
    inbox_id: str,
    user: CurrentUser,
    repo: RepoDep,
    action_repo: ActionRepoDep,
    session: SessionDep,
) -> InboxItem:
    """Inbox → ActionItem(source=inbox) 변환. inbox.status=promoted."""
    item = await repo.get_by_id(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()
    _reject_system_item(item)
    raw_text = decrypt_inbox_text(item.raw_text_encrypted)
    category = item.user_category or item.ai_category_guess or "other"
    await action_repo.create_from_inbox(
        user_id=user.id,
        inbox_item_id=item.id,
        title=raw_text[:300],  # ActionItem.title 컬럼 길이 제한
        category=category,
        target_date=_today_kst().date(),
    )
    await repo.mark_promoted_to_action(item)
    await session.commit()
    await session.refresh(item)
    return _to_schema(item)


@router.post("/{inbox_id}/adopt-step")
async def adopt_resource_step(
    inbox_id: str,
    body: InboxAdoptStepRequest,
    user: CurrentUser,
    repo: RepoDep,
    action_repo: ActionRepoDep,
    session: SessionDep,
) -> InboxAdoptedStep:
    """자료가 제안한 '한 걸음'을 오늘 할 일로 채택한다 (#171 후속).

    자료를 읽고 끝나지 않게 하는 **유일한 출구**다. 여기서 만든 `ActionItem` 은
    `source='inbox'` + `inbox_item_id` 로 자료에 연결되므로, 그 뒤 체크인 → 21시 회고
    → 실패 사유 → 회복 카드는 **기존 루프가 그대로** 처리한다. 회복을 새로 만들지 않는다
    (AGENTS §1: 회복 시점은 21시 일괄 회고만).

    자료 항목은 `promoted` 로 바꾸지 않는다 — 한 걸음을 채택한 것이지 자료를 승격한 게
    아니고, 나중에 다른 걸음을 또 채택하거나 다시 읽을 수 있어야 한다.
    """
    item = await repo.get_by_id(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()
    if item.source != "system" or item.resource_slug is None:
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "추천 자료 항목에서만 한 걸음을 가져올 수 있어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="inboxId",
        )

    try:
        doc = registry.get(item.resource_slug)
    except ContentNotFound as e:
        # 자료 파일이 사라졌다(레포에서 제거). 항목은 남아 있지만 가리킬 곳이 없다.
        raise _resource_not_found() from e

    if body.step_index >= len(doc.steps):
        raise ApiError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            "그 한 걸음을 찾을 수 없어요.",
            http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
            field="stepIndex",
        )
    step = doc.steps[body.step_index]

    target_date = _today_kst().date()
    # 같은 걸음을 두 번 눌러도 카드는 하나다 (#213). 헤더 멱등(24h TTL) 대신 도메인
    # dedup 인 이유: 활성 카드만 보므로 날짜가 바뀌거나 카드를 보관한 뒤에는 같은
    # 걸음을 다시 담을 수 있다 — "어제 actionId 를 replay 하는 조용한 거짓"이 없다.
    existing = await action_repo.find_adopted_step(
        user_id=user.id,
        inbox_item_id=item.id,
        title=step,
        target_date=target_date,
    )
    if existing is not None:
        return InboxAdoptedStep(
            action_id=f"action_{existing.id}",
            title=step,
            target_date=target_date.isoformat(),
            resource_slug=doc.slug,
        )
    action = await action_repo.create_from_inbox(
        user_id=user.id,
        inbox_item_id=item.id,
        title=step,
        # 자료가 어느 분야인지는 **자료 자신이 권위**다. `ai_category_guess` 는 inbox
        # enum 이 6종뿐이라 career/relationship/self_dev 가 other 로 접힌 값이라,
        # 그걸 쓰면 9종을 받는 ActionItem 에 손실을 전파하고 주간 category_success_rate
        # 버킷이 영영 안 생긴다. 사용자가 명시적으로 재분류했다면 그건 존중한다.
        category=item.user_category or doc.category,
        target_date=target_date,
    )
    await session.commit()
    return InboxAdoptedStep(
        action_id=f"action_{action.id}",
        title=step,
        target_date=target_date.isoformat(),
        resource_slug=doc.slug,
    )


@router.post("/{inbox_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_inbox(
    inbox_id: str,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> None:
    """Inbox 항목 soft delete (`archived_at` + `status=archived`)."""
    item = await repo.get_by_id(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()
    await repo.soft_delete(item)
    await session.commit()
    return None


@router.post("/{inbox_id}/restore")
async def restore_inbox(
    inbox_id: str,
    user: CurrentUser,
    repo: RepoDep,
    session: SessionDep,
) -> InboxItem:
    """보관 취소 — `archived_at` 클리어 + status 를 활성(classified/captured)으로 복원.

    보관함(`status=archived`)에서만 의미. 이미 활성이면 멱등(현재 상태 그대로 반환).
    존재하지 않으면 404 `INBOX_NOT_FOUND`. hard delete 는 없으므로 언제든 되살릴 수 있다.
    """
    item = await repo.get_by_id_any(user.id, _parse_inbox_id(inbox_id))
    if item is None:
        raise _not_found()
    await repo.restore(item)
    await session.commit()
    await session.refresh(item)
    return _to_schema(item)
