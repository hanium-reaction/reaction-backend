"""first_plan_adapter.materialize_goals (#96) — 인터뷰 완료/계획 승인 공유 목표 영속.

핵심: 이미 있는 제목의 목표는 재사용(중복 생성 방지), placeholder(#88)는 제외.
인터뷰 완료가 먼저 목표를 저장하고 계획 승인이 같은 목표를 재사용하는 계약을 보증한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from reaction_backend.db.models.goal import Goal
from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.orchestrator.first_plan_adapter import (
    _derive_goal_category,
    materialize_goals,
    supersede_proposed_goals,
)
from reaction_backend.orchestrator.interview_adapter import PLACEHOLDER_GOAL_TITLE
from reaction_backend.schemas.interview import GoalCandidate, normalize_deadline


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """execute → entity 별로 미리 넣은 행 반환, add/flush 기록.

    `_active_goals`(W3, `1ee508b967ba`)가 `Goal` 조회 뒤 `_mandala_owned_goal_ids`
    로 `GoalNode` 도 조회하므로, entity 를 구분 않고 항상 같은 목록을 돌려주면 Goal
    자리에 GoalNode 조회가 Goal 행을 받아(또는 반대) `.tree_kind` 등에서 깨진다
    (`test_plan_approve_replace.py`의 `_EntitySession`과 같은 이유).
    """

    def __init__(
        self, existing: list[Goal] | None = None, *, nodes: list[GoalNode] | None = None
    ) -> None:
        self._by_entity: dict[Any, list[Any]] = {
            Goal: existing or [],
            GoalNode: nodes or [],
        }
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0]["entity"]
        return _Result(self._by_entity.get(entity, []))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _goal(title: str, *, heaviest: bool = False, tier: str = "maintain") -> GoalCandidate:
    return GoalCandidate(
        title=title,
        category="other",
        is_heaviest=heaviest,
        tentative_tier=tier,
        confidence=0.5,
    )


def _placeholder() -> GoalCandidate:
    return GoalCandidate(
        title=PLACEHOLDER_GOAL_TITLE, category="other", tentative_tier="maintain", confidence=0.0
    )


def _mandala_node(goal_id: Any) -> GoalNode:
    n = GoalNode()
    n.id = uuid4()
    n.goal_id = goal_id
    n.title = "만다라 core"
    n.tree_kind = "mandala"
    n.archived_at = None
    return n


async def test_creates_new_goals_and_picks_heaviest() -> None:
    sess = _FakeSession()
    goals = [_goal("캡스톤", heaviest=True, tier="focus"), _goal("토익")]
    rows, heaviest = await materialize_goals(sess, user_id=uuid4(), core_goals=goals)  # type: ignore[arg-type]

    assert len(rows) == 2
    assert len(sess.added) == 2  # 둘 다 신규 생성
    assert heaviest is not None and heaviest.title == "캡스톤"
    assert heaviest.goal_tier == "focus"


async def test_reuses_existing_goal_by_title() -> None:
    uid = uuid4()
    existing = Goal()
    existing.id = uuid4()
    existing.user_id = uid
    existing.title = "캡스톤"
    existing.goal_tier = "focus"
    existing.archived_at = None
    sess = _FakeSession(existing=[existing])

    goals = [_goal("캡스톤", heaviest=True, tier="focus"), _goal("토익")]
    rows, heaviest = await materialize_goals(sess, user_id=uid, core_goals=goals)  # type: ignore[arg-type]

    # 캡스톤은 재사용(신규 add X) → 토익만 새로 생성
    assert len(sess.added) == 1
    assert sess.added[0].title == "토익"
    assert heaviest is existing  # 기존 행을 heaviest 로
    assert len(rows) == 2


async def test_placeholder_only_yields_no_goals() -> None:
    sess = _FakeSession()
    rows, heaviest = await materialize_goals(
        sess,
        user_id=uuid4(),
        core_goals=[_placeholder()],  # type: ignore[arg-type]
    )
    assert rows == []
    assert heaviest is None
    assert sess.added == []


# ───────────────────── _derive_goal_category (순수) ─────────────────────
# 인터뷰가 목표 카테고리를 분류하지 않아 'other' 로 저장되던 것을,
# 분해된 액션 카테고리 다수결로 파생한다 (블록/목표가 전부 '기타' 로 뜨던 문제).


def test_derive_goal_category_majority() -> None:
    assert _derive_goal_category(["study", "study", "health"]) == "study"


def test_derive_goal_category_ignores_other() -> None:
    # 'other' 는 표에서 제외 — 실카테고리 소수라도 그것을 채택.
    assert _derive_goal_category(["other", "other", "study"]) == "study"


def test_derive_goal_category_all_other_returns_none() -> None:
    assert _derive_goal_category(["other", "other"]) is None
    assert _derive_goal_category([]) is None


# ───────────────────── 잠정(proposed) 상태 라이프사이클 ─────────────────────
# 인터뷰만 마쳐도 목표가 곧바로 active 로 저장돼, 계획을 승인하지 않고 나간 목표가
# 진짜 목표와 구분 없이 쌓였다(실측: 67개 중 43개가 계획 없는 active).


async def test_interview_creates_proposed_not_active() -> None:
    """기본값은 잠정 — 인터뷰 완료만으로는 '진짜 목표' 가 아니다."""
    sess = _FakeSession()
    rows, _ = await materialize_goals(
        sess,  # type: ignore[arg-type]
        user_id=uuid4(),
        core_goals=[_goal("캡스톤", heaviest=True)],  # type: ignore[arg-type]
    )
    assert [g.status for g in rows] == ["proposed"]


async def test_approve_promotes_proposed_to_active() -> None:
    """계획 승인은 '이 목표를 실제로 하겠다' 는 결정 → 잠정 목표를 승격한다."""
    existing = Goal()
    existing.id = uuid4()
    existing.title = "캡스톤"
    existing.status = "proposed"
    existing.archived_at = None
    sess = _FakeSession([existing])

    rows, _ = await materialize_goals(
        sess,  # type: ignore[arg-type]
        user_id=uuid4(),
        core_goals=[_goal("캡스톤", heaviest=True)],  # type: ignore[arg-type]
        status="active",
    )
    assert rows[0] is existing  # 재사용(중복 생성 X)
    assert existing.status == "active"  # 승격
    assert sess.added == []


async def test_approve_does_not_demote_or_touch_real_goals() -> None:
    """이미 active/completed 인 목표는 건드리지 않는다 — 승격은 한 방향이다."""
    done = Goal()
    done.id = uuid4()
    done.title = "토익"
    done.status = "completed"
    done.archived_at = None
    sess = _FakeSession([done])

    await materialize_goals(
        sess,  # type: ignore[arg-type]
        user_id=uuid4(),
        core_goals=[_goal("토익")],  # type: ignore[arg-type]
        status="active",
    )
    assert done.status == "completed"


async def test_supersede_archives_only_stale_proposed() -> None:
    """새 인터뷰가 이전 잠정 목표를 대체 — active/completed 는 살려둔다.

    세션은 이미 restart-wins 로 이전 세션을 abandoned 처리한다. 목표에도 같은 규칙을 적용해
    계획으로 이어지지 않은 잠정 목표가 계속 쌓이지 않게 한다.
    """
    keep = Goal()
    keep.id = uuid4()
    keep.title = "이번에도 나온 목표"
    keep.status = "proposed"
    keep.archived_at = None

    stale = Goal()
    stale.id = uuid4()
    stale.title = "지난 인터뷰 잔재"
    stale.status = "proposed"
    stale.archived_at = None

    real = Goal()
    real.id = uuid4()
    real.title = "승인했던 목표"
    real.status = "active"
    real.archived_at = None

    sess = _FakeSession([keep, stale, real])
    n = await supersede_proposed_goals(
        sess,  # type: ignore[arg-type]
        user_id=uuid4(),
        keep=[keep],
        onboarding_state="ONBOARDING_INTERVIEW",  # 온보딩 중 — restart-wins 가 맞다
    )
    assert n == 1
    assert stale.status == "archived" and stale.archived_at is not None  # 보관(soft)
    assert keep.status == "proposed" and keep.archived_at is None  # 이번에 살린 것
    assert real.status == "active" and real.archived_at is None  # 진짜 목표는 불변


async def test_supersede_keeps_unplanned_goals_after_onboarding() -> None:
    """⚠️ **온보딩을 마친 사용자의 재인터뷰는 미계획 목표를 지우지 않는다.**

    실측(브라우저 재현): 목표 관리에서 "다시 인터뷰하기" 로 들어가 한 문항만 답하고
    [충분해요] 로 끝냈는데, 이전 `proposed` 목표가 보관돼 화면에서 사라졌다.

        전:  proposed | 내년 1월 중순까지 교환학생 파견 확정 받기   ← "미계획" 배지
        후:  archived | 내년 1월 중순까지 교환학생 파견 확정 받기   ← 사라짐

    온보딩 중에는 restart-wins 가 맞다(인터뷰를 여러 번 시도하며 나온 잔재를 정리).
    그러나 앱을 쓰다 하는 재인터뷰에서 남은 `proposed` 는 **사용자가 나중에 계획하려고
    남겨둔 미계획 목표**다. 재인터뷰 시트도 "이미 만들어진 목표와 일정은 그대로 남아요"
    라고 약속한다.
    """
    stale = Goal()
    stale.id = uuid4()
    stale.title = "내년 1월 중순까지 교환학생 파견 확정 받기"
    stale.status = "proposed"
    stale.archived_at = None

    sess = _FakeSession([stale])
    n = await supersede_proposed_goals(
        sess,  # type: ignore[arg-type]
        user_id=uuid4(),
        keep=[],  # 이번 인터뷰에서 이 목표를 다시 말하지 않았다
        onboarding_state="ACTIVE",
    )

    assert n == 0
    assert stale.status == "proposed" and stale.archived_at is None


@pytest.mark.parametrize(
    "state",
    [
        "WELCOME",
        "ONBOARDING_INTERVIEW",
        "ONBOARDING_CONFIRM",
        "ONBOARDING_CALENDAR",
        "ONBOARDING_MANUAL_SCHEDULE",
        "ONBOARDING_POLICIES",
        "ONBOARDING_FIRST_PLAN",
        "ONBOARDING_NOTIFICATIONS",
    ],
)
async def test_supersede_still_cleans_up_during_onboarding(state: str) -> None:
    """온보딩 **전 단계**에서는 그대로 정리한다 — 인터뷰 재시도의 잔재가 쌓이면 안 된다."""
    stale = Goal()
    stale.id = uuid4()
    stale.title = "재시도 중 나온 잔재"
    stale.status = "proposed"
    stale.archived_at = None

    sess = _FakeSession([stale])
    n = await supersede_proposed_goals(
        sess,  # type: ignore[arg-type]
        user_id=uuid4(),
        keep=[],
        onboarding_state=state,
    )

    assert n == 1
    assert stale.status == "archived"


def test_both_interview_completion_paths_pass_the_onboarding_state() -> None:
    """호출부 **둘 다** 사용자 상태를 넘기는가.

    인터뷰가 끝나는 경로는 둘이다(`submit_answer` 의 `result.done`, `finish_session`).
    한쪽만 고치면 다른 쪽으로 끝낸 사용자만 목표를 잃는다 — 재현하기 어려운 버그가 된다.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent / "src/reaction_backend/api/routes/interview.py"
    ).read_text(encoding="utf-8")
    # `supersede_proposed_goals(` 문자열은 주석(#186 함정 설명)에도 나온다 — **실제로 넘기는
    # 인자**를 센다. 호출 경로가 둘이므로 둘 다여야 한다.
    assert src.count("onboarding_state=user.onboarding_state") == 2


