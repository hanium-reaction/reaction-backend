"""`goals.heaviest` 동적 보기 — 만다라 승격 목표 병합 (ADR-0008 §8 "B").

`_question_options` 은 순수 함수라 HTTP 를 안 태우고 직접 검증한다 — `mandala_goal_titles`
의 실제 DB 조회(`mandala_adapter.fetch_promoted_goal_titles_for_user`)는
`_FakeSession.execute` 가 항상 빈 결과라(다른 mandala 라우트 테스트와 같은 HTTP 경계
한계, `test_mandala_tree_route.py` 참고) 이 계층에서 확인할 수 없다.
"""

from __future__ import annotations

from reaction_backend.api.routes.interview import _question_options


def test_goals_heaviest_merges_mandala_titles_before_typed_ones() -> None:
    slot_answers = {"goals.list": {"type": "text", "raw": "토익", "normalized": ["토익"]}}
    options = _question_options(
        "goals.heaviest", slot_answers, mandala_goal_titles=["메이저리그 드래프트"]
    )
    assert options == ["메이저리그 드래프트", "토익"]


def test_goals_heaviest_dedupes_overlapping_titles() -> None:
    """승격 목표와 같은 제목을 goals.list 에도 타이핑했으면 한 번만 나온다."""
    slot_answers = {"goals.list": {"type": "text", "raw": "", "normalized": ["토익", "캡스톤"]}}
    options = _question_options("goals.heaviest", slot_answers, mandala_goal_titles=["토익"])
    assert options == ["토익", "캡스톤"]


def test_goals_heaviest_without_mandala_titles_matches_previous_behavior() -> None:
    """mandala_goal_titles 생략(기본값 ()) — 기존 동작과 100% 동일."""
    slot_answers = {"goals.list": {"type": "text", "raw": "캡스톤", "normalized": ["캡스톤"]}}
    assert _question_options("goals.heaviest", slot_answers) == ["캡스톤"]


def test_goals_heaviest_empty_when_nothing_to_suggest() -> None:
    assert _question_options("goals.heaviest", {}) == []


def test_goals_heaviest_shows_mandala_titles_even_before_goals_list_answered() -> None:
    """goals.list 를 아직 안 답했어도(정상 순서상 먼저 묻지만) 승격 목표는 보인다."""
    assert _question_options("goals.heaviest", {}, mandala_goal_titles=["메이저리그 드래프트"]) == [
        "메이저리그 드래프트"
    ]


def test_non_heaviest_slot_ignores_mandala_titles() -> None:
    """chip 슬롯(identity.role)은 mandala_goal_titles 를 무시하고 카탈로그 고정 보기 그대로."""
    options = _question_options("identity.role", {}, mandala_goal_titles=["무관한 목표"])
    assert "무관한 목표" not in options
    assert options  # 카탈로그 보기가 비어 있지 않다
