"""회고 누적 창 만료 cron (#20) — 3일 초과 미회고 카드 자동 만료 검증.

job 함수에 FakeExecutionRepo 주입 — 룰만(LLM/DB 무관), idempotent 보장 확인.
`_actions` 는 FakeActionItemRepo 와 공유(conftest fixture) — 실 코드와 같은 교차 변경 경로.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.scheduler.expire_reflections import (
    pending_reflection_since,
    run_expire_unreflected_cards,
)
from reaction_backend.schemas.common import KST
from tests.conftest import DEMO_USER_UUID, FakeExecutionRepo, _FakeSession

# 결정적 테스트 — now_kst() 대신 고정 시각을 주입한다. 04:00 KST = cron 등록 시각.
NOW = datetime(2026, 7, 16, 4, 0, tzinfo=KST)
TODAY = NOW.date()
SINCE = pending_reflection_since(TODAY)  # 2026-07-14 00:00 KST


def _add_block(
    repo: FakeExecutionRepo, action: ActionItem, *, start_at: datetime, block_status: str
) -> ScheduledBlock:
    block = ScheduledBlock()
    block.id = uuid4()
    block.user_id = DEMO_USER_UUID
    block.action_item_id = action.id
    block.start_at = start_at
    block.end_at = start_at + timedelta(minutes=30)
    block.block_status = block_status
    block.source = "ai_plan"
    repo._blocks[block.id] = block
    return block


def _seed_card(
    repo: FakeExecutionRepo,
    *,
    plan_start_at: datetime,
    actual_start_at: datetime | None = None,
    completion_status: str = "in_progress",
    status: str = "in_progress",
    archived_at: datetime | None = None,
    system_failure_reason: str | None = None,
    block_status: str = "started",
) -> tuple[ActionItem, ExecutionEvent]:
    """카드 1장 + 그 카드의 첫 블록 + 실행을 시드. `actual_start_at` 기본값은 계획 시각."""
    action = ActionItem()
    action.id = uuid4()
    action.user_id = DEMO_USER_UUID
    action.title = "테스트 카드"
    action.target_date = plan_start_at.date()
    action.status = status
    action.source = "goal"
    action.category = "study"
    action.priority = 3
    action.estimated_minutes = 30
    action.archived_at = archived_at
    action.system_failure_reason = system_failure_reason
    repo._actions[action.id] = action

    block = _add_block(repo, action, start_at=plan_start_at, block_status=block_status)

    execution = ExecutionEvent()
    execution.id = uuid4()
    execution.user_id = DEMO_USER_UUID
    execution.action_item_id = action.id
    execution.scheduled_block_id = block.id
    execution.plan_start_at = plan_start_at
    execution.plan_end_at = plan_start_at + timedelta(minutes=30)
    execution.actual_start_at = actual_start_at if actual_start_at is not None else plan_start_at
    execution.completion_status = completion_status
    repo._executions[execution.id] = execution

    return action, execution


def _at(days_ago: int, hour: int = 14) -> datetime:
    return datetime.combine(
        TODAY - timedelta(days=days_ago), datetime.min.time(), tzinfo=KST
    ).replace(hour=hour)


async def _run(repo: FakeExecutionRepo) -> int:
    return await run_expire_unreflected_cards(_FakeSession(), now=NOW, repo=repo)


async def test_expires_only_outside_window(fake_execution_repo: FakeExecutionRepo) -> None:
    """오늘/어제/그제 3건은 보존, 그끄제(창 밖) 1건만 만료 — 회고 기회 정확히 3회 보장."""
    today_card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(0))
    yesterday_card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(1))
    two_days_card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(2))
    three_days_card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(3))

    count = await _run(fake_execution_repo)

    assert count == 1
    assert three_days_card.archived_at == NOW
    for survivor in (today_card, yesterday_card, two_days_card):
        assert survivor.archived_at is None
        assert survivor.system_failure_reason is None


async def test_boundary_exactly_since(fake_execution_repo: FakeExecutionRepo) -> None:
    """창 경계 정각(그제 00:00)은 **보존** — pending 이 `>=` 이므로 아직 회고 가능하다.

    `<` 를 `<=` 로 잘못 쓰면 회고 가능한 카드를 지우는 과잉 삭제가 된다. 이 테스트가 방어선.
    """
    on_boundary, _ = _seed_card(fake_execution_repo, plan_start_at=SINCE)
    just_before, _ = _seed_card(fake_execution_repo, plan_start_at=SINCE - timedelta(minutes=1))

    count = await _run(fake_execution_repo)

    assert count == 1
    assert on_boundary.archived_at is None
    assert just_before.archived_at == NOW


async def test_pending_and_expiry_are_disjoint(fake_execution_repo: FakeExecutionRepo) -> None:
    """만료 대상과 /reflection/pending 노출 집합의 교집합은 공집합 (정확한 여집합 계약).

    이게 깨지면 회고 목록에 '(삭제된 카드)' 유령이 뜬다 (routes/reflection.py 참조).
    """
    for days_ago in range(6):
        _seed_card(fake_execution_repo, plan_start_at=_at(days_ago))
    visible_before = {
        e.action_item_id
        for e in await fake_execution_repo.list_pending_reflection(DEMO_USER_UUID, since=SINCE)
    }

    await _run(fake_execution_repo)

    expired: set[UUID] = {
        a.id for a in fake_execution_repo._actions.values() if a.archived_at is not None
    }
    assert expired & visible_before == set()
    # 창 안 카드는 만료 후에도 그대로 노출된다.
    visible_after = {
        e.action_item_id
        for e in await fake_execution_repo.list_pending_reflection(DEMO_USER_UUID, since=SINCE)
    }
    assert visible_after == visible_before


async def test_expire_is_idempotent(fake_execution_repo: FakeExecutionRepo) -> None:
    """다회 실행해도 안전 — 2회차는 0건.

    구동 조건(execution.completion_status='in_progress')이 만료 후에도 그대로 남으므로,
    `archived_at IS NULL` 가드가 멱등성의 **유일한** 방어선이다 (PlanDraftRepo.expire_stale 과
    달리 멱등이 공짜가 아니다). AGENTS.md §2.
    """
    card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(3))

    first = await _run(fake_execution_repo)
    archived_at_after_first = card.archived_at
    second = await _run(fake_execution_repo)

    assert first == 1
    assert second == 0
    assert card.archived_at == archived_at_after_first  # 만료 시각이 밀리지 않는다


async def test_does_not_touch_completion_status_or_status(
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """만료가 execution.completion_status 와 action.status 를 건드리지 않는다.

    ⚠️ 이 assert 가 KPI 오염의 **유일한 자동 방어선**이다. review_repo.collect_execution_stats
    는 ActionItem 을 join 하면서 archived 필터가 없어, 만료 카드의 실행이 주간 집계 쿼리에
    그대로 유입된다. 이를 걸러주는 건 weekly_review._TERMINAL_STATUSES 의 in_progress 제외뿐 —
    누군가 여기서 'failed' 로 종결시키는 순간 adherence·resilience 가 조용히 오염된다.
    action.status 불변은 AGENTS.md §2 (Resilience 지표 전제).
    """
    card, execution = _seed_card(fake_execution_repo, plan_start_at=_at(3))

    await _run(fake_execution_repo)

    assert execution.completion_status == "in_progress"
    assert card.status == "in_progress"


async def test_skips_already_archived(fake_execution_repo: FakeExecutionRepo) -> None:
    """이미 보관된 카드(승인=교체 supersede 등)는 건드리지 않는다 — 원 보관 시각 보존."""
    archived_earlier = NOW - timedelta(days=1)
    card, _ = _seed_card(
        fake_execution_repo, plan_start_at=_at(3), status="planned", archived_at=archived_earlier
    )

    count = await _run(fake_execution_repo)

    assert count == 0
    assert card.archived_at == archived_earlier
    assert card.system_failure_reason is None


async def test_skips_existing_failure_reason(fake_execution_repo: FakeExecutionRepo) -> None:
    """다른 system_failure_reason 이 이미 있으면 덮어쓰지 않는다 — 최초 사유 보존."""
    card, _ = _seed_card(
        fake_execution_repo, plan_start_at=_at(3), system_failure_reason="cancelled_by_replan"
    )

    count = await _run(fake_execution_repo)

    assert count == 0
    assert card.system_failure_reason == "cancelled_by_replan"
    assert card.archived_at is None


async def test_terminal_executions_untouched(fake_execution_repo: FakeExecutionRepo) -> None:
    """이미 체크인이 끝난(done/failed) 실행의 카드는 창 밖이어도 만료 X — 회고를 마쳤으므로."""
    done_card, _ = _seed_card(
        fake_execution_repo, plan_start_at=_at(5), completion_status="done", status="done"
    )
    failed_card, _ = _seed_card(
        fake_execution_repo, plan_start_at=_at(5), completion_status="failed", status="failed"
    )

    count = await _run(fake_execution_repo)

    assert count == 0
    assert done_card.archived_at is None
    assert failed_card.archived_at is None


async def test_planned_card_without_execution_untouched(
    fake_execution_repo: FakeExecutionRepo, fake_action_item_repo: object
) -> None:
    """시작조차 안 한(execution 없는) 오래된 planned 카드는 만료 X.

    만료 대상은 '회고 창을 벗어난 카드'지 '오래된 카드'가 아니다. 실행이 없으면 회고 의무가
    발생한 적도 없고, '3일'을 셀 기준(plan_start_at)도 없다. 무엇보다 AGENTS.md §1 은
    "Parked 자유"(보류 카드는 기간 무제한)를 잠금 결정으로 두고 있어, 오래된 planned 카드를
    쓸어버리면 그 결정을 코드로 우회하게 된다.
    """
    orphan = ActionItem()
    orphan.id = uuid4()
    orphan.user_id = DEMO_USER_UUID
    orphan.title = "묵혀둔 카드"
    orphan.target_date = _at(10).date()
    orphan.status = "planned"
    orphan.source = "manual"
    orphan.category = "other"
    orphan.priority = 3
    orphan.estimated_minutes = 30
    orphan.archived_at = None
    orphan.system_failure_reason = None
    fake_execution_repo._actions[orphan.id] = orphan

    count = await _run(fake_execution_repo)

    assert count == 0
    assert orphan.archived_at is None
    assert orphan.system_failure_reason is None


async def test_expire_marks_reason_and_timestamp(fake_execution_repo: FakeExecutionRepo) -> None:
    """만료 표식 = system_failure_reason + archived_at(주입한 now)."""
    card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(3))

    await _run(fake_execution_repo)

    assert card.system_failure_reason == "reflection_skipped"
    assert card.archived_at == NOW


async def test_cancels_orphan_blocks(fake_execution_repo: FakeExecutionRepo) -> None:
    """만료 카드의 미종결 블록은 cancelled — 카드만 지우면 주간 그리드에 유령 블록이 남는다.

    list_week 는 archived 를 안 보고 block_status != 'cancelled' 만 본다.
    """
    expired_card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(3))
    kept_card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(1))

    await _run(fake_execution_repo)

    def _block_of(action: ActionItem) -> ScheduledBlock:
        return next(
            b for b in fake_execution_repo._blocks.values() if b.action_item_id == action.id
        )

    assert _block_of(expired_card).block_status == "cancelled"
    assert _block_of(kept_card).block_status == "started"


async def test_finished_block_not_cancelled(fake_execution_repo: FakeExecutionRepo) -> None:
    """이미 종결된(finished) 블록은 취소하지 않는다 — 실제 수행 이력이라 왜곡 금지.

    카드는 만료되지만(실행이 in_progress) 블록은 건드리지 않는다. 미종결의 정의는
    `find_open_block` 과 동일하게 scheduled/started 뿐.
    """
    card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(4), block_status="finished")

    count = await _run(fake_execution_repo)

    assert count == 1
    assert card.archived_at == NOW
    block = next(b for b in fake_execution_repo._blocks.values() if b.action_item_id == card.id)
    assert block.block_status == "finished"


async def test_cancels_scheduled_block_too(fake_execution_repo: FakeExecutionRepo) -> None:
    """미착수(scheduled) 상태로 남은 과거 블록도 취소된다 — started 만 취소하면 유령이 남는다.

    카드 1장이 여러 세션 블록을 가지면 `find_open_block` 이 가장 이른 것만 started 로 바꾸므로,
    같은 카드의 나머지 과거 세션은 'scheduled' 로 남아 있다.
    """
    card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(4))
    sibling = _add_block(fake_execution_repo, card, start_at=_at(3), block_status="scheduled")

    count = await _run(fake_execution_repo)

    assert count == 1
    assert sibling.block_status == "cancelled"


async def test_keeps_card_with_future_session_block(
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """창 안/이후에 미종결 세션 블록이 남은 카드는 만료 X — 아직 진행 중인 계획이다.

    카드 1장은 여러 날짜의 세션 블록을 가질 수 있다(ScheduledBlock docstring: '2일에 걸쳐 분할').
    첫 세션만 하고 체크인을 잊었다고 아직 오지 않은 세션까지 취소하면, 사용자가 하려던 계획이
    조용히 사라진다(list_week·list_busy_between 이 cancelled 를 제외하므로 그 슬롯이 덮인다).
    """
    card, _ = _seed_card(fake_execution_repo, plan_start_at=_at(4))
    future_session = _add_block(
        fake_execution_repo, card, start_at=_at(-1), block_status="scheduled"
    )

    count = await _run(fake_execution_repo)

    assert count == 0
    assert card.archived_at is None
    assert card.system_failure_reason is None
    assert future_session.block_status == "scheduled"


async def test_keeps_card_started_late_against_past_block(
    fake_execution_repo: FakeExecutionRepo,
) -> None:
    """계획은 과거지만 **방금 착수한** 카드는 만료 X — 계획 시각만 보면 어제 시작한 걸 오늘 지운다.

    `find_open_block` 에 날짜 필터가 없어 지난 블록을 뒤늦게 [▶시작] 할 수 있다. 그러면
    plan_start_at 은 과거인데 actual_start_at 은 방금이 된다. 두 시각 중 나중을 기준으로 한다.
    """
    card, _ = _seed_card(
        fake_execution_repo,
        plan_start_at=_at(6),  # 계획은 6일 전 = 창 밖
        actual_start_at=_at(0, hour=3),  # 실제 착수는 오늘 새벽 = 창 안
    )

    count = await _run(fake_execution_repo)

    assert count == 0
    assert card.archived_at is None
