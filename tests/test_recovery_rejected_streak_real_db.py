"""`recovery_rejected_streak` 는 **카드 행이 아니라 회복 결정 횟수**를 센다 (#479) — 실 Postgres.

한 세트의 카드는 한 번의 결정으로 함께 닫힌다. 「나중에」는 2~4장을 전부 `skipped` 로,
수락은 고른 카드 + 형제들을 `rejected` 로 쓴다. 예전엔 그 행을 하나씩 결정으로 세서:

    신규 사용자 · 첫 실패 · 첫 회복 → 2장 세트에 「나중에」 **한 번**
    → recovery_rejected_streak=2 → L3 goal_renegotiation

실측(2026-09-16, main 0c6168d, 로컬 API + 브라우저): 서버 로그
`L3 goal_renegotiation … same_goal_failures=0 rejected_streak=2`, 화면은 "같은 방식으로는 잘
안 풀렸어요" 와 컴백 프리픽스를 띄웠다. 사용자는 한 번 미뤘을 뿐이다.

⚠️ **순수 함수 테스트로는 이 결함이 원리적으로 안 잡힌다.** `tests/test_escalation.py` 는 결정
문자열 리스트를 직접 만든다 — "한 세트 → 카드 여러 행 → 결정 한 번" 이라는 실제 저장 형태를
안 지난다. 그래서 여기서는 **실제 라우트 함수**(`generate_recovery_proposals`/`decide_recovery`)
를 실 `RecoveryRepo` + Postgres 로 돌려 카드 행을 진짜로 만들고, 그 이력으로 streak 를 잰다.
fake repo 는 같은 의미를 흉내 낼 뿐이라 SQL 의 동점 정렬도 못 본다.

라우트는 `session.commit()` 을 부르는데, `real_db_session` 은 바깥 트랜잭션을 롤백해 격리하므로
commit 을 부르면 안 된다(conftest 픽스처 docstring). 같은 트랜잭션 안에서는 flush 만으로 이후
SELECT 에 보이므로 이 파일에서만 commit 을 flush 로 바꿔 끼운다. LLM 은 stub 이다 — 이 테스트의
관심사는 문구가 아니라 결정 이력이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.api.routes.recovery import decide_recovery, generate_recovery_proposals
from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.recovery_attempt import RecoveryAttempt
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.models.user import User
from reaction_backend.orchestrator.escalation import (
    L3_REJECTED_STREAK_THRESHOLD,
    compute_escalation_state,
)
from reaction_backend.repositories.action_item_repo import ActionItemRepo
from reaction_backend.repositories.recovery_repo import RecoveryRepo
from reaction_backend.schemas.common import KST, now_kst
from reaction_backend.schemas.recovery import (
    RecoveryDecisionRequest,
    RecoveryGenerateRequest,
    RecoveryProposalLLM,
    RecoveryProposalsResponse,
)
from tests.conftest import DB_AVAILABLE

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="DATABASE_URL not set")

# 마이그레이션이 시드한 실 카탈로그에서 세트 크기를 정하는 태그 조합 — 그룹당 1장이라 매칭된
# 그룹 수가 곧 카드 수다(2장 미만이면 패딩으로 2장).
_TWO_CARDS = ["TIME_SHORTAGE"]  # RESCHEDULE + 패딩 DOWNSCOPE
_THREE_CARDS = ["TIME_SHORTAGE", "PRIORITY_SHIFT"]  # RESCHEDULE · CARRY_OVER · PARK
_FOUR_CARDS = ["AMBIGUITY", "TIME_SHORTAGE", "PRIORITY_SHIFT"]  # + DOWNSCOPE


@pytest.fixture
def db(real_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    """라우트가 부르는 commit 을 flush 로 — 바깥 트랜잭션 롤백 격리를 지킨다(모듈 docstring)."""
    monkeypatch.setattr(real_db_session, "commit", real_db_session.flush)

    from reaction_backend.llm import RunResult, aiClient

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        return RunResult(
            value=RecoveryProposalLLM(
                strategy_code="x", if_clause="", then_clause="다듬은 문구", rationale=""
            ),
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="2",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)
    return real_db_session


async def _seed_user(session: AsyncSession) -> User:
    user = User(id=uuid4(), email=f"{uuid4()}@test.local", name="streak 테스트")
    session.add(user)
    await session.flush()
    return user


async def _seed_failed_execution(session: AsyncSession, user: User, tags: list[str]) -> str:
    """실패 실행 1건 — 매번 **새 카드**라 L1(동일 카드 연속 실패)·L3(동일 goal)가 끼지 않는다."""
    action = ActionItem(
        id=uuid4(),
        user_id=user.id,
        title="streak 테스트 카드",
        target_date=now_kst().date(),
        estimated_minutes=30,
        category="study",
    )
    session.add(action)
    await session.flush()
    start = now_kst()
    block = ScheduledBlock(
        id=uuid4(),
        user_id=user.id,
        action_item_id=action.id,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )
    session.add(block)
    await session.flush()
    execution = ExecutionEvent(
        id=uuid4(),
        user_id=user.id,
        action_item_id=action.id,
        scheduled_block_id=block.id,
        plan_start_at=start,
        plan_end_at=start + timedelta(minutes=30),
        completion_status="failed",
    )
    session.add(execution)
    await session.flush()
    for tag in tags:
        session.add(ExecutionFailureTag(id=uuid4(), execution_id=execution.id, tag_code=tag))
    await session.flush()
    return f"exec_{execution.id}"


async def _generate(session: AsyncSession, user: User, exec_id: str) -> RecoveryProposalsResponse:
    return await generate_recovery_proposals(
        RecoveryGenerateRequest(execution_id=exec_id),
        user,
        RecoveryRepo(session),
        ActionItemRepo(session),
        session,
    )


async def _decide(session: AsyncSession, user: User, **body: Any) -> None:
    await decide_recovery(
        RecoveryDecisionRequest(**body),
        user,
        RecoveryRepo(session),
        ActionItemRepo(session),
        session,
    )


async def _streak(session: AsyncSession, user_id: UUID) -> int:
    decisions = await RecoveryRepo(session).list_recovery_decisions(user_id)
    state = compute_escalation_state(
        same_card_outcomes_most_recent_first=[],
        same_tag_outcomes_most_recent_first=[],
        same_goal_outcomes_most_recent_first=[],
        recovery_decisions_most_recent_first=decisions,
        recovery_results_most_recent_first=[],
    )
    return state.counters.recovery_rejected_streak


# ── Case 1 — 한 번 skip ─────────────────────────────────────────────────────


async def test_one_skip_of_a_two_card_set_counts_once_and_does_not_reach_l3(
    db: AsyncSession,
) -> None:
    user = await _seed_user(db)
    exec_id = await _seed_failed_execution(db, user, _TWO_CARDS)

    first = await _generate(db, user, exec_id)
    assert len(first.cards) == 2
    assert first.recovery_mode == "standard"

    await _decide(db, user, execution_id=exec_id, decision="skipped")

    assert await _streak(db, user.id) == 1, "「나중에」 한 번이 카드 수만큼 세졌다"
    again = await _generate(db, user, exec_id)
    assert again.recovery_mode == "standard", "첫 「나중에」 만으로 L3 재협상에 들어갔다"


# ── Case 2 — 두 번 skip ─────────────────────────────────────────────────────


async def test_two_skips_on_regenerated_sets_of_the_same_execution_reach_l3(
    db: AsyncSession,
) -> None:
    """미룬 뒤 같은 실행에서 새 세트를 받아 또 미룬다 — 실제 두 번이므로 L3 가 맞다."""
    user = await _seed_user(db)
    exec_id = await _seed_failed_execution(db, user, _TWO_CARDS)

    await _generate(db, user, exec_id)
    await _decide(db, user, execution_id=exec_id, decision="skipped")
    await _generate(db, user, exec_id)
    await _decide(db, user, execution_id=exec_id, decision="skipped")

    assert await _streak(db, user.id) == L3_REJECTED_STREAK_THRESHOLD
    assert (await _generate(db, user, exec_id)).recovery_mode == "goal_renegotiation"


async def test_two_skips_on_different_executions_reach_l3(db: AsyncSession) -> None:
    """사용자 전체 이력 스코프는 그대로다 — 다른 카드의 회복을 미룬 것도 연속으로 센다."""
    user = await _seed_user(db)
    for _ in range(L3_REJECTED_STREAK_THRESHOLD):
        exec_id = await _seed_failed_execution(db, user, _TWO_CARDS)
        await _generate(db, user, exec_id)
        await _decide(db, user, execution_id=exec_id, decision="skipped")

    assert await _streak(db, user.id) == L3_REJECTED_STREAK_THRESHOLD
    current = await _seed_failed_execution(db, user, _TWO_CARDS)
    assert (await _generate(db, user, current)).recovery_mode == "goal_renegotiation"


# ── Case 3 — accepted + 형제 rejected ───────────────────────────────────────


async def test_accepting_one_of_three_resets_the_streak(db: AsyncSession) -> None:
    """수락 한 번이 형제 2장의 자동 `rejected` 때문에 거절로 읽히면 안 된다.

    앞에 「나중에」 1회를 깔아 둔다 — 형제 rejected 가 먼저 읽히면 예전 방식으로는
    rejected 2 + skipped 2 로 L3 문턱을 넘는 자리다.
    """
    user = await _seed_user(db)
    earlier = await _seed_failed_execution(db, user, _TWO_CARDS)
    await _generate(db, user, earlier)
    await _decide(db, user, execution_id=earlier, decision="skipped")

    exec_id = await _seed_failed_execution(db, user, _THREE_CARDS)
    proposals = await _generate(db, user, exec_id)
    assert len(proposals.cards) == 3
    await _decide(
        db,
        user,
        execution_id=exec_id,
        decision="accepted",
        accepted_attempt_id=proposals.cards[0].attempt_id,
    )

    assert await RecoveryRepo(db).list_recovery_decisions(user.id) == ["accepted", "skipped"]
    assert await _streak(db, user.id) == 0


# ── Case 4 — edited + 형제 rejected ─────────────────────────────────────────


async def test_edited_acceptance_with_siblings_resets_the_streak(db: AsyncSession) -> None:
    user = await _seed_user(db)
    earlier = await _seed_failed_execution(db, user, _TWO_CARDS)
    await _generate(db, user, earlier)
    await _decide(db, user, execution_id=earlier, decision="skipped")

    exec_id = await _seed_failed_execution(db, user, _FOUR_CARDS)
    proposals = await _generate(db, user, exec_id)
    assert len(proposals.cards) == 4
    # 문구 편집은 새 카드를 만드는 그룹(DOWNSCOPE/CARRY_OVER)만 받는다.
    editable = next(c for c in proposals.cards if c.option_group in ("DOWNSCOPE", "CARRY_OVER"))
    await _decide(
        db,
        user,
        execution_id=exec_id,
        decision="edited",
        accepted_attempt_id=editable.attempt_id,
        edited_action_text="내가 고친 한 걸음",
    )

    assert await RecoveryRepo(db).list_recovery_decisions(user.id) == ["edited", "skipped"]
    assert await _streak(db, user.id) == 0


# ── Case 5 — 카드 개수 불변성 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tags", "expected_cards"),
    [(_TWO_CARDS, 2), (_THREE_CARDS, 3), (_FOUR_CARDS, 4)],
)
async def test_one_skip_counts_once_whatever_the_set_size(
    db: AsyncSession, tags: list[str], expected_cards: int
) -> None:
    user = await _seed_user(db)
    exec_id = await _seed_failed_execution(db, user, tags)
    proposals = await _generate(db, user, exec_id)
    assert len(proposals.cards) == expected_cards

    await _decide(db, user, execution_id=exec_id, decision="skipped")

    assert await _streak(db, user.id) == 1


# ── DB 반환 순서 무관 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("accepted_position", [0, 1, 2, 3])
async def test_event_representative_does_not_depend_on_row_order(
    real_db_session: AsyncSession, accepted_position: int
) -> None:
    """같은 `decided_at` 을 공유하는 행의 삽입 순서를 바꿔도 결과가 같다.

    동점은 Postgres 가 순서를 보장하지 않는다 — 수락 카드가 몇 번째로 들어가든 결정 1회의
    대표값은 `accepted` 여야 한다. 라우트를 거치지 않고 행을 직접 넣어 순서를 통제한다.
    """
    session = real_db_session
    user = await _seed_user(session)
    decided = datetime(2026, 9, 16, 21, 0, tzinfo=KST)

    skipped_exec = UUID((await _seed_failed_execution(session, user, [])).removeprefix("exec_"))
    for _ in range(2):
        session.add(_row(user.id, skipped_exec, "skipped", decided - timedelta(hours=1)))

    accepted_exec = UUID((await _seed_failed_execution(session, user, [])).removeprefix("exec_"))
    decisions = ["rejected"] * 3
    decisions.insert(accepted_position, "accepted")
    for decision in decisions:
        session.add(_row(user.id, accepted_exec, decision, decided))
        await session.flush()  # 한 행씩 넣어 힙 순서를 삽입 순서대로 만든다

    assert await RecoveryRepo(session).list_recovery_decisions(user.id) == [
        "accepted",
        "skipped",
    ]


async def test_expiry_cron_closes_each_execution_as_its_own_decision(
    real_db_session: AsyncSession,
) -> None:
    """만료 cron 은 여러 실행을 **한 시각**으로 닫는다 — 실행마다 따로 방치된 결정이다.

    `decided_at` 하나로만 묶으면 두 실행이 결정 1회로 접힌다. 한 실행의 카드들은 1회다.
    """
    session = real_db_session
    user = await _seed_user(session)
    old = datetime(2026, 1, 1, 9, 0, tzinfo=KST)
    for _ in range(2):
        exec_uuid = UUID((await _seed_failed_execution(session, user, [])).removeprefix("exec_"))
        for _ in range(3):
            row = _row(user.id, exec_uuid, "pending", None)
            row.created_at = old
            session.add(row)
    await session.flush()

    closed = await RecoveryRepo(session).expire_undecided(
        before=old + timedelta(days=1), decided_at=now_kst()
    )

    assert closed >= 6
    assert await RecoveryRepo(session).list_recovery_decisions(user.id) == ["rejected", "rejected"]


def _row(
    user_id: UUID, execution_id: UUID, decision: str, decided_at: datetime | None
) -> RecoveryAttempt:
    return RecoveryAttempt(
        id=uuid4(),
        user_id=user_id,
        execution_id=execution_id,
        recovery_option_group="DOWNSCOPE",
        recovery_strategy_type="NANO_STEP",
        user_decision=decision,
        recovery_decided_at=decided_at,
    )
