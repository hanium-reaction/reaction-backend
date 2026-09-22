"""목표 tier 한도 — Focus ≤ 3 / Maintain ≤ 5 (DevBaseline §1.4 잠금 결정) 의 단일 구현.

한도를 거는 경로가 다섯 곳이다 — 목표 추가(`POST /goals`), tier 변경(`PATCH /goals/{id}`),
완료 되돌리기, 만다라 축 승격(`promote`·`/plans/mandala/next-cycle`), 인박스 → 목표.
예전엔 라우터마다 "세고 → 넣기" 를 따로 적었는데 **잠금이 없었다.** 느린 모바일에서
'추가' 를 두 번 누르면 두 요청이 같은 개수(2)를 읽고 둘 다 넣어 Focus 가 4개가 됐고,
인박스 '목표로' 를 연달아 누르면 Maintain 이 9개까지 갔다(미러 재현). 잠금 결정이 한 번
깨지면 되돌릴 방법이 없다.

그래서 **세기 전에 사용자 단위 advisory lock** 을 잡는다(`user_agent_lock`, xact 범위).
lock 은 호출자의 commit/rollback 까지 유지되므로 "세기 → 넣기 → commit" 이 한 덩어리가 되고,
뒤 요청은 앞 요청이 commit 한 개수를 보고 평범한 422 를 받는다. 기다림은 최대 5초, 그 뒤엔
기존 409 `AGENT_CONCURRENT_ACCESS`. 새 에러 코드·envelope 는 없다.

문구는 화면이 쓰는 이름(집중/유지/보류)으로 적는다 — 예전 문구는 `Focus 목표는…` 이라
FE 가 그대로 띄우면 앱 어디에도 없는 영어 이름이 나왔다.
"""

from __future__ import annotations

from http import HTTPStatus
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.orchestrator._common import user_agent_lock
from reaction_backend.repositories.goal_repo import GoalRepo
from reaction_backend.schemas.errors import ApiError, ErrorCode
from reaction_backend.schemas.goals import GOAL_TITLE_MAX_LENGTH

TIER_LIMITS: dict[str, int] = {"focus": 3, "maintain": 5}  # parked 자유 (DevBaseline §1.4)
TIER_LABEL_KO: dict[str, str] = {"focus": "집중", "maintain": "유지", "parked": "보류"}

# 한도를 거는 모든 쓰기 경로가 **같은 키**를 잡아야 서로 직렬화된다.
_TIER_LOCK_AGENT = "goal_tier"

# 한도가 찼을 때 무엇을 하면 되는지 — 비난 없이, 할 수 있는 일만 적는다.
_HOW_TO_MAKE_ROOM: dict[str, str] = {
    "focus": "하나를 유지·보류로 옮기거나 완료하면 새로 담을 수 있어요.",
    "maintain": "하나를 보류로 옮기거나 완료하면 새로 담을 수 있어요.",
}


def tier_label(tier: str) -> str:
    return TIER_LABEL_KO.get(tier, tier)


def tier_limit_error(tier: str, limit: int) -> ApiError:
    """422 `GOAL_TIER_LIMIT_EXCEEDED` — 코드·field 는 그대로, 문구만 화면 말로."""
    how = _HOW_TO_MAKE_ROOM.get(tier, "")
    message = f"{tier_label(tier)} 목표는 최대 {limit}개까지예요. {how}".strip()
    return ApiError(
        ErrorCode.GOAL_TIER_LIMIT_EXCEEDED,
        message,
        http_status=HTTPStatus.UNPROCESSABLE_ENTITY,
        field="goalTier",
    )


async def hold_tier_lock(session: AsyncSession, user_id: UUID) -> None:
    """이 사용자의 tier 한도 쓰기를 직렬화하는 lock 을 **트랜잭션 끝까지** 잡는다.

    `pg_advisory_xact_lock` 이라 `async with` 블록을 나가도 풀리지 않는다 — 호출자의
    commit/rollback 이 푼다. 같은 트랜잭션에서 다시 잡아도 즉시 통과한다(재진입).

    멱등 판정(이미 승격한 축인가 등)을 lock **뒤에서** 읽어야 할 때 직접 부른다 — lock 전에
    읽은 행은 앞 요청이 commit 하기 전 값이라 두 요청이 모두 "아직 없음" 을 본다.
    """
    async with user_agent_lock(session, user_id, _TIER_LOCK_AGENT):
        return


