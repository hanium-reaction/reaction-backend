"""first_plan_adapter 승인=교체(supersede) — 같은 날짜 카드/블록 중복 누적 방지.

배경: generate 는 기존 블록을 busy 로 보지 않고 approve 는 무조건 INSERT 만 해서,
재생성→재승인을 반복하면 같은 날짜에 동일 카드/블록이 겹겹이 쌓였다(운영에서 같은 제목
×5, 같은 시각 4중첩 관측). 승인 시 같은 target_date 의 이전 AI 계획 산출물 중 사용자가
손대지 않은 것(action_item: source='goal' & status='planned')만 soft 정리한다:
- action_item → archived_at (soft delete, AGENTS §2 hard delete 금지)
- scheduled_block → block_status='cancelled'
시작한 카드(in_progress 등)와 inbox/manual/recovery 카드는 이력 보존을 위해 유지.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.orchestrator.first_plan_adapter import (
    db_apply_first_plan,
    supersede_previous_plan,
)
from reaction_backend.orchestrator.interview_adapter import PLACEHOLDER_GOAL_TITLE
from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.interview import (
    AvailabilityProfile,
    GoalCandidate,
    IdentityContext,
    InterviewOutcome,
    PreferenceProfile,
    TimeRange,
)
from reaction_backend.schemas.planning import (
    ActionItemDraft,
    GoalNodeDraft,
    ScheduledBlockPreview,
)

UID = UUID("22222222-2222-4222-8222-222222222222")
TARGET = date(2026, 7, 8)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _EntitySession:
    """select 대상 entity 별로 시드+추가 행을 돌려주는 fake session — add/commit 기록.

    WHERE 는 평가하지 않는다 — supersede 가 파이썬 술어(`_replaceable_action`)로
    이중 방어하므로, 조건 불일치 행을 시드해 '건드리지 않음'을 검증할 수 있다.
    `execute` 는 `session.add()` 된 객체도 함께 돌려준다 — supersede/트리 보관이
    INSERT **이전**에 실행된다는 순서를 테스트가 고정할 수 있게 (뒤로 밀리면 방금
    삽입한 새 계획을 제 손으로 보관해 버리는 회귀가 잡힌다).
    """

    def __init__(
        self,
        *,
        goals: list[Goal] | None = None,
        actions: list[ActionItem] | None = None,
        blocks: list[ScheduledBlock] | None = None,
        nodes: list[GoalNode] | None = None,
    ) -> None:
        self._by_entity: dict[Any, list[Any]] = {
            Goal: goals or [],
            ActionItem: actions or [],
            ScheduledBlock: blocks or [],
            GoalNode: nodes or [],
        }
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0]["entity"]
        seeded = self._by_entity.get(entity, [])
        added = [o for o in self.added if isinstance(o, entity)]
        return _Result([*seeded, *added])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _action(
    *,
    status: str = "planned",
    source: str = "goal",
    target: date = TARGET,
    archived: bool = False,
) -> ActionItem:
    a = ActionItem()
    a.id = uuid4()
    a.user_id = UID
    a.title = "이전 계획 카드"
    a.target_date = target
    a.estimated_minutes = 30
    a.status = status
    a.source = source
    a.category = "study"
    a.priority = 3
    a.archived_at = now_kst() if archived else None
    return a


def _sched_block(
    action: ActionItem, *, status: str = "scheduled", source: str = "ai_plan"
) -> ScheduledBlock:
    b = ScheduledBlock()
    b.id = uuid4()
    b.user_id = UID
    b.action_item_id = action.id
    b.start_at = datetime.combine(TARGET, time(9, 0), tzinfo=KST)
    b.end_at = b.start_at + timedelta(minutes=30)
    b.block_status = status
    b.source = source
    b.external_calendar_event_id = None
    return b


def _goal_row(title: str = "캡스톤") -> Goal:
    g = Goal()
    g.id = uuid4()
    g.user_id = UID
    g.title = title
    g.category = "study"
    g.goal_tier = "focus"
    g.status = "active"
    g.archived_at = None
    return g


def _node_row(goal_id: Any) -> GoalNode:
    n = GoalNode()
    n.id = uuid4()
    n.goal_id = goal_id
    n.title = "이전 승인 트리 노드"
    n.node_type = "core"
    n.depth = 0
    n.order_index = 0
    n.is_leaf = True
    n.archived_at = None
    return n


# ───────────────────── supersede_previous_plan (단위) ─────────────────────


async def test_supersede_archives_planned_goal_actions_and_cancels_blocks() -> None:
    """미시작(planned·goal) 카드만 보관 + 그 블록만 취소 — 나머지는 불변."""
    stale = _action()  # 교체 대상
    started = _action(status="in_progress")  # 시작한 카드 — 보존
    manual = _action(source="manual")  # 사용자 직접 카드 — 보존
    stale_block = _sched_block(stale)
    started_block = _sched_block(started, status="started")

    sess = _EntitySession(actions=[stale, started, manual], blocks=[stale_block, started_block])
    replaced = await supersede_previous_plan(sess, user_id=UID, target_date=TARGET)  # type: ignore[arg-type]

    assert replaced == 1
    assert stale.archived_at is not None
    assert stale_block.block_status == "cancelled"
    # 시작한 카드/블록과 manual 카드는 그대로.
    assert started.archived_at is None
    assert started_block.block_status == "started"
    assert manual.archived_at is None


async def test_supersede_ignores_other_dates() -> None:
    """다른 날짜(어제) 카드는 이 승인의 교체 범위가 아니다."""
    yesterday = _action(target=TARGET - timedelta(days=1))
    sess = _EntitySession(actions=[yesterday], blocks=[_sched_block(yesterday)])

    replaced = await supersede_previous_plan(sess, user_id=UID, target_date=TARGET)  # type: ignore[arg-type]

    assert replaced == 0
    assert yesterday.archived_at is None


async def test_supersede_already_cancelled_block_stays() -> None:
    """이미 cancelled 인 블록은 재마킹 없이 그대로 (멱등)."""
    stale = _action()
    done_block = _sched_block(stale, status="cancelled")
    sess = _EntitySession(actions=[stale], blocks=[done_block])

    await supersede_previous_plan(sess, user_id=UID, target_date=TARGET)  # type: ignore[arg-type]

    assert done_block.block_status == "cancelled"


async def test_supersede_preserves_user_edited_blocks() -> None:
    """사용자가 직접 옮긴(user_edit) 블록을 가진 카드는 통째로 보존.

    S15 에서 블록을 09:00→20:00 으로 옮기면 block.source='user_edit' 이 되지만 카드는
    여전히 planned 다 — 카드 층 술어만 보면 교체돼 버려 사용자의 수동 배치가 소리 없이
    사라진다. 블록 층 보호를 검증한다.
    """
    moved_card = _action()
    moved_block = _sched_block(moved_card, source="user_edit")
    plain_card = _action()
    plain_block = _sched_block(plain_card)

    sess = _EntitySession(actions=[moved_card, plain_card], blocks=[moved_block, plain_block])
    replaced = await supersede_previous_plan(sess, user_id=UID, target_date=TARGET)  # type: ignore[arg-type]

    # user_edit 블록을 가진 카드는 보존 — 카드도 블록도 불변.
    assert replaced == 1
    assert moved_card.archived_at is None
    assert moved_block.block_status == "scheduled"
    # 손대지 않은 카드만 교체.
    assert plain_card.archived_at is not None
    assert plain_block.block_status == "cancelled"


# ───────────────────── db_apply_first_plan 통합 (SAVING 경로) ─────────────────────


def _outcome(*, placeholder: bool = False) -> InterviewOutcome:
    title = PLACEHOLDER_GOAL_TITLE if placeholder else "캡스톤"
    return InterviewOutcome(
        session_id="iv_replace",
        generated_at=now_kst(),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[
            GoalCandidate(
                title=title,
                category="study",
                is_heaviest=not placeholder,
                tentative_tier="focus",
                confidence=0.0 if placeholder else 0.9,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["오전"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True),
        unresolved_slots=["goals.list"] if placeholder else [],
        horizon=None,
    )


def _new_plan_parts() -> tuple[
    list[GoalNodeDraft], list[ActionItemDraft], list[ScheduledBlockPreview]
]:
    node = GoalNodeDraft(
        node_id="n1", parent_id=None, title="목표", node_type="root", order_index=0, is_leaf=True
    )
    action = ActionItemDraft(
        node_id="n1", title="새 작업", estimated_minutes=30, category="study", first_step="시작"
    )
    start = datetime.combine(TARGET, time(14, 0), tzinfo=KST)
    block = ScheduledBlockPreview(
        start=start,
        end=start + timedelta(minutes=30),
        title="새 작업",
        category="study",
        origin="goal",
        origin_id="n1",
    )
    return [node], [action], [block]


async def test_db_apply_supersedes_previous_before_insert() -> None:
    """SAVING: 이전 planned 카드+블록을 정리한 뒤 새 계획을 영속화한다."""
    stale = _action()
    stale_block = _sched_block(stale)
    sess = _EntitySession(actions=[stale], blocks=[stale_block])
    nodes, actions, blocks = _new_plan_parts()

    result = await db_apply_first_plan(
        sess,  # type: ignore[arg-type]
        user_id=UID,
        target_date=TARGET,
        outcome=_outcome(),
        goal_nodes=nodes,
        action_items=actions,
        blocks=blocks,
        time_policies=[],
    )

    # 이전 산출물 정리
    assert stale.archived_at is not None
    assert stale_block.block_status == "cancelled"
    # 새 계획 영속화
    assert result.goals == 1
    assert result.action_items == 1
    assert result.scheduled_blocks == 1
    new_actions = [o for o in sess.added if isinstance(o, ActionItem)]
    assert len(new_actions) == 1 and new_actions[0].title == "새 작업"
    # 순서 고정: supersede 가 INSERT 이전에 실행됐어야 새 카드가 살아남는다.
    # (_EntitySession.execute 는 added 객체도 돌려주므로, supersede 가 INSERT 뒤로
    #  밀리면 방금 만든 새 카드가 제 손에 보관돼 이 단언이 깨진다.)
    assert all(a.archived_at is None for a in new_actions)
    new_blocks = [o for o in sess.added if isinstance(o, ScheduledBlock)]
    assert all(b.block_status == "scheduled" for b in new_blocks)
    assert sess.committed is True


async def test_db_apply_replaces_previous_goal_node_tree() -> None:
    """heaviest goal 의 기존 활성 트리는 보관되고, 새 트리만 활성으로 남는다.

    goals 는 제목으로 재사용(중복 방지)되지만 goal_nodes 는 승인마다 새로 INSERT 되므로,
    이전 트리를 보관하지 않으면 카드/블록과 같은 누적 버그가 노드에서 반복된다.
    """
    goal = _goal_row()  # outcome 의 '캡스톤' 과 같은 제목 → materialize 가 재사용
    old_node = _node_row(goal.id)
    sess = _EntitySession(goals=[goal], nodes=[old_node])
    nodes, actions, blocks = _new_plan_parts()

    result = await db_apply_first_plan(
        sess,  # type: ignore[arg-type]
        user_id=UID,
        target_date=TARGET,
        outcome=_outcome(),
        goal_nodes=nodes,
        action_items=actions,
        blocks=blocks,
        time_policies=[],
    )

    assert result.goal_nodes == 1
    assert old_node.archived_at is not None  # 이전 트리 보관
    new_nodes = [o for o in sess.added if isinstance(o, GoalNode)]
    assert len(new_nodes) == 1
    assert all(n.archived_at is None for n in new_nodes)  # 새 트리는 활성


async def test_db_apply_finalize_runs_inside_guarded_transaction() -> None:
    """on_success(Draft 승인 마킹 등)는 영속화와 같은 트랜잭션 안(commit 이전)에서 실행."""
    order: list[str] = []

    class _TracingSession(_EntitySession):
        async def commit(self) -> None:
            order.append("commit")
            await super().commit()

    sess = _TracingSession()
    nodes, actions, blocks = _new_plan_parts()

    async def _finalize() -> None:
        order.append("finalize")

    await db_apply_first_plan(
        sess,  # type: ignore[arg-type]
        user_id=UID,
        target_date=TARGET,
        outcome=_outcome(),
        goal_nodes=nodes,
        action_items=actions,
        blocks=blocks,
        time_policies=[],
        on_success=_finalize,
    )

    # finalize → commit 순서 = 같은 트랜잭션, 단일 commit (advisory lock 이 풀리기 전).
    assert order == ["finalize", "commit"]


async def test_db_apply_placeholder_plan_does_not_supersede() -> None:
    """빈 계획(placeholder 만 → 영속화 대상 없음)은 기존 계획을 지우지 않는다."""
    stale = _action()
    sess = _EntitySession(actions=[stale], blocks=[_sched_block(stale)])
    nodes, actions, blocks = _new_plan_parts()

    result = await db_apply_first_plan(
        sess,  # type: ignore[arg-type]
        user_id=UID,
        target_date=TARGET,
        outcome=_outcome(placeholder=True),
        goal_nodes=nodes,
        action_items=actions,
        blocks=blocks,
        time_policies=[],
    )

    assert result.goals == 0
    assert stale.archived_at is None  # 아무것도 지우지 않음