# ───────────────────── 만다라 오염 격리 (W3, `1ee508b967ba`) ─────────────────────
# 궁극목표(§3.2) 제목이 계획 인터뷰 core_goals 제목과 우연히 겹치면, 그 goal 이 heaviest 로
# 오인돼 계획 승인 한 번에 만다라 73칸이 통째로 archived 될 뻔했다(W1/W2 가 막는 사고의
# 성립 조건 자체를 여기서 끊는다) — `_active_goals` 가 만다라 트리를 소유한 goal 을 제외.


async def test_materialize_ignores_mandala_owned_goal_title() -> None:
    """만다라 트리를 가진 목표는 제목이 같아도 재사용 대상에서 빠져 새 목표가 생긴다."""
    uid = uuid4()
    mandala_owner = Goal()
    mandala_owner.id = uuid4()
    mandala_owner.user_id = uid
    mandala_owner.title = "캡스톤"
    mandala_owner.status = "active"
    mandala_owner.archived_at = None
    node = _mandala_node(mandala_owner.id)

    sess = _FakeSession([mandala_owner], nodes=[node])
    rows, heaviest = await materialize_goals(
        sess,  # type: ignore[arg-type]
        user_id=uid,
        core_goals=[_goal("캡스톤", heaviest=True)],  # type: ignore[arg-type]
    )

    # 만다라 소유 goal 은 후보에서 빠지므로 재사용되지 않고 신규 생성된다.
    assert len(sess.added) == 1
    assert sess.added[0].title == "캡스톤"
    assert heaviest is sess.added[0]
    assert mandala_owner not in rows