async def enforce_tier_limit(
    session: AsyncSession, repo: GoalRepo, user_id: UUID, tier: str
) -> None:
    """tier 에 **하나 더** 담을 수 있는지 — 못 담으면 422. Parked 는 한도가 없다.

    lock 을 잡은 **뒤에** 센다. 호출자는 이 호출 뒤 같은 트랜잭션 안에서 넣고 commit 한다.
    """
    limit = TIER_LIMITS.get(tier)
    if limit is None:
        return
    await hold_tier_lock(session, user_id)
    if await repo.count_by_tier(user_id, tier) + 1 > limit:
        raise tier_limit_error(tier, limit)


async def promote_axis(
    session: AsyncSession,
    repo: GoalRepo,
    *,
    node: GoalNode,
    user_id: UUID,
    goal_tier: str,
) -> tuple[Goal, bool]:
    """만다라 축(depth=1) → 이번 학기 `Goal(status="proposed")`. 반환: (목표, 새로 만들었나).

    `POST /goals/mandala/nodes/{id}/promote` 와 `POST /plans/mandala/next-cycle` 이 **같은
    규칙**을 쓴다 — 예전엔 두 라우터가 목표 만들기를 각자 복제했다.

    - **멱등** — 이미 승격돼 그 목표가 살아 있으면 그 행을 그대로(새로 안 만든다, tier 도 안
      잰다: 이미 있는 목표의 다음 주기를 여는 데 한도를 걸면 Focus 가 꽉 찬 사용자가 자기 목표를
      못 연다). 지웠으면(보관) 새로 만든다.
    - 멱등 판정(`promoted_goal_id`)은 tier lock **뒤에서 다시 읽는다** — lock 전에 읽은 값은 앞
      요청이 커밋하기 전이라, 두 번 탭한 두 요청이 모두 "아직 승격 전" 을 보고 같은 축으로
      목표를 두 개 만든다.
    - 새로 만들 때만 한도(Focus≤3/Maintain≤5)를 잰다. category 는 `other`(만다라 축엔 분류
      개념이 없다 — 승격 뒤 사용자가 PATCH), 우선순위 3, 이유는 축의 `why_text`.

    commit 은 호출자 몫이다(lock 도 그때 풀린다).
    """
    await hold_tier_lock(session, user_id)
    await session.refresh(node)
    if node.promoted_goal_id is not None:
        existing = await repo.get_by_id(user_id, node.promoted_goal_id)
        if existing is not None:
            return existing, False

    await enforce_tier_limit(session, repo, user_id, goal_tier)
    goal = Goal()
    # id 는 flush 로 받지 않고 여기서 채운다 — 곧바로 `node.promoted_goal_id` 로 써야 하고,
    # DB 왕복 없이도(테스트의 fake session 포함) 항상 값이 있어야 한다.
    goal.id = uuid4()
    goal.user_id = user_id
    goal.title = node.title
    goal.category = "other"
    goal.goal_tier = goal_tier
    goal.status = "proposed"
    goal.priority_level = 3
    goal.is_ultimate = False  # 승격된 목표는 축의 파생물이지 궁극목표 자체가 아니다
    goal.why_now = node.why_text
    session.add(goal)
    await session.flush()
    node.promoted_goal_id = goal.id
    return goal, True


def clip_goal_title(text: str) -> str:
    """서버가 **사용자 글에서** 만드는 목표 제목을 `goals.title` 길이에 맞춘다.

    요청 스키마가 막는 건 사용자가 직접 적는 제목뿐이다. 인박스 메모(→ 목표)나 궁극목표
    인터뷰 문장처럼 길이 제한이 없는 글을 제목으로 옮기는 경로는 그대로 200자를 넘겨
    `StringDataRightTruncation` → 500 이 났고, 다시 눌러도 영영 성공할 수 없었다.
    원문은 각자의 자리(인박스 항목·인터뷰 기록)에 그대로 남으니 제목만 줄인다.
    """
    text = text.strip()
    if len(text) <= GOAL_TITLE_MAX_LENGTH:
        return text
    return text[: GOAL_TITLE_MAX_LENGTH - 1].rstrip() + "…"


__all__ = [
    "TIER_LABEL_KO",
    "TIER_LIMITS",
    "clip_goal_title",
    "enforce_tier_limit",
    "hold_tier_lock",
    "promote_axis",
    "tier_label",
    "tier_limit_error",
]
