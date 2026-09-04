"""`goal_nodes.source` 는 **누가 그 칸을 채웠는지**를 말한다 — 이제 사실을 말한다 (#454).

컬럼은 처음부터 `llm | rule | user` 를 뜻했고 CHECK 제약도 그렇다. 그런데 계획 트리에서
이 값이 **맞은 적이 한 번도 없었다:**

- 마이그레이션 `1ee508b967ba` 가 기존 행을 전량 `llm` 로 백필했다 — 룰 패딩까지 함께.
- 그 뒤 행은 `_apply_once` 가 세팅을 안 해 서버 기본값 `user` 로 떨어졌다.

실측(로컬 DB): 자리표시자 카드 77장의 노드가 llm 68 · user 9. **둘 다 거짓이다.**

## 왜 라벨 문제가 아닌가

승인 시점에 초안 node_id(`tmp-continue-N`)가 실제 UUID 로 바뀌고, **초안 id 를 보존하는
컬럼이 없다.** 여기서 안 남기면 DB 에 들어간 뒤로는 자리표시자를 식별할 방법이 아예 없다.
재계획이 그 내용을 채우려면(#454 방향 1) 이 컬럼이 유일한 단서다.
"""

from __future__ import annotations

import pathlib
import re
from datetime import date
from typing import Any
from uuid import UUID

from reaction_backend.db.models.goal_node import GoalNode
from reaction_backend.orchestrator import first_plan_adapter as FPA
from reaction_backend.schemas.common import now_kst
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
_CONT = FPA.CONTINUATION_NODE_PREFIX
_FALL = FPA.FALLBACK_NODE_PREFIX


# ── 순수 판정 ───────────────────────────────────────────────────────────────


def test_rule_produced_nodes_are_marked_rule() -> None:
    """두 룰 경로 — 마감 보충(확장)과 분해 실패 폴백 — 이 모두 'rule' 이다."""
    assert FPA.node_source_for(f"{_CONT}-0") == "rule"
    assert FPA.node_source_for(_CONT) == "rule"  # '이어가기' 브랜치도 룰이 만든다
    assert FPA.node_source_for(f"{_FALL}-3") == "rule"


def test_llm_authored_nodes_are_marked_llm() -> None:
    """분해 LLM 의 node_id 는 자유 형식이다 — 실측 6,959장에서 `tmp-` 로 시작한 건 없었다."""
    for nid in ("leaf-1", "branch_2", "node-lN", "g_l1_2", "root", "l-1-1", "leaf_mock_3"):
        assert FPA.node_source_for(nid) == "llm", nid


def test_unknown_node_id_defaults_to_llm_not_user() -> None:
    """⚠️ 모르면 'llm' 이다. 'user' 로 떨어뜨리면 **사용자가 쓴 것처럼** 보인다 —
    지금까지 나던 거짓말이 정확히 그것이다(서버 기본값 `user`)."""
    assert FPA.node_source_for(None) == "llm"
    assert FPA.node_source_for("") == "llm"


# ── 승인 경로가 실제로 남기는가 ─────────────────────────────────────────────


