"""마일스톤 영속(ADR-0007 PR-2)을 **실 Postgres 위에서** 검증.

`test_plan_approve_replace.py` 의 `_sync_milestones`/`_archive_goal_nodes` 단위
테스트는 손으로 만든 `_EntitySession` 이다 — WHERE 절을 평가하지 않고(주석에 명시) 파이썬
쪽 술어로만 판정하므로, "이미 활성 마일스톤이 있으면 새로 안 만든다"는 실제로는 SQL
WHERE(`archived_at IS NULL` 등)가 옳아야 성립하는 판정인데 fake session 으로는 그 SQL 자체가
한 번도 실행되지 않는다.

ADR-0007 이 "이 설계의 관문이자 유일한 고위험 지점"이라 부른 게 바로 이 경로다 —
`_archive_goal_nodes`(모든 사용자의 모든 계획 승인이 지나가는 함수)가 필터 하나만
잘못돼도 만다라와 무관한 일반 사용자의 재승인까지 깨질 수 있다. 여기서는 실 DB로
"재승인 반복 시 마일스톤은 유지·leaf 트리만 교체·중복 생성 없음"을 직접 확인한다
(같은 문서 §검증 항목).

`db_apply_first_plan`/`_apply_once` 전체는 안 부른다 — 내부에서
`policy_guarded_transaction` 이 `session.commit()` 을 호출하는데, `real_db_session`
픽스처는 그 호출을 명시적으로 금지한다(nested-savepoint 하네스는 아직 없음, 픽스처
docstring 참고). 대신 `_sync_milestones`/`_archive_goal_nodes` 는 둘 다
commit 없이 add/flush 만 하므로 직접 호출로 같은 SQL 경로를 검증할 수 있다.

DATABASE_URL 이 없으면 전부 스킵 — `test_mandala_persist_real_db.py` 와 같은 게이트.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.db.models.user import User
from reaction_backend.orchestrator.first_plan_adapter import (
    _archive_goal_nodes,
    _sync_milestones,
    completed_milestone_cursor,
    fetch_confirmed_milestones,
)
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.planning import MilestoneDraft
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _seed_goal(session: AsyncSession, *, title: str = "캡스톤 프로젝트") -> Goal:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="마일스톤 영속 테스트"))
    goal = Goal()
    goal.id = uuid.uuid4()
    goal.user_id = user_id
    goal.title = title
    goal.category = "study"
    goal.goal_tier = "focus"
    goal.status = "active"
    session.add(goal)
    await session.flush()
    return goal


async def _plan_node_count(
    session: AsyncSession, *, goal_id: uuid.UUID, node_type: str | None = None
) -> int:
    stmt = (
        select(func.count())
        .select_from(GoalNode)
        .where(
            GoalNode.goal_id == goal_id,
            GoalNode.tree_kind == "plan",
            GoalNode.archived_at.is_(None),
        )
    )
    if node_type is not None:
        stmt = stmt.where(GoalNode.node_type == node_type)
    return (await session.scalar(stmt)) or 0


async def test_persist_milestones_writes_real_rows(real_db_session: AsyncSession) -> None:
    goal = await _seed_goal(real_db_session)

    rows = await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary="변수·조건문"),
            MilestoneDraft(title="자료구조", summary=""),
            MilestoneDraft(title="배포까지", summary="CI/CD"),
        ],
    )

    assert len(rows) == 3
    stored = (
        (
            await real_db_session.execute(
                select(GoalNode)
                .where(GoalNode.goal_id == goal.id, GoalNode.node_type == "milestone")
                .order_by(GoalNode.order_index)
            )
        )
        .scalars()
        .all()
    )
    assert [n.title for n in stored] == ["기초 문법", "자료구조", "배포까지"]
    assert all(n.depth == 1 and n.parent_node_id is None for n in stored)
    assert all(n.tree_kind == "plan" and n.archived_at is None for n in stored)
    assert stored[0].why_text == "변수·조건문"


async def test_sync_milestones_is_idempotent_against_real_where_clause(
    real_db_session: AsyncSession,
) -> None:
    """재승인에 **같은 목록**을 다시 보내면 실 SQL WHERE 로도 중복 생성이 없다.

    fake session 판(test_plan_approve_replace.py)은 WHERE 를 평가하지 않아 파이썬 쪽
    이중 필터에 기대는데, 여기서는 그 SQL 자체가 옳은지를 확인한다.
    """
    goal = await _seed_goal(real_db_session)
    same = [
        MilestoneDraft(title="기초 문법", summary=""),
        MilestoneDraft(title="자료구조", summary=""),
    ]
    first = await _sync_milestones(real_db_session, goal_id=goal.id, milestones=same)
    assert len(first) == 2

    second = await _sync_milestones(real_db_session, goal_id=goal.id, milestones=same)

    assert second == []  # 중복 생성 없음
    assert await _plan_node_count(real_db_session, goal_id=goal.id, node_type="milestone") == 2


async def test_sync_milestones_keeps_node_identity_when_only_order_changed(
    real_db_session: AsyncSession,
) -> None:
    """순서만 바꾼 확정 — **같은 행**을 재사용하고 `order_index` 만 갱신한다.

    새로 만들어 갈아치우면 노드 id 가 바뀌고 `completed_at`(ADR-0007 이 유일하게 저장을
    허용한 진척)이 날아간다. 순서 한 번 바꿨다고 진척이 사라지면 안 된다.
    """
    goal = await _seed_goal(real_db_session)
    created = await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary=""),
            MilestoneDraft(title="배포까지", summary=""),
        ],
    )
    ids = {n.title: n.id for n in created}
    done_at = now_kst()
    created[1].completed_at = done_at
    await real_db_session.flush()

    # 사용자가 순서를 뒤집고 요약을 고쳐 확정.
    await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="배포까지", summary="남에게 보여줄 수 있는 상태"),
            MilestoneDraft(title="기초 문법", summary=""),
        ],
    )

    saved = await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)
    assert [m.title for m in saved] == ["배포까지", "기초 문법"]
    assert [m.summary for m in saved] == ["남에게 보여줄 수 있는 상태", ""]
    rows = (
        (
            await real_db_session.execute(
                select(GoalNode).where(
                    GoalNode.goal_id == goal.id,
                    GoalNode.node_type == "milestone",
                    GoalNode.archived_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert {n.title: n.id for n in rows} == ids  # 행이 그대로다 — 재생성 아님
    assert next(n for n in rows if n.title == "배포까지").completed_at == done_at


async def test_sync_milestones_archives_what_the_user_removed(
    real_db_session: AsyncSession,
) -> None:
    """확정 목록에서 뺀 마일스톤은 **보관**된다 — 지우지 않는다(AGENTS §2 hard delete 금지).

    보관이라 `fetch_confirmed_milestones` 에는 안 잡히고(같은 `archived_at` 술어), 행은
    남아 나중에 계보를 되짚을 수 있다.
    """
    goal = await _seed_goal(real_db_session)
    await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary=""),
            MilestoneDraft(title="자료구조", summary=""),
            MilestoneDraft(title="배포까지", summary=""),
        ],
    )

    await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary=""),
            MilestoneDraft(title="배포까지", summary=""),
        ],
    )

    saved = await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)
    assert [m.title for m in saved] == ["기초 문법", "배포까지"]
    total = await real_db_session.scalar(
        select(func.count())
        .select_from(GoalNode)
        .where(GoalNode.goal_id == goal.id, GoalNode.node_type == "milestone")
    )
    assert total == 3  # 보관됐을 뿐 지워지지 않았다


async def test_sync_milestones_leaves_everything_alone_for_an_empty_list(
    real_db_session: AsyncSession,
) -> None:
    """빈 목록은 "뼈대를 지워라" 가 아니라 "이번엔 마일스톤 없이" 다 — 아무것도 안 건드린다.

    FE 는 Stage A 를 건너뛴 경우를 빈 목록으로 표현한다. 이걸 삭제로 해석하면 뼈대를
    가진 사용자가 한 번 건너뛰는 순간 마감까지의 계획 축이 사라진다.
    """
    goal = await _seed_goal(real_db_session)
    await _sync_milestones(
        real_db_session, goal_id=goal.id, milestones=[MilestoneDraft(title="기초 문법", summary="")]
    )

    assert await _sync_milestones(real_db_session, goal_id=goal.id, milestones=[]) == []

    saved = await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)
    assert [m.title for m in saved] == ["기초 문법"]


async def test_archive_goal_nodes_spares_real_milestone_rows(real_db_session: AsyncSession) -> None:
    goal = await _seed_goal(real_db_session)
    await _sync_milestones(
        real_db_session, goal_id=goal.id, milestones=[MilestoneDraft(title="기초 문법", summary="")]
    )
    ephemeral = GoalNode()
    ephemeral.goal_id = goal.id
    ephemeral.title = "이번 4주 트리"
    ephemeral.node_type = "core"
    ephemeral.depth = 0
    ephemeral.order_index = 0
    ephemeral.is_leaf = False
    ephemeral.tree_kind = "plan"
    real_db_session.add(ephemeral)
    await real_db_session.flush()

    archived = await _archive_goal_nodes(real_db_session, goal_id=goal.id)

    assert archived == 1
    await real_db_session.refresh(ephemeral)
    assert ephemeral.archived_at is not None
    assert await _plan_node_count(real_db_session, goal_id=goal.id, node_type="milestone") == 1


async def test_reapproval_cycle_keeps_milestones_and_replaces_leaf_tree_only(
    real_db_session: AsyncSession,
) -> None:
    """전체 재승인 주기를 실 DB로 재현 — 마일스톤은 두 번째 주기까지 그대로, leaf 트리만 교체.

    ADR-0007 §검증: "재승인 반복 시 마일스톤은 유지 · leaf 만 교체 · 트리 누적 없음".
    """
    goal = await _seed_goal(real_db_session)

    # ── 1주기 승인: 마일스톤 확정 + 이번 4주 트리 ──
    milestones = [
        MilestoneDraft(title="기초 문법", summary=""),
        MilestoneDraft(title="배포까지", summary=""),
    ]
    await _sync_milestones(real_db_session, goal_id=goal.id, milestones=milestones)
    cycle1_leaf = GoalNode()
    cycle1_leaf.goal_id = goal.id
    cycle1_leaf.title = "1주기 leaf"
    cycle1_leaf.node_type = "leaf"
    cycle1_leaf.depth = 2
    cycle1_leaf.order_index = 0
    cycle1_leaf.is_leaf = True
    cycle1_leaf.tree_kind = "plan"
    real_db_session.add(cycle1_leaf)
    await real_db_session.flush()

    assert await _plan_node_count(real_db_session, goal_id=goal.id) == 3  # 마일스톤 2 + leaf 1

    # ── 2주기 재승인: _archive_goal_nodes 로 1주기 leaf 를 보관하고, 새 leaf 를 추가 ──
    archived = await _archive_goal_nodes(real_db_session, goal_id=goal.id)
    assert archived == 1  # 마일스톤은 안 셈
    reused = await _sync_milestones(real_db_session, goal_id=goal.id, milestones=milestones)
    assert reused == []  # 이미 있으니 재생성 안 함
    cycle2_leaf = GoalNode()
    cycle2_leaf.goal_id = goal.id
    cycle2_leaf.title = "2주기 leaf"
    cycle2_leaf.node_type = "leaf"
    cycle2_leaf.depth = 2
    cycle2_leaf.order_index = 0
    cycle2_leaf.is_leaf = True
    cycle2_leaf.tree_kind = "plan"
    real_db_session.add(cycle2_leaf)
    await real_db_session.flush()

    # 활성 상태: 마일스톤 2(그대로) + 2주기 leaf 1 = 3. 1주기 leaf 는 보관돼 안 잡힌다.
    assert await _plan_node_count(real_db_session, goal_id=goal.id) == 3
    assert await _plan_node_count(real_db_session, goal_id=goal.id, node_type="milestone") == 2
    active_leaves = (
        (
            await real_db_session.execute(
                select(GoalNode.title).where(
                    GoalNode.goal_id == goal.id,
                    GoalNode.node_type == "leaf",
                    GoalNode.archived_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert active_leaves == ["2주기 leaf"]  # 트리 누적 없음 — 딱 이번 주기 것만 활성


async def test_fetch_confirmed_milestones_returns_saved_skeleton_in_confirmed_order(
    real_db_session: AsyncSession,
) -> None:
    """Stage A 읽기(ADR-0007 PR-2.5) — 저장된 뼈대를 **사용자가 확정한 순서 그대로** 돌려준다.

    `order_index` 로 정렬한다는 게 이 판정의 핵심이라, 삽입 순서와 다르게 나오는지 보려면
    실 DB 가 필요하다 — fake session 은 ORDER BY 를 평가하지 않는다.
    """
    goal = await _seed_goal(real_db_session)
    confirmed = [
        MilestoneDraft(title="기초 문법", summary="변수와 조건문"),
        MilestoneDraft(title="상태 관리", summary=""),
        MilestoneDraft(title="배포까지", summary="직접 만든 걸 남에게 보여준다"),
    ]
    await _sync_milestones(real_db_session, goal_id=goal.id, milestones=confirmed)

    saved = await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)

    assert [m.title for m in saved] == ["기초 문법", "상태 관리", "배포까지"]
    # summary 는 why_text 로 왕복한다. 빈 요약은 None 으로 저장되므로 ""(스키마 기본값)로 복원.
    assert [m.summary for m in saved] == ["변수와 조건문", "", "직접 만든 걸 남에게 보여준다"]


async def test_fetch_confirmed_milestones_is_empty_before_any_approval(
    real_db_session: AsyncSession,
) -> None:
    """아직 계획을 승인한 적 없으면 빈 리스트 — Stage A 는 그때 LLM 으로 내려간다."""
    goal = await _seed_goal(real_db_session)
    assert await fetch_confirmed_milestones(real_db_session, goal_id=goal.id) == []


async def test_fetch_confirmed_milestones_shares_the_predicate_with_persist(
    real_db_session: AsyncSession,
) -> None:
    """읽기와 쓰기가 **같은 술어**를 봐야 한다 — 갈라지면 매 주기 마일스톤이 쌓인다.

    "읽을 땐 없고 쓸 땐 있다"가 되면 Stage A 는 LLM 으로 새 목록을 만들고, 승인은 그걸
    또 저장하지 않는(또는 중복 저장하는) 상태가 된다. leaf 트리만 보관하는
    `_archive_goal_nodes` 를 지나도 읽기 결과가 변하지 않는지까지 함께 본다.
    """
    goal = await _seed_goal(real_db_session)
    confirmed = [MilestoneDraft(title="기초 문법", summary="")]
    await _sync_milestones(real_db_session, goal_id=goal.id, milestones=confirmed)

    leaf = GoalNode()
    leaf.goal_id = goal.id
    leaf.title = "1주기 leaf"
    leaf.node_type = "leaf"
    leaf.depth = 2
    leaf.order_index = 0
    leaf.is_leaf = True
    leaf.tree_kind = "plan"
    real_db_session.add(leaf)
    await real_db_session.flush()
    await _archive_goal_nodes(real_db_session, goal_id=goal.id)

    # 쓰기 쪽 판정: 이미 있으니 재생성 안 함.
    assert await _sync_milestones(real_db_session, goal_id=goal.id, milestones=confirmed) == []
    # 읽기 쪽 판정: 같은 이유로 "있다" 여야 한다.
    assert [
        m.title for m in await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)
    ] == ["기초 문법"]


async def test_fetch_confirmed_milestones_ignores_other_goals_and_archived_rows(
    real_db_session: AsyncSession,
) -> None:
    """남의 목표 뼈대도, 보관된 옛 마일스톤도 섞여 오지 않는다.

    보관 케이스는 가정이 아니다 — 재조정 HITL(PR-6)이 옛 뼈대를 `archived_at` 으로
    보내면 곧바로 이 경로를 탄다. `_sync_milestones` 의 "이미 있나?" 판정과
    같은 술어를 쓰는지 확인하는 것이기도 하다(둘이 갈라지면 매 주기 뼈대가 쌓인다).

    만다라 오염은 여기서 검증하지 않는다 — DB 가 `ck_goal_nodes_mandala_type` 으로
    `tree_kind='mandala' AND node_type='milestone'` 조합 자체를 막고 있어(depth 별로
    core/subgoal/leaf 만 허용) 그런 행은 애초에 INSERT 되지 않는다. 쿼리의 `tree_kind`
    조건은 그 제약이 아니라 `_sync_milestones` 와 술어를 맞추기 위한 것이다.
    """
    mine = await _seed_goal(real_db_session, title="웹 개발")
    other = await _seed_goal(real_db_session, title="다른 목표")
    await _sync_milestones(
        real_db_session, goal_id=mine.id, milestones=[MilestoneDraft(title="내 뼈대", summary="")]
    )
    await _sync_milestones(
        real_db_session,
        goal_id=other.id,
        milestones=[MilestoneDraft(title="남의 뼈대", summary="")],
    )
    stale = GoalNode()
    stale.goal_id = mine.id
    stale.title = "옛 뼈대"
    stale.node_type = "milestone"
    stale.depth = 1
    stale.order_index = 0  # 활성 행보다 앞 순서 — 정렬만 보면 먼저 나올 자리다
    stale.is_leaf = False
    stale.tree_kind = "plan"
    stale.archived_at = now_kst()
    real_db_session.add(stale)
    await real_db_session.flush()

    saved = await fetch_confirmed_milestones(real_db_session, goal_id=mine.id)
    assert [m.title for m in saved] == ["내 뼈대"]


async def test_fetch_confirmed_milestones_excludes_the_live_plan_tree(
    real_db_session: AsyncSession,
) -> None:
    """**활성 계획 트리와 공존**할 때 마일스톤만 골라낸다 — `node_type` 필터의 유일한 방어선.

    승인 직후의 정상 상태다: 같은 goal 에 마일스톤과 이번 주기의 core/subgoal/leaf 가
    **둘 다 활성**(`tree_kind='plan'` · `archived_at IS NULL`)으로 존재한다. 앞의
    `..._shares_the_predicate_with_persist` 는 leaf 를 만든 뒤 곧바로 보관해 버려서
    `archived_at` 필터가 대신 잡아낸다 — `node_type` 조건을 지워도 초록이었다(뮤테이션
    확인). 그 상태로는 이 필터가 한 번도 실제로 일하지 않는다.

    이게 없으면 Stage A 가 **주차·세션 노드까지 "확정된 마일스톤"이라며** 사용자 확인
    화면에 그대로 띄운다.
    """
    goal = await _seed_goal(real_db_session)
    await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary=""),
            MilestoneDraft(title="배포까지", summary=""),
        ],
    )
    for title, node_type, depth, is_leaf in (
        ("캡스톤 프로젝트", "core", 0, False),
        ("1주차: 입문", "subgoal", 1, False),
        ("조건문 문제 3개", "leaf", 2, True),
    ):
        n = GoalNode()
        n.goal_id = goal.id
        n.title = title
        n.node_type = node_type
        n.depth = depth
        n.order_index = 0
        n.is_leaf = is_leaf
        n.tree_kind = "plan"
        real_db_session.add(n)
    await real_db_session.flush()

    saved = await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)

    assert [m.title for m in saved] == ["기초 문법", "배포까지"]


async def test_sync_milestones_is_idempotent_even_with_a_blank_title(
    real_db_session: AsyncSession,
) -> None:
    """제목이 빈/공백인 항목이 섞여도 재승인이 멱등해야 한다.

    `MilestoneDraft.title` 에 길이 제약이 없고 generate·approve 어느 쪽도 trim 하지 않아,
    사용자가 확인 화면에서 제목 칸을 비우고 확정하면 그대로 들어온다. 정규화 제목이 빈
    행은 대조 딕셔너리에 담기지 않으므로 **매 승인마다 새로 만들어지고 옛것은 보관된다**
    — 승인할 때마다 행이 늘고 node id 와 `completed_at` 이 갈린다.
    """
    goal = await _seed_goal(real_db_session)
    same = [MilestoneDraft(title="   ", summary=""), MilestoneDraft(title="기초 문법", summary="")]

    await _sync_milestones(real_db_session, goal_id=goal.id, milestones=same)
    second = await _sync_milestones(real_db_session, goal_id=goal.id, milestones=same)

    assert second == []
    total = await real_db_session.scalar(
        select(func.count())
        .select_from(GoalNode)
        .where(GoalNode.goal_id == goal.id, GoalNode.node_type == "milestone")
    )
    assert total == 1  # 공백 제목은 뼈대가 아니다 — 아예 안 만든다


async def test_sync_milestones_saves_a_whitespace_only_title_edit(
    real_db_session: AsyncSession,
) -> None:
    """띄어쓰기만 고친 편집도 저장된다 — 대조는 공백을 무시하지만 **표기는 사용자 것**이다.

    같은 행을 재사용하면서 제목을 안 갱신하면 DB 에 옛 표기가 남고, Stage A 가 다음 주기에
    그걸 되읽어 준다 — 이 PR 이 닫겠다고 한 "고친 게 저장되지 않는다" 의 축소판이다.
    """
    goal = await _seed_goal(real_db_session)
    created = await _sync_milestones(
        real_db_session, goal_id=goal.id, milestones=[MilestoneDraft(title="기초문법", summary="")]
    )
    node_id = created[0].id

    await _sync_milestones(
        real_db_session, goal_id=goal.id, milestones=[MilestoneDraft(title="기초 문법", summary="")]
    )

    saved = await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)
    assert [m.title for m in saved] == ["기초 문법"]  # 사용자가 확정한 표기
    rows = (
        (
            await real_db_session.execute(
                select(GoalNode).where(
                    GoalNode.goal_id == goal.id,
                    GoalNode.node_type == "milestone",
                    GoalNode.archived_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert [n.id for n in rows] == [node_id]  # 같은 행 — 재생성 아님


async def test_sync_milestones_does_not_duplicate_a_repeated_title(
    real_db_session: AsyncSession,
) -> None:
    """확정 목록에 같은 제목이 두 번 오면 행을 두 개 만들지 않는다.

    만들어 두면 다음 승인에서 대조 딕셔너리가 **마지막 것만** 담아, 먼저 만들어진 행이
    `completed_at` 을 가진 채 보관된다 — "행을 유지하는 이유는 진척 보존" 이라는 이 층의
    근거를 함수 스스로 무너뜨린다. 정규화가 같으면(`"기초 문법"` vs `"기초문법"`) 같은 것이다.
    """
    goal = await _seed_goal(real_db_session)

    created = await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary=""),
            MilestoneDraft(title="기초문법", summary="같은 것을 두 번 적었다"),
            MilestoneDraft(title="배포까지", summary=""),
        ],
    )

    assert len(created) == 2
    saved = await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)
    assert [m.title for m in saved] == ["기초 문법", "배포까지"]

    # 2주기 — 이번엔 **기존 행에 붙는** 제목이 중복으로 온다. 유지 경로만 막으면 두 번째가
    # 새 행으로 새어 같은 키의 활성 행이 둘이 된다.
    await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary=""),
            MilestoneDraft(title="기초문법", summary="또 적었다"),
        ],
    )
    again = await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)
    assert [m.title for m in again] == ["기초 문법"]


async def test_sync_milestones_creates_rows_outside_the_plan_tree(
    real_db_session: AsyncSession,
) -> None:
    """새 마일스톤 행의 **형상** — `parent_node_id=None` · `depth=1` · `is_leaf=False`.

    셋 다 load-bearing 이다:
    - `parent_node_id=None` — 계획 트리와 부모-자식으로 얽으면 매 주기 그 트리가
      `_archive_goal_nodes` 로 보관될 때 마일스톤이 **같이 끌려간다**. 이 층이 주기를
      넘어 사는 근거가 바로 이 분리다.
    - `depth=1` · `is_leaf=False` — `GET /goals/{id}/nodes` 가 그대로 FE 에 노출한다.
      `is_leaf=True` 면 화면이 마일스톤을 실행 카드로 그린다.

    이 형상은 DB CHECK 제약이 안 막아준다(`ck_goal_nodes_mandala_*` 는 `tree_kind='mandala'`
    에만 걸린다) — 계획 트리 쪽은 이 테스트가 유일한 방어선이다.
    """
    goal = await _seed_goal(real_db_session)
    core = GoalNode()
    core.goal_id = goal.id
    core.title = "캡스톤 프로젝트"
    core.node_type = "core"
    core.depth = 0
    core.order_index = 0
    core.is_leaf = False
    core.tree_kind = "plan"
    real_db_session.add(core)
    await real_db_session.flush()

    # 기존 마일스톤이 **있는** 상태에서 새 것을 만든다 — 1주기(빈 상태)만 보면 "기존 행을
    # 부모로 건다" 류의 오염이 관측되지 않는다.
    await _sync_milestones(
        real_db_session, goal_id=goal.id, milestones=[MilestoneDraft(title="기초 문법", summary="")]
    )
    created = await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초 문법", summary=""),
            MilestoneDraft(title="배포까지", summary=""),
        ],
    )

    assert len(created) == 1
    n = created[0]
    assert n.parent_node_id is None  # core 아래로 걸리면 안 된다
    assert n.depth == 1
    assert n.is_leaf is False
    assert n.tree_kind == "plan"


async def test_sync_milestones_keeps_the_first_row_when_two_have_the_same_key(
    real_db_session: AsyncSession,
) -> None:
    """기존 행 둘의 정규화 제목이 겹치면 **먼저 만들어진 것**(order_index 가 앞)을 잇는다.

    이런 상태는 6a 이전 데이터에 남아 있을 수 있다. 어느 쪽을 잇느냐가 곧 어느 쪽이
    `completed_at` 을 안고 보관되느냐라, 임의로 정해지면 진척이 조용히 사라진다.
    """
    goal = await _seed_goal(real_db_session)
    # 삽입 순서와 order_index 를 **거꾸로** 둔다 — ORDER BY 가 없으면 DB 반환 순서(대개
    # 삽입 순서)를 타서 뒤쪽 행이 이긴다. 이 어긋남이 없으면 정렬 유무를 구별할 수 없다.
    for idx, title in ((1, "기초문법"), (0, "기초 문법")):
        n = GoalNode()
        n.goal_id = goal.id
        n.title = title
        n.node_type = "milestone"
        n.depth = 1
        n.order_index = idx
        n.is_leaf = False
        n.tree_kind = "plan"
        real_db_session.add(n)
    await real_db_session.flush()
    rows = (
        (
            await real_db_session.execute(
                select(GoalNode)
                .where(GoalNode.goal_id == goal.id, GoalNode.node_type == "milestone")
                .order_by(GoalNode.order_index.asc())
            )
        )
        .scalars()
        .all()
    )
    first_id = rows[0].id

    await _sync_milestones(
        real_db_session, goal_id=goal.id, milestones=[MilestoneDraft(title="기초 문법", summary="")]
    )

    survivors = (
        (
            await real_db_session.execute(
                select(GoalNode).where(
                    GoalNode.goal_id == goal.id,
                    GoalNode.node_type == "milestone",
                    GoalNode.archived_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert [n.id for n in survivors] == [first_id]


async def test_cursor_counts_leading_completed_milestones(real_db_session: AsyncSession) -> None:
    """앞에서부터 **연속으로** 완료된 개수 — 이번 주기의 시작점 (ADR-0007 §1).

    이 값이 없으면 Stage A 가 매 주기 같은 뼈대를 돌려주므로(PR-2.5) 분해가 영원히 1번
    마일스톤부터 다시 시작한다.
    """
    goal = await _seed_goal(real_db_session)
    created = await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초", summary=""),
            MilestoneDraft(title="중급", summary=""),
            MilestoneDraft(title="심화", summary=""),
        ],
    )
    assert await completed_milestone_cursor(real_db_session, goal_id=goal.id) == 0

    created[0].completed_at = now_kst()
    await real_db_session.flush()
    assert await completed_milestone_cursor(real_db_session, goal_id=goal.id) == 1

    created[1].completed_at = now_kst()
    await real_db_session.flush()
    assert await completed_milestone_cursor(real_db_session, goal_id=goal.id) == 2


async def test_cursor_stops_at_the_first_incomplete_milestone(
    real_db_session: AsyncSession,
) -> None:
    """중간 것만 먼저 끝냈으면 커서는 0 — 앞의 것이 남아 있으면 거기부터가 맞다."""
    goal = await _seed_goal(real_db_session)
    created = await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[
            MilestoneDraft(title="기초", summary=""),
            MilestoneDraft(title="중급", summary=""),
        ],
    )
    created[1].completed_at = now_kst()  # 2번만 완료
    await real_db_session.flush()

    assert await completed_milestone_cursor(real_db_session, goal_id=goal.id) == 0


async def test_cursor_ignores_archived_milestones(real_db_session: AsyncSession) -> None:
    """보관된 옛 마일스톤은 커서 계산에 안 들어간다 — `fetch_confirmed_milestones` 와
    같은 목록을 봐야 커서가 그 목록의 인덱스로 성립한다."""
    goal = await _seed_goal(real_db_session)
    created = await _sync_milestones(
        real_db_session,
        goal_id=goal.id,
        milestones=[MilestoneDraft(title="기초", summary="")],
    )
    created[0].completed_at = now_kst()
    created[0].archived_at = now_kst()
    await real_db_session.flush()

    assert await completed_milestone_cursor(real_db_session, goal_id=goal.id) == 0


async def test_long_milestone_title_is_clipped_and_stays_idempotent(
    real_db_session: AsyncSession,
) -> None:
    """200자를 넘는 마일스톤 제목도 승인이 INSERT 에서 죽지 않는다 (interview-10 리뷰).

    긴 목표 제목을 룰 폴백이 '{제목} 준비·기초' 로 잇거나 LLM 이 그대로 옮겨 쓰면 VARCHAR(200)
    을 넘는다 — 사용자가 그대로 확정하면 승인이 세 번 재시도 끝에 실패했다. 잘라서 저장하고,
    같은 목록(또는 Stage A 가 되읽은 잘린 제목)을 다시 보내도 새 행을 만들지 않아야 한다.
    """
    goal = await _seed_goal(real_db_session)
    long_title = "다음 달 토익 시험과 중간고사 동아리 발표가 겹쳐서 " * 8 + "준비·기초"
    assert len(long_title) > 200
    same = [MilestoneDraft(title=long_title, summary=""), MilestoneDraft(title="실전", summary="")]

    created = await _sync_milestones(real_db_session, goal_id=goal.id, milestones=same)
    assert len(created) == 2
    assert len(created[0].title) <= 200 and created[0].title.endswith("…")

    assert await _sync_milestones(real_db_session, goal_id=goal.id, milestones=same) == []
    reread = await fetch_confirmed_milestones(real_db_session, goal_id=goal.id)
    assert await _sync_milestones(real_db_session, goal_id=goal.id, milestones=reread) == []
    assert await _plan_node_count(real_db_session, goal_id=goal.id, node_type="milestone") == 2
