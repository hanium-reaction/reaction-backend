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
from reaction_backend.orchestrator.first_plan import _existing_busy_by_day
from reaction_backend.orchestrator.first_plan_adapter import (
    _archive_goal_nodes,
    _persist_milestones_if_new,
    db_apply_first_plan,
    supersede_previous_plan,
    superseded_card_ids,
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
    MilestoneDraft,
    ScheduledBlockPreview,
)

UID = UUID("22222222-2222-4222-8222-222222222222")
TARGET = date(2026, 7, 8)
GOAL = UUID("33333333-3333-4333-8333-333333333333")
OTHER_GOAL = UUID("44444444-4444-4444-8444-444444444444")


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


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
    goal_id: UUID | None = GOAL,
    goal_node_id: UUID | None = None,
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
    a.goal_id = goal_id
    a.goal_node_id = goal_node_id
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


def _goal_row(title: str = "캡스톤", *, goal_id: UUID | None = None) -> Goal:
    g = Goal()
    g.id = goal_id or uuid4()
    g.user_id = UID
    g.title = title
    g.category = "study"
    g.goal_tier = "focus"
    g.status = "active"
    g.archived_at = None
    return g


def _node_row(goal_id: Any, *, tree_kind: str = "plan", node_type: str = "core") -> GoalNode:
    n = GoalNode()
    n.id = uuid4()
    n.goal_id = goal_id
    n.title = "이전 승인 트리 노드"
    n.node_type = node_type
    n.depth = 0
    n.order_index = 0
    n.is_leaf = True
    n.archived_at = None
    n.tree_kind = tree_kind
    n.source = "llm"
    n.locked = False
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
    replaced = await supersede_previous_plan(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

    assert replaced == 1
    assert stale.archived_at is not None
    assert stale_block.block_status == "cancelled"
    # 시작한 카드/블록과 manual 카드는 그대로.
    assert started.archived_at is None
    assert started_block.block_status == "started"
    assert manual.archived_at is None


async def test_supersede_replaces_all_dates_of_the_same_goal() -> None:
    """같은 목표면 **다른 날짜** 카드도 교체된다 — 교체 단위는 '그 목표의 이전 계획 전체'.

    #222 이후 카드 날짜가 자기 블록 날짜를 따라가므로(4주 계획 = 4주치 날짜), 날짜를 교체
    키로 쓰면 이전 계획의 뒷날짜 카드가 빠져 재승인마다 누적된다. 노드층(_archive_goal_nodes)이
    goal 단위로 보관하는 것과도 정합.
    """
    day1 = _action()
    day15 = _action(target=TARGET + timedelta(days=14))
    sess = _EntitySession(actions=[day1, day15], blocks=[_sched_block(day1), _sched_block(day15)])

    replaced = await supersede_previous_plan(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

    assert replaced == 2
    assert day1.archived_at is not None
    assert day15.archived_at is not None


async def test_supersede_ignores_other_goals() -> None:
    """**다른 목표**의 계획은 같은 날 시작했어도 지우지 않는다 — 교체가 아니라 공존.

    회귀 재현: 같은 날 'MVP 만들기' 계획을 승인한 뒤 '토익' 계획을 승인하자, 교체 키가
    target_date(=계획 시작일) 뿐이라 MVP 4주치(카드 12개)가 통째로 보관됐다. 시작일은
    사용자에게 보이지도 않는 값이라 어떤 계획이 사라질지 예측할 수도 없었다.
    """
    mine = _action()  # 이번에 승인하는 목표 — 교체 대상
    others = _action(goal_id=OTHER_GOAL)  # 다른 목표, 같은 시작일 — 보존
    mine_block = _sched_block(mine)
    others_block = _sched_block(others)

    sess = _EntitySession(actions=[mine, others], blocks=[mine_block, others_block])
    replaced = await supersede_previous_plan(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

    assert replaced == 1
    assert mine.archived_at is not None
    assert mine_block.block_status == "cancelled"
    # 다른 목표의 계획은 카드도 블록도 불변.
    assert others.archived_at is None
    assert others_block.block_status == "scheduled"


async def test_superseded_card_ids_excludes_other_goals() -> None:
    """busy 제외도 같은 목표만 — 다른 목표 블록은 busy 로 남아 스케줄러가 피한다.

    generate 가 남의 블록을 busy 에서 빼면 그 위에 겹쳐 배치된다(실측: MVP 17:15~19:15
    한복판에 토익 18:15~18:49). supersede 와 같은 규칙을 쓰는지 고정한다.
    """
    mine = _action()
    others = _action(goal_id=OTHER_GOAL)
    sess = _EntitySession(actions=[mine, others], blocks=[_sched_block(mine), _sched_block(others)])

    ids = await superseded_card_ids(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

    assert ids == {mine.id}


async def test_superseded_card_ids_empty_when_goal_unknown() -> None:
    """목표가 아직 영속되지 않으면(goal_id=None) 아무것도 제외하지 않는다 — 전부 회피.

    잘못 제외하면 남의 계획 위에 겹치고, 잘못 피하면 배치가 뒤로 밀릴 뿐이다 → 안전한 쪽.
    """
    stale = _action()
    sess = _EntitySession(actions=[stale], blocks=[_sched_block(stale)])

    ids = await superseded_card_ids(sess, user_id=UID, goal_id=None)  # type: ignore[arg-type]

    assert ids == set()


async def test_supersede_ignores_mandala_owned_action() -> None:
    """`goal_node_id` 가 만다라 셀을 가리키는 카드는 계획 재승인이 절대 교체하지 않는다(W2).

    만다라 셀에서 승격된 카드가 `source='goal'` 로 같은 goal 아래 계획 카드와 섞여 있어도,
    `_replaceable_action` 이 `mandala_node_ids` 로 걸러 건드리지 않는다.
    """
    mandala_node = _node_row(GOAL, tree_kind="mandala")
    mandala_owned = _action(goal_node_id=mandala_node.id)
    plain = _action()

    sess = _EntitySession(
        actions=[mandala_owned, plain],
        blocks=[_sched_block(mandala_owned), _sched_block(plain)],
        nodes=[mandala_node],
    )
    replaced = await supersede_previous_plan(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

    assert replaced == 1
    assert plain.archived_at is not None
    # 만다라 셀 유래 카드는 불변.
    assert mandala_owned.archived_at is None


async def test_superseded_card_ids_ignores_mandala_owned_action() -> None:
    """busy 제외 계산(`superseded_card_ids`)도 같은 규칙 — 만다라 셀 유래 카드는 빼지 않는다."""
    mandala_node = _node_row(GOAL, tree_kind="mandala")
    mandala_owned = _action(goal_node_id=mandala_node.id)
    plain = _action()

    sess = _EntitySession(
        actions=[mandala_owned, plain],
        blocks=[_sched_block(mandala_owned), _sched_block(plain)],
        nodes=[mandala_node],
    )
    ids = await superseded_card_ids(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

    assert ids == {plain.id}


async def test_supersede_already_cancelled_block_stays() -> None:
    """이미 cancelled 인 블록은 재마킹 없이 그대로 (멱등)."""
    stale = _action()
    done_block = _sched_block(stale, status="cancelled")
    sess = _EntitySession(actions=[stale], blocks=[done_block])

    await supersede_previous_plan(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

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
    replaced = await supersede_previous_plan(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

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
    # 교체는 **같은 목표** 안에서만 일어난다 → 이전 카드의 goal 을 시드해
    # materialize_goals 가 재사용(heaviest.id == GOAL)하게 한다.
    goal = _goal_row(goal_id=GOAL)
    stale = _action()
    stale_block = _sched_block(stale)
    sess = _EntitySession(goals=[goal], actions=[stale], blocks=[stale_block])
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


async def test_db_apply_card_dates_follow_their_blocks() -> None:
    """카드의 target_date = 자기 블록(가장 이른 것)의 KST 날짜 (#222).

    회귀(라이브 실측): 전 카드가 계획 시작일이라 4주 계획 승인 직후 **오늘 아젠다에 28장**
    이 통째로 떴고(아젠다는 target_date 조회), 미래 블록 카드를 오늘 시작하면 주간 리뷰가
    계획 시각 기준으로 왜곡됐다(avgDelayMinutes -3778, peakWindow=계획 슬롯). 블록이 없는
    카드만 계획 시작일 폴백.
    """
    nodes = [
        GoalNodeDraft(
            node_id=f"n{i}",
            parent_id=None,
            title=f"작업{i}",
            node_type="root" if i == 1 else "leaf",
            order_index=i,
            is_leaf=True,
        )
        for i in (1, 2, 3)
    ]
    actions = [
        ActionItemDraft(
            node_id=f"n{i}",
            title=f"작업{i}",
            estimated_minutes=30,
            category="study",
            first_step="시작",
        )
        for i in (1, 2, 3)
    ]

    def _block(node_id: str, day: date, hour: int) -> ScheduledBlockPreview:
        start = datetime.combine(day, time(hour, 0), tzinfo=KST)
        return ScheduledBlockPreview(
            start=start,
            end=start + timedelta(minutes=30),
            title=node_id,
            category="study",
            origin="goal",
            origin_id=node_id,
        )

    blocks = [
        _block("n1", TARGET, 9),
        # n2: 세션이 두 날로 쪼개짐 — **가장 이른** 날이 카드 날짜.
        _block("n2", TARGET + timedelta(days=14), 9),
        _block("n2", TARGET + timedelta(days=1), 9),
        # n3: 블록 없음 → 계획 시작일 폴백.
    ]

    sess = _EntitySession(goals=[_goal_row(goal_id=GOAL)])
    await db_apply_first_plan(
        sess,  # type: ignore[arg-type]
        user_id=UID,
        target_date=TARGET,
        outcome=_outcome(),
        goal_nodes=nodes,
        action_items=actions,
        blocks=blocks,
        time_policies=[],
    )

    by_title = {a.title: a for a in sess.added if isinstance(a, ActionItem)}
    assert by_title["작업1"].target_date == TARGET
    assert by_title["작업2"].target_date == TARGET + timedelta(days=1)  # 이른 블록의 날
    assert by_title["작업3"].target_date == TARGET  # 블록 없음 → 폴백


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


async def test_archive_goal_nodes_spares_mandala_tree() -> None:
    """같은 goal 아래 만다라트(`tree_kind='mandala'`) 노드는 계획 트리 보관이 건드리지 않는다(W1).

    §3.4-b 처럼 궁극목표와 계획 core_goals 제목이 겹쳐 같은 goal 을 가리키게 되는 상황은
    W3(`_active_goals`)가 먼저 막지만, `_archive_goal_nodes` 를 직접 호출해 그 방어가
    뚫리거나 아직 적용되기 전이어도(두 번째 방어선) `tree_kind='plan'` 만 보관됨을 고정한다.
    """
    goal = _goal_row()
    plan_node = _node_row(goal.id, tree_kind="plan")
    mandala_node = _node_row(goal.id, tree_kind="mandala")
    sess = _EntitySession(goals=[goal], nodes=[plan_node, mandala_node])

    archived = await _archive_goal_nodes(sess, goal_id=goal.id)  # type: ignore[arg-type]

    assert archived == 1
    assert plan_node.archived_at is not None  # 이전 계획 트리만 보관
    assert mandala_node.archived_at is None  # 만다라 트리는 불변


# ─────────────── 마일스톤 영속 (ADR-0007 PR-2, _persist_milestones_if_new) ───────────────


def _milestones(*titles: str) -> list[MilestoneDraft]:
    return [MilestoneDraft(title=t, summary=f"{t} 요약") for t in titles]


async def test_archive_goal_nodes_spares_milestone_nodes() -> None:
    """`node_type='milestone'` 은 계획 트리 보관 대상에서 빠진다 — 주기를 넘어 살아남아야
    하는 층이라 매 승인이 갈아치우는 core/subgoal/leaf 와 같이 archive 되면 안 된다."""
    goal = _goal_row()
    ephemeral_node = _node_row(goal.id, tree_kind="plan", node_type="core")
    milestone_node = _node_row(goal.id, tree_kind="plan", node_type="milestone")
    sess = _EntitySession(goals=[goal], nodes=[ephemeral_node, milestone_node])

    archived = await _archive_goal_nodes(sess, goal_id=goal.id)  # type: ignore[arg-type]

    assert archived == 1
    assert ephemeral_node.archived_at is not None  # 매 주기 교체되는 층만 보관
    assert milestone_node.archived_at is None  # 마일스톤은 불변


async def test_persist_milestones_creates_nodes_when_none_exist() -> None:
    goal = _goal_row()
    sess = _EntitySession(goals=[goal])

    rows = await _persist_milestones_if_new(
        sess,  # type: ignore[arg-type]
        goal_id=goal.id,
        milestones=_milestones("기초 문법", "자료구조", "배포까지"),
    )

    assert [n.title for n in rows] == ["기초 문법", "자료구조", "배포까지"]
    assert all(n.node_type == "milestone" for n in rows)
    assert all(n.tree_kind == "plan" for n in rows)
    assert all(n.depth == 1 and n.parent_node_id is None for n in rows)
    assert [n.order_index for n in rows] == [0, 1, 2]
    assert rows[0].why_text == "기초 문법 요약"
    assert sess.added == rows


async def test_persist_milestones_noop_when_list_empty() -> None:
    """확정 마일스톤이 없으면(마일스톤 없이 진행) 아무것도 안 만든다."""
    goal = _goal_row()
    sess = _EntitySession(goals=[goal])

    rows = await _persist_milestones_if_new(
        sess,
        goal_id=goal.id,
        milestones=[],  # type: ignore[arg-type]
    )

    assert rows == []
    assert sess.added == []


async def test_persist_milestones_is_idempotent_when_already_persisted() -> None:
    """이미 활성 마일스톤이 있으면(재승인) 새로 안 만들고 그대로 둔다 — 두 번째 승인이
    같은 목록을 다시 보내도 중복 생성되지 않는다."""
    goal = _goal_row()
    existing_milestone = _node_row(goal.id, tree_kind="plan", node_type="milestone")
    sess = _EntitySession(goals=[goal], nodes=[existing_milestone])

    rows = await _persist_milestones_if_new(
        sess,  # type: ignore[arg-type]
        goal_id=goal.id,
        milestones=_milestones("기초 문법", "자료구조"),
    )

    assert rows == []
    assert sess.added == []
    assert existing_milestone.title == "이전 승인 트리 노드"  # 손 안 댐


async def test_persist_milestones_ignores_archived_milestones() -> None:
    """보관된(archived) 마일스톤은 '있음'으로 안 친다 — 새로 만들 수 있어야 한다."""
    goal = _goal_row()
    archived_milestone = _node_row(goal.id, tree_kind="plan", node_type="milestone")
    archived_milestone.archived_at = now_kst()
    sess = _EntitySession(goals=[goal], nodes=[archived_milestone])

    rows = await _persist_milestones_if_new(
        sess,  # type: ignore[arg-type]
        goal_id=goal.id,
        milestones=_milestones("기초 문법"),
    )

    assert [n.title for n in rows] == ["기초 문법"]


async def test_db_apply_first_plan_persists_milestones_alongside_new_tree() -> None:
    """승인(approve) 경로 전체 — db_apply_first_plan 에 milestones 를 실으면 함께 영속된다."""
    goal = _goal_row(goal_id=GOAL)
    sess = _EntitySession(goals=[goal])
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
        milestones=_milestones("기초 문법", "배포까지"),
    )

    milestone_nodes = [
        n for n in sess.added if isinstance(n, GoalNode) and n.node_type == "milestone"
    ]
    assert [n.title for n in milestone_nodes] == ["기초 문법", "배포까지"]
    # goal_nodes 카운트에 이번 4주 트리(1) + 마일스톤(2) 가 함께 잡힌다.
    assert result.goal_nodes == 1 + 2


async def test_db_apply_first_plan_reapproval_keeps_milestones_and_replaces_leaf_tree() -> None:
    """재승인(같은 goal 을 다시 승인) — 마일스톤은 그대로, 이전 4주 트리만 교체된다.

    "재승인"을 두 번째 `db_apply_first_plan` 호출로 흉내낸다 — DB 에 이미 있는 상태(마일스톤
    + 이전 4주 트리)를 이 호출의 시드로 미리 넣어 둔다.
    """
    goal = _goal_row(goal_id=GOAL)  # outcome 의 '캡스톤' 과 제목이 같아 재사용된다
    old_milestone = _node_row(goal.id, tree_kind="plan", node_type="milestone")
    old_leaf_tree = _node_row(goal.id, tree_kind="plan", node_type="core")
    sess = _EntitySession(goals=[goal], nodes=[old_milestone, old_leaf_tree])
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
        milestones=_milestones("기초 문법", "배포까지"),  # 같은 목록을 다시 보냄
    )

    assert old_milestone.archived_at is None  # 마일스톤은 주기를 넘어 산다
    assert old_leaf_tree.archived_at is not None  # 4주 트리는 매 승인 교체
    new_milestone_nodes = [
        n for n in sess.added if isinstance(n, GoalNode) and n.node_type == "milestone"
    ]
    assert new_milestone_nodes == []  # 중복 생성 없음
    assert result.goal_nodes == 1  # 이번 승인이 새로 만든 건 4주 트리 1개뿐


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


# ───────────────── 재생성 시 자기 이전 계획 busy 제외 (#118) ─────────────────
# superseded_card_ids = approve 시 supersede 가 교체할 카드 (같은 규칙, read-only).
# generate 는 이 카드들의 블록을 busy 에서 빼, 재생성 계획이 곧 비워질 슬롯을 피하지 않게.


async def test_superseded_card_ids_matches_supersede_rule() -> None:
    """교체 대상(goal·planned·미보관·user_edit 없음) 카드만 반환 — supersede 와 동일.

    다른 날짜 카드도 같은 목표면 포함된다(#222) — 카드 날짜가 블록 날짜를 따라가므로
    교체 단위는 목표의 이전 계획 전체다.
    """
    stale = _action()  # 교체 대상
    started = _action(status="in_progress")  # 실행 이력 → 제외
    moved = _action()  # user_edit 블록 보유 → 보존(제외)
    other_day = _action(target=TARGET - timedelta(days=1))  # 같은 목표의 다른 날짜 → 포함
    sess = _EntitySession(
        actions=[stale, started, moved, other_day],
        blocks=[_sched_block(stale), _sched_block(moved, source="user_edit")],
    )

    ids = await superseded_card_ids(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]

    assert ids == {stale.id, other_day.id}


async def test_superseded_card_ids_empty_on_first_plan() -> None:
    """교체 대상이 없으면(첫 계획) 빈 집합 → busy 제외 no-op."""
    sess = _EntitySession(actions=[_action(status="in_progress")], blocks=[])
    ids = await superseded_card_ids(sess, user_id=UID, goal_id=GOAL)  # type: ignore[arg-type]
    assert ids == set()


async def test_existing_busy_excludes_own_superseded_plan() -> None:
    """generate busy: 자기 이전 계획(교체 대상) 블록은 빠지고, 시작된 카드 블록은 남는다."""
    prev = _action()  # 이전 계획(goal/planned/오늘) → 승인 시 supersede
    prev_block = _sched_block(prev)  # scheduled·ai_plan
    started = _action(status="in_progress")  # 실행 중 → busy 유지
    started_block = _sched_block(started, status="started")
    sess = _EntitySession(actions=[prev, started], blocks=[prev_block, started_block])
    config: Any = {"configurable": {"session": sess}}

    busy = await _existing_busy_by_day(
        config, UID, TARGET, TARGET, exclude_target_date=TARGET, exclude_goal_id=GOAL
    )
    kept = [b for blist in busy.values() for b in blist]
    assert len(kept) == 1  # started 만 busy, prev 는 제외

    # exclude 안 하면 둘 다 busy (제외 로직이 실제로 동작함을 대조).
    busy_all = await _existing_busy_by_day(config, UID, TARGET, TARGET)
    assert sum(len(v) for v in busy_all.values()) == 2