class _CaptureSession:
    """`session.add()` 된 객체만 모으는 최소 fake — 승인 경로가 무엇을 쓰는지 본다."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        entity = stmt.column_descriptions[0]["entity"]

        class _R:
            def __init__(self, rows: list[Any]) -> None:
                self._rows = rows

            def scalars(self) -> Any:
                return self

            def all(self) -> list[Any]:
                return list(self._rows)

            def first(self) -> Any:
                return self._rows[0] if self._rows else None

        return _R([o for o in self.added if isinstance(o, entity)])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _outcome() -> InterviewOutcome:
    return InterviewOutcome(
        session_id="iv_source",
        generated_at=now_kst(),
        end_reason="completed",
        ambiguity_final=0.1,
        analysis_source="llm",
        identity=IdentityContext(role="대3", season="학기중"),
        core_goals=[
            GoalCandidate(
                title="정보처리기사 실기 합격",
                category="study",
                is_heaviest=True,
                tentative_tier="focus",
                confidence=0.9,
            )
        ],
        availability=AvailabilityProfile(
            activity_window=TimeRange(start="09:00", end="23:00"), peak_window=["오전"]
        ),
        preferences=PreferenceProfile(recovery_tone="담백", rest_ok=True),
        unresolved_slots=[],
        horizon=None,
    )


async def test_apply_persists_source_per_node() -> None:
    """⚠️ **이 파일의 핵심.** 승인이 노드마다 출처를 남기는지 — 남기지 않으면 영영 못 남긴다.

    아래에서 초안 node_id 가 실제 UUID 로 바뀌므로, 이 시점을 놓치면 복구 경로가 없다.
    """
    nodes = [
        GoalNodeDraft(
            node_id="root",
            parent_id=None,
            title="정보처리기사 실기 합격",
            node_type="root",
            order_index=0,
            is_leaf=False,
        ),
        GoalNodeDraft(
            node_id="leaf-1",
            parent_id="root",
            title="1년차 1회차 기출 문제 풀이",
            node_type="leaf",
            order_index=0,
            is_leaf=True,
        ),
        GoalNodeDraft(
            node_id=_CONT,
            parent_id="root",
            title="정보처리기사 실기 합격 이어가기",
            node_type="branch",
            order_index=1,
            is_leaf=False,
        ),
        GoalNodeDraft(
            node_id=f"{_CONT}-0",
            parent_id=_CONT,
            title="정보처리기사 실기 합격 21회차",
            node_type="leaf",
            order_index=0,
            is_leaf=True,
        ),
    ]
    actions = [
        ActionItemDraft(
            node_id="leaf-1",
            title="1년차 1회차 기출 문제 풀이",
            estimated_minutes=120,
            category="study",
            first_step="시험지 PDF를 열고 1번 문제부터 연필 들고 풀기 시작하기",
        ),
        ActionItemDraft(
            node_id=f"{_CONT}-0",
            title="정보처리기사 실기 합격 21회차",
            estimated_minutes=120,
            category="study",
            first_step=FPA.CONTINUATION_FIRST_STEP,
        ),
    ]
    blocks: list[ScheduledBlockPreview] = []

    sess = _CaptureSession()
    await FPA.db_apply_first_plan(
        sess,  # type: ignore[arg-type]
        user_id=UID,
        target_date=TARGET,
        outcome=_outcome(),
        goal_nodes=nodes,
        action_items=actions,
        blocks=blocks,
        time_policies=[],
    )

    by_title = {n.title: n.source for n in sess.added if isinstance(n, GoalNode)}
    assert by_title["1년차 1회차 기출 문제 풀이"] == "llm"
    assert by_title["정보처리기사 실기 합격 21회차"] == "rule"
    assert by_title["정보처리기사 실기 합격 이어가기"] == "rule"
    # 서버 기본값 'user' 로 새는 노드가 하나도 없어야 한다 — 그게 지금까지의 거짓말이다.
    assert "user" not in by_title.values(), by_title


# ── 생성부·마이그레이션 드리프트 ────────────────────────────────────────────


def test_generators_use_the_shared_constants() -> None:
    """생성부가 상수를 안 쓰면 판정이 조용히 빗나간다 — 아무 테스트도 안 깨지면서."""
    src = pathlib.Path(FPA.__file__).read_text(encoding="utf-8")
    assert "branch_id = CONTINUATION_NODE_PREFIX" in src
    assert 'leaf_id = f"{CONTINUATION_NODE_PREFIX}-{i}"' in src
    assert "first_step=CONTINUATION_FIRST_STEP," in src

    fp = pathlib.Path(FPA.__file__).with_name("first_plan.py").read_text(encoding="utf-8")
    assert 'leaf_id = f"{first_plan_adapter.FALLBACK_NODE_PREFIX}-{i}"' in fp
    assert "first_step=first_plan_adapter.FALLBACK_FIRST_STEP," in fp


def test_backfill_migration_matches_the_code_constants() -> None:
    """⚠️ 마이그레이션은 **문자열로** 과거 행을 찾는다 — 코드가 바뀌면 조용히 빗나간다.

    이미 적용된 마이그레이션은 다시 안 돌지만, 목록이 틀린 채 남으면 다음 사람이 그걸
    복사해 새 백필을 쓴다. 여기서 대조해 둔다.
    """
    mig = next(
        p
        for p in (pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions").iterdir()
        if "goal_nodes_source" in p.name
    )
    text = mig.read_text(encoding="utf-8")
    listed = set(re.findall(r'"([^"]*5분만 시작하기)"', text))
    assert listed == {FPA.CONTINUATION_FIRST_STEP, FPA.FALLBACK_FIRST_STEP}, listed
