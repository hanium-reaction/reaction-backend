"""④층 검토기는 '이어가기' 확장 구간을 **보지 않는다** (#436).

`extend_action_plan_to_horizon` 은 분해가 지평 예산에 못 미치면 마감까지 자리표시자로
채운다 — 제목은 `{목표} 21회차` 로 번호만 다르고, first_step 은 여덟 장이 글자까지 같은
"지난 회차에서 이어서 5분만 시작하기" 다. **규칙이 의도적으로 그렇게 만든다**(루브릭 §D1
"반려 금지"). 지금 내용을 지어내면 사용자가 정하지 않은 걸 정해 버리기 때문이다.

## 왜 프롬프트가 아니라 입력에서 빼는가

루브릭 §1.2:

> "프롬프트에 '검사하지 마라'가 아니라 **변수 자체를 넘기지 않는** 방식으로 강제한다
>  — 변수가 있으면 지시를 어긴다(실측 다수)."

면제 조항 자체는 `plan_quality_eval.v4.md` 에 이미 글자 그대로 있지만 그건 오프라인 평가용
프롬프트이고, 프로덕션은 `planning/plan_quality` → v3 을 부른다(그 조항 0회).

## 근거 — M33 3-arm 실측

반려 9건이 **전부** 확장 구간을 가진 계획에서 나왔고, 확장이 없는 40케이스 120회에서는
0건이었다. 그중 8건은 피드백이 이 구간을 명시적으로 지목했다. 검토기는 정체를 알아보고도
("이어하기로 추가된 …") 결함이라 말한다 — 혼동이 아니라 계약 충돌이다.

⚠️ 그 표는 **인과로 읽지 않는다** — challenge 층은 2⁴ 요인설계라 '확장 보유' 와
'빈도=hard' 가 같은 분할이다. 이 파일이 고정하는 것은 통계가 아니라 **배선**이다.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from reaction_backend.orchestrator import first_plan, first_plan_adapter
from reaction_backend.schemas.planning import ActionItemDraft, GoalDecomposition, GoalNodeDraft

_PREFIX = first_plan_adapter.CONTINUATION_NODE_PREFIX


def _plan_with_extension() -> GoalDecomposition:
    """LLM 원안 2장 + 규칙 확장 3장. 실제 M33 산출물의 모양 그대로다.

    ⚠️ 원안 제목에도 "회차" 가 들어간다 — 목표가 기출문제면 LLM 이 자연스럽게 그렇게 쓴다.
    제목으로 거르면 이 카드가 함께 사라지므로, 판별은 **node_id 접두사**로 한다.
    """
    return GoalDecomposition(
        goal_nodes=[
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
                node_id="leaf-2",
                parent_id="root",
                title="SQL 실전 응용 문제 풀이",
                node_type="leaf",
                order_index=1,
                is_leaf=True,
            ),
            GoalNodeDraft(
                node_id=_PREFIX,
                parent_id="root",
                title="정보처리기사 실기 합격 이어가기",
                node_type="branch",
                order_index=2,
                is_leaf=False,
            ),
            *[
                GoalNodeDraft(
                    node_id=f"{_PREFIX}-{i}",
                    parent_id=_PREFIX,
                    title=f"정보처리기사 실기 합격 {21 + i}회차",
                    node_type="leaf",
                    order_index=i,
                    is_leaf=True,
                )
                for i in range(3)
            ],
        ],
        action_items=[
            ActionItemDraft(
                node_id="leaf-1",
                title="1년차 1회차 기출 문제 풀이",
                estimated_minutes=120,
                category="study",
                first_step="시험지 PDF를 열고 1번 문제부터 연필 들고 풀기 시작하기",
            ),
            ActionItemDraft(
                node_id="leaf-2",
                title="SQL 실전 응용 문제 풀이",
                estimated_minutes=90,
                category="study",
                first_step="기출에 나온 SQL 빈출 구문 모음집 열기",
            ),
            *[
                ActionItemDraft(
                    node_id=f"{_PREFIX}-{i}",
                    title=f"정보처리기사 실기 합격 {21 + i}회차",
                    estimated_minutes=120,
                    category="study",
                    first_step="지난 회차에서 이어서 5분만 시작하기",
                )
                for i in range(3)
            ],
        ],
        policy_violations=[],
    )


def _state(gp: GoalDecomposition | None) -> Any:
    return {
        "user_id": uuid4(),
        "outcome": None,
        "target_date": "2026-07-30",
        "scope": "horizon",
        "density": "standard",
        "milestones": None,
        "planning_context": {},
        "goal_plan": gp,
        "schedule_warnings": [],
    }


def test_reviewer_never_sees_continuation_cards() -> None:
    """확장 구간의 카드도 노드도 검토기 입력에 없다."""
    v = first_plan._review_variables(_state(_plan_with_extension()))
    items = json.loads(v["action_items_json"])
    nodes = json.loads(v["goal_nodes_json"])

    assert [i["node_id"] for i in items] == ["leaf-1", "leaf-2"]
    assert [n["node_id"] for n in nodes] == ["root", "leaf-1", "leaf-2"]
    # 자리표시자의 지문이 어느 변수에도 남으면 안 된다 — 제목만 지우고 first_step 을
    # 남기면 검토기가 여전히 "첫 단계가 추상적" 이라 지적할 수 있다.
    blob = v["action_items_json"] + v["goal_nodes_json"]
    assert _PREFIX not in blob
    assert "지난 회차에서 이어서 5분만 시작하기" not in blob
    assert "21회차" not in blob


def test_llm_authored_cards_survive_even_when_their_titles_say_hoecha() -> None:
    r"""⚠️ 제목이 아니라 node_id 로 거른다 — 목표가 기출문제면 LLM 도 "회차" 를 쓴다.

    제목 정규식(`\d+회차`)으로 걸렀다면 이 카드가 함께 사라졌을 것이다. 실측에서 그
    정규식은 challenge 층 145건 중 81건이 오탐이었다(과대 2.3배).
    """
    v = first_plan._review_variables(_state(_plan_with_extension()))
    items = json.loads(v["action_items_json"])

    assert any(i["title"] == "1년차 1회차 기출 문제 풀이" for i in items), (
        "LLM 이 쓴 '1회차' 카드가 함께 걸러졌다 — 제목으로 거르고 있다"
    )
    assert items[0]["first_step"] == "시험지 PDF를 열고 1번 문제부터 연필 들고 풀기 시작하기"


def test_plan_without_extension_is_passed_through_untouched() -> None:
    """확장이 없으면 아무것도 달라지지 않는다 — 필터가 정상 계획을 갉아먹지 않는다."""
    gp = _plan_with_extension()
    plain = GoalDecomposition(
        goal_nodes=[n for n in gp.goal_nodes if not n.node_id.startswith(_PREFIX)],
        action_items=[a for a in gp.action_items if not a.node_id.startswith(_PREFIX)],
        policy_violations=[],
    )
    v = first_plan._review_variables(_state(plain))
    assert json.loads(v["action_items_json"]) == [a.model_dump() for a in plain.action_items]
    assert json.loads(v["goal_nodes_json"]) == [n.model_dump() for n in plain.goal_nodes]


def test_decomposition_failure_still_reaches_the_reviewer() -> None:
    """⚠️ 확장이 계획 **전부**를 채웠으면 검토기는 빈 계획을 봐야 한다.

    그건 확장의 문제가 아니라 **분해 실패**다. 여기서 조용히 통과시키면 아무 내용 없는
    계획이 승인돼 나간다. 반려되는 것이 맞다.
    """
    only_ext = GoalDecomposition(
        goal_nodes=[
            GoalNodeDraft(
                node_id=f"{_PREFIX}-{i}",
                parent_id=None,
                title=f"목표 {i + 1}회차",
                node_type="leaf",
                order_index=i,
                is_leaf=True,
            )
            for i in range(3)
        ],
        action_items=[
            ActionItemDraft(
                node_id=f"{_PREFIX}-{i}",
                title=f"목표 {i + 1}회차",
                estimated_minutes=60,
                category="study",
                first_step="지난 회차에서 이어서 5분만 시작하기",
            )
            for i in range(3)
        ],
        policy_violations=[],
    )
    v = first_plan._review_variables(_state(only_ext))
    assert json.loads(v["action_items_json"]) == []
    assert json.loads(v["goal_nodes_json"]) == []


def test_extend_output_actually_carries_the_prefix() -> None:
    """⚠️ 생성부와 필터부가 갈리지 않게 — 상수를 바꿔도 이 테스트가 잡는다.

    필터는 `extend_action_plan_to_horizon` 이 그 접두사를 **실제로 붙인다**는 데 기대고
    있다. 생성부만 바뀌면 검토기가 다시 자리표시자를 보게 되는데, 배선 테스트만으로는
    아무것도 안 깨진다.
    """
    src = __import__("pathlib").Path(first_plan_adapter.__file__).read_text(encoding="utf-8")
    assert "branch_id = CONTINUATION_NODE_PREFIX" in src
    assert 'leaf_id = f"{CONTINUATION_NODE_PREFIX}-{i}"' in src
    assert first_plan_adapter.is_continuation_node(f"{_PREFIX}-7")
    assert first_plan_adapter.is_continuation_node(_PREFIX)
    # 분해 실패 폴백(`tmp-leaf-N`)은 걸리면 안 된다 — 그건 계획 전체가 자리표시자라
    # 검토기가 봐야 한다.
    assert not first_plan_adapter.is_continuation_node("tmp-leaf-3")
    assert not first_plan_adapter.is_continuation_node("leaf-1")
    assert not first_plan_adapter.is_continuation_node(None)