async def test_supersede_proposed_goals_ignores_mandala_owned_goal() -> None:
    """만다라 트리를 가진 목표는 `proposed` 여도 계획 잠정-정리 대상에서 제외된다."""
    uid = uuid4()
    mandala_owner = Goal()
    mandala_owner.id = uuid4()
    mandala_owner.user_id = uid
    mandala_owner.title = "궁극목표"
    mandala_owner.status = "proposed"
    mandala_owner.archived_at = None
    node = _mandala_node(mandala_owner.id)

    sess = _FakeSession([mandala_owner], nodes=[node])
    n = await supersede_proposed_goals(
        sess,  # type: ignore[arg-type]
        user_id=uid,
        keep=[],
        onboarding_state="ONBOARDING_INTERVIEW",  # 온보딩 중이어야 정리 로직이 돈다
    )

    assert n == 0
    assert mandala_owner.status == "proposed" and mandala_owner.archived_at is None


async def test_month_only_deadline_does_not_crash_the_interview() -> None:
    """월만 말한 마감(`2026-10-00`)이 인터뷰 마지막 턴을 500 으로 죽이던 회귀 (라이브 8/29).

    `goals.deadlines` 는 date_picker 지만 사용자가 답하지 않아도 `goals.list` 자유서술
    하베스트가 이 슬롯을 채운다. "10월에 시험이에요" → LLM 이 `2026-10-00` 을 냈고,
    인터뷰 **마지막 턴**의 `materialize_goals` 가 `date.fromisoformat` 에서
    `ValueError: day is out of range for month` 로 터졌다 — 500 이 나고 세션이
    `end_reason=None` 으로 남아 15턴짜리 인터뷰가 통째로 날아갔다.

    경계(`GoalCandidate.deadline`)에서 그 달 1일로 정규화한다 — 늦게 잡는 것보다 이르게
    잡는 쪽이 안전하다.
    """
    sess = _FakeSession()
    goal = GoalCandidate(
        title="정보처리기사 실기 합격",
        category="study",
        is_heaviest=True,
        deadline="2026-10-00",
        tentative_tier="focus",
        confidence=0.5,
    )
    assert goal.deadline == "2026-10-01"  # 경계에서 이미 정규화됐다

    rows, heaviest = await materialize_goals(sess, user_id=uuid4(), core_goals=[goal])  # type: ignore[arg-type]

    assert heaviest is not None
    assert heaviest.deadline == date(2026, 10, 1)
    assert len(rows) == 1


def test_normalize_deadline_reads_only_real_dates() -> None:
    """읽히는 건 그대로, 월만 있으면 1일, 아예 못 읽으면 마감 없음."""
    assert normalize_deadline("2026-10-13") == "2026-10-13"
    assert normalize_deadline("2026-10-00") == "2026-10-01"
    assert normalize_deadline("2026-10") == "2026-10-01"
    assert normalize_deadline(" 2026-10-13 ") == "2026-10-13"
    # 지어내지 않는다 — 마감 없는 목표도 정상 경로다.
    assert normalize_deadline("2026-13-01") is None
    assert normalize_deadline("2026-02-30") is None
    assert normalize_deadline("내년 봄쯤") is None
    assert normalize_deadline("") is None
    assert normalize_deadline(None) is None
