"""회복 카드의 순서는 **고정돼야 한다** — 추천 배지가 그 순서에 달려 있다.

FE 는 목록의 **첫 카드**에 "추천" 배지를 붙인다(`RecoveryScreen.tsx` — `recommended={i === 0}`).
그런데 `list_attempts` 의 정렬이 `ORDER BY created_at` 하나였고, 한 세트는 같은 트랜잭션에서
만들어져 `created_at` 이 마이크로초까지 같다. 동점이면 Postgres 는 순서를 보장하지 않는다.

실측(2026-09-05, 촬영 리허설 중):

    RESCHEDULE_DEFAULT @ 2026-09-05 21:25:58.352381
    DOWNSCOPE_DEFAULT  @ 2026-09-05 21:25:58.352381

같은 회복 화면을 다시 열었더니 두 카드의 순서가 뒤바뀌고 **추천이 옮겨 갔다.** 새 정보 없이
새로고침만으로 판단이 뒤집힌 것이다.

⚠️ **fake session 으로는 이 결함이 안 잡힌다.** 파이썬 리스트는 삽입 순서를 지키므로 항상
같은 답이 나온다. 순서를 정하는 건 DB 라서 real_db 여야 한다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.schemas.common import now_kst
from tests.conftest import DB_AVAILABLE

# ⚠️ `pytest.mark.anyio` 를 쓰면 안 된다 — pyproject 의 `asyncio_mode = "auto"` 가
# 이미 async 테스트를 몰고, anyio 마커를 얹으면 테스트 본문만 anyio 의 새 루프에서
# 돌아 `real_db_session` 이 만든 asyncpg 커넥션과 루프가 갈린다
# ("attached to a different loop"). 다른 real_db 파일들과 같은 관행을 쓴다.
pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")


async def _seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """사용자 + 실패 실행 하나. 회복 카드를 매달 자리다."""
    user_id = uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local", name="정렬 테스트"))
    await session.flush()

    card = ActionItem()
    card.id = uuid.uuid4()
    card.user_id = user_id
    card.title = "정렬 테스트 카드"
    card.target_date = now_kst().date()
    card.estimated_minutes = 30
    card.status = "failed"
    card.source = "goal"
    card.category = "study"
    card.priority = 3
    session.add(card)
    await session.flush()

    block = ScheduledBlock()
    block.id = uuid.uuid4()
    block.user_id = user_id
    block.action_item_id = card.id
    block.start_at = now_kst()
    block.end_at = now_kst()
    block.block_status = "finished"
    block.source = "ai_plan"
    session.add(block)
    await session.flush()

    execution = ExecutionEvent()
    execution.id = uuid.uuid4()
    execution.user_id = user_id
    execution.action_item_id = card.id
    execution.scheduled_block_id = block.id
    execution.completion_status = "failed"
    execution.plan_start_at = now_kst()
    execution.plan_end_at = now_kst()
    session.add(execution)
    await session.flush()
    return user_id, execution.id


def _attempt(
    user_id: uuid.UUID,
    execution_id: uuid.UUID,
    *,
    strategy: str,
    group: str,
    trigger_tag: str | None,
) -> RecoveryAttempt:
    a = RecoveryAttempt()
    a.id = uuid.uuid4()
    a.user_id = user_id
    a.execution_id = execution_id
    a.recovery_strategy_type = strategy
    a.recovery_option_group = group
    a.trigger_tag = trigger_tag
    a.suggested_action_text = strategy
    return a


async def test_tag_matched_card_comes_first(real_db_session: AsyncSession) -> None:
    """⚠️ **추천은 '왜 실패했는지에 맞는 카드'여야 한다.**

    `trigger_tag` 가 있다는 건 실패 태그에 매칭됐다는 뜻이고, 그게
    `select_strategies` 의 1순위다. 매칭 없이 패딩된 카드는 `trigger_tag` 가 비어 있다.

    ⚠️ **카탈로그 우선순위가 반대인 조합을 일부러 고른다.**

        CARRYOVER_DEFAULT = 70  (태그 매칭)
        NANO_STEP         = 10  (매칭 없이 패딩)

    우선순위만 보면 `NANO_STEP` 이 앞이다. 태그 키가 빠지면 그 답이 나오므로, 이 조합이라야
    "태그 우선" 규칙이 실제로 살아 있는지 검사된다. 삽입도 매칭 안 된 것부터 한다.
    """
    user_id, execution_id = await _seed(real_db_session)
    real_db_session.add(
        _attempt(user_id, execution_id, strategy="NANO_STEP", group="DOWNSCOPE", trigger_tag=None)
    )
    real_db_session.add(
        _attempt(
            user_id,
            execution_id,
            strategy="CARRYOVER_DEFAULT",
            group="CARRY_OVER",
            trigger_tag="PLAN_TOO_BIG",
        )
    )
    await real_db_session.flush()

    got = await RecoveryRepo(real_db_session).list_attempts(user_id, execution_id)

    assert [a.recovery_strategy_type for a in got] == [
        "CARRYOVER_DEFAULT",  # 태그 매칭 → 우선순위가 나빠도 추천 자리
        "NANO_STEP",
    ]


async def test_catalog_priority_breaks_the_tie(real_db_session: AsyncSession) -> None:
    """태그 매칭이 다 없으면 **카탈로그 우선순위**가 순서를 정한다.

    `select_strategies` 의 동점 처리와 같은 기준이다(`display_priority` 오름차순).
    ⚠️ 카탈로그 우선순위의 **역순으로 넣는다** — 삽입 순서를 따르면 실패하도록.

        NANO_STEP = 10   ·   RESCHEDULE_DEFAULT = 50   ·   CARRYOVER_DEFAULT = 70
    """
    user_id, execution_id = await _seed(real_db_session)
    for strategy, group in (
        ("CARRYOVER_DEFAULT", "CARRY_OVER"),
        ("RESCHEDULE_DEFAULT", "RESCHEDULE"),
        ("NANO_STEP", "DOWNSCOPE"),
    ):
        real_db_session.add(
            _attempt(user_id, execution_id, strategy=strategy, group=group, trigger_tag=None)
        )
    await real_db_session.flush()

    got = await RecoveryRepo(real_db_session).list_attempts(user_id, execution_id)

    assert [a.recovery_strategy_type for a in got] == [
        "NANO_STEP",
        "RESCHEDULE_DEFAULT",
        "CARRYOVER_DEFAULT",
    ]


async def test_order_is_stable_across_repeated_reads(real_db_session: AsyncSession) -> None:
    """같은 요청을 여러 번 해도 순서가 같다.

    ⚠️ **이 테스트만으로는 결함을 못 잡는다.** 예전 동작은 "틀린 순서"가 아니라
    **미정의**였고, 미정의는 결정적으로 재현되지 않는다(작은 테이블에서는 우연히 매번
    같은 답이 나온다 — 실제로 옛 코드에서도 이 테스트는 초록이었다).

    결함을 잡는 건 위 두 테스트다(`test_tag_matched_card_comes_first`,
    `test_catalog_priority_breaks_the_tie`) — 둘 다 **삽입 순서와 다른 답**을 요구하므로
    정렬 키가 빠지면 반드시 빨개진다. 이 테스트는 계약을 문서로 남기는 역할이다.
    """
    user_id, execution_id = await _seed(real_db_session)
    for strategy, group, tag in (
        ("NANO_STEP", "DOWNSCOPE", None),
        ("RESCHEDULE_DEFAULT", "RESCHEDULE", None),
        ("PARK_DEFAULT", "PARK", None),
    ):
        real_db_session.add(
            _attempt(user_id, execution_id, strategy=strategy, group=group, trigger_tag=tag)
        )
    await real_db_session.flush()

    repo = RecoveryRepo(real_db_session)
    runs = [
        [a.recovery_strategy_type for a in await repo.list_attempts(user_id, execution_id)]
        for _ in range(5)
    ]

    assert len(runs[0]) == 3
    assert all(r == runs[0] for r in runs), f"읽을 때마다 순서가 달라진다: {runs}"


async def test_same_created_at_still_has_one_answer(real_db_session: AsyncSession) -> None:
    """⚠️ `created_at` 동점이 이 결함의 원인이었다 — 정말 동점인지 확인하고 간다.

    이 단언이 깨지면(예: 생성부가 타임스탬프를 하나씩 어긋나게 넣게 바뀌면) 위 테스트들이
    **결함을 못 잡는 상태로 초록**이 된다. 전제를 테스트가 직접 들고 있게 한다.
    """
    user_id, execution_id = await _seed(real_db_session)
    for strategy, group in (("NANO_STEP", "DOWNSCOPE"), ("PARK_DEFAULT", "PARK")):
        real_db_session.add(
            _attempt(user_id, execution_id, strategy=strategy, group=group, trigger_tag=None)
        )
    await real_db_session.flush()

    got = await RecoveryRepo(real_db_session).list_attempts(user_id, execution_id)
    assert got[0].created_at == got[1].created_at, "동점이 아니면 이 테스트들의 전제가 깨진 것"
