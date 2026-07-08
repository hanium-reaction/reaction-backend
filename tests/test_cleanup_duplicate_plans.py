"""scripts.cleanup_duplicate_plans.plan_cleanup — 소급 정리 선택 로직 (DB 무관).

'승인=교체'(PR #113) 를 과거 데이터에 소급: (user, date) 별로 승인 배치(created_at)를
묶어 최신 배치만 남기고 이전 배치의 goal·planned 카드/블록을 보관·취소 대상으로 표시.
실행 이력(status!=planned)과 user_edit 블록을 가진 카드는 보존.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from scripts.cleanup_duplicate_plans import ActionRow, BlockRow, plan_cleanup

UTC = UTC
D = date(2026, 7, 8)
T0 = datetime(2026, 7, 8, 9, 0, tzinfo=UTC)  # 배치 1 (이전)
T1 = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)  # 배치 2 (최신)
USER = UUID("11111111-1111-4111-8111-111111111111")


def _action(
    *,
    created_at: datetime,
    status: str = "planned",
    source: str = "goal",
    target: date = D,
    archived: bool = False,
    user: UUID = USER,
    title: str = "카드",
) -> ActionRow:
    return ActionRow(
        id=uuid4(),
        user_id=user,
        target_date=target,
        source=source,
        status=status,
        title=title,
        created_at=created_at,
        archived_at=created_at if archived else None,
    )


def _block(action: ActionRow, *, status: str = "scheduled", source: str = "ai_plan") -> BlockRow:
    return BlockRow(id=uuid4(), action_item_id=action.id, source=source, block_status=status)


def test_keeps_latest_batch_archives_earlier() -> None:
    old1, old2 = _action(created_at=T0), _action(created_at=T0)
    new = _action(created_at=T1)
    blocks = [_block(old1), _block(old2), _block(new)]

    plan = plan_cleanup(
        [
            old1,
            old2,
            new,
        ],
        blocks,
    )

    assert set(plan.archive_action_ids) == {old1.id, old2.id}
    assert new.id not in plan.archive_action_ids
    # 이전 배치 카드의 scheduled 블록만 취소 대상.
    cancelled = set(plan.cancel_block_ids)
    assert {b.id for b in blocks if b.action_item_id in {old1.id, old2.id}} == cancelled
    assert plan.groups[0].batch_count == 2
    assert plan.groups[0].kept_batch_at == T1


def test_single_batch_is_left_untouched() -> None:
    a, b = _action(created_at=T0), _action(created_at=T0)
    plan = plan_cleanup([a, b], [_block(a), _block(b)])
    assert plan.archive_action_ids == []
    assert plan.groups == []


def test_preserves_started_and_finished_history() -> None:
    """이전 배치라도 시작/완료 카드(실행 이력)는 보관하지 않는다."""
    started = _action(created_at=T0, status="in_progress")
    done = _action(created_at=T0, status="done")
    planned_old = _action(created_at=T0)
    new = _action(created_at=T1)

    plan = plan_cleanup([started, done, planned_old, new], [])

    assert plan.archive_action_ids == [planned_old.id]  # planned 만
    assert started.id not in plan.archive_action_ids
    assert done.id not in plan.archive_action_ids


def test_preserves_user_edited_block_cards() -> None:
    """이전 배치라도 user_edit 블록을 가진 카드는 통째로 보존 (fix 와 동일)."""
    moved_old = _action(created_at=T0)
    plain_old = _action(created_at=T0)
    new = _action(created_at=T1)
    blocks = [
        _block(moved_old, source="user_edit"),
        _block(plain_old),
        _block(new),
    ]

    plan = plan_cleanup([moved_old, plain_old, new], blocks)

    assert plan.archive_action_ids == [plain_old.id]
    assert moved_old.id not in plan.archive_action_ids


def test_only_cancels_scheduled_blocks() -> None:
    """이전 배치 카드라도 이미 finished/cancelled 인 블록은 재변경하지 않는다."""
    old = _action(created_at=T0)
    new = _action(created_at=T1)
    sched = _block(old, status="scheduled")
    fin = _block(old, status="finished")
    canc = _block(old, status="cancelled")

    plan = plan_cleanup([old, new], [sched, fin, canc, _block(new)])

    assert plan.archive_action_ids == [old.id]
    assert plan.cancel_block_ids == [sched.id]  # scheduled 만


def test_groups_are_per_user_and_date() -> None:
    """배치 그룹화는 (user, date) 단위 — 다른 날짜/사용자는 서로 섞이지 않는다."""
    other_user = UUID("22222222-2222-4222-8222-222222222222")
    other_day = D + timedelta(days=1)
    # 사용자 A, 날짜 D: 2배치 → 정리 대상
    a_old, a_new = _action(created_at=T0), _action(created_at=T1)
    # 사용자 A, 다른 날짜: 각 1배치 → 무시
    a_other = _action(created_at=T0, target=other_day)
    # 사용자 B, 날짜 D: 1배치 → 무시
    b_one = _action(created_at=T0, user=other_user)

    plan = plan_cleanup([a_old, a_new, a_other, b_one], [])

    assert plan.archive_action_ids == [a_old.id]
    assert plan.touched_dates == 1
