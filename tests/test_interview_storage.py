"""`interview._decide_storage` 표 단위 테스트 — 저장 결정 순수 함수 (LLM/async 없음).

validate_answer 의 분기(스킵/핵심/pending/제약/clarity)를 LLM stub 없이 표로 고정한다.
서명: _decide_storage(slot_key, answer_type, last_answer, normalized, clarity, attempts)
       -> (stored: dict|None, filled_now: bool)
"""

from __future__ import annotations

from typing import Any

import pytest

from reaction_backend.orchestrator import interview
from reaction_backend.orchestrator.interview import _SKIP_MARKER, _decide_storage, _pending

_TEXT = {"type": "text", "raw": "x"}  # 대표 자유서술 raw (내용은 normalized/clarity 로 제어)


@pytest.mark.parametrize(
    ("slot_key", "answer_type", "last_answer", "normalized", "clarity", "attempts", "expected"),
    [
        # 답 미주입(배치 그래프) → 저장·충족 없음
        ("identity.role", "chip", None, None, 0.9, 1, (None, False)),
        # chip: LLM 정규화값 → chip 구조로 저장, 충족
        (
            "identity.role",
            "chip",
            {"type": "text", "raw": "컴공 3학년"},
            "3학년",
            0.2,
            1,
            ({"type": "chip", "values": ["3학년"]}, True),
        ),
        # text 고clarity → 저장·충족
        (
            "goals.success_image",
            "text",
            {"type": "text", "raw": "발표 잘 마치기"},
            "발표 잘 마치기",
            0.9,
            1,
            ({"type": "text", "raw": "발표 잘 마치기"}, True),
        ),
        # text 저clarity·비스킵·비핵심·상한 전 → 재질문(pending)
        (
            "goals.success_image",
            "text",
            {"type": "text", "raw": "음 그냥"},
            None,
            0.1,
            1,
            (_pending(1), False),
        ),
        # text 스킵 의사(비핵심) → 스킵 저장·진행
        (
            "goals.success_image",
            "text",
            {"type": "text", "raw": "없어"},
            None,
            0.0,
            1,
            (_SKIP_MARKER, True),
        ),
        # 제약 슬롯(chip) LLM 매핑 실패·비스킵·비핵심 → 스킵으로 진행(무루프)
        (
            "recovery.tone",
            "chip",
            {"type": "text", "raw": "빨간색으로"},
            None,
            0.1,
            1,
            (_SKIP_MARKER, True),
        ),
        # 핵심(goals.list) 스킵 신호·상한 전 → 스킵 거부, 재질문(pending)
        ("goals.list", "text", {"type": "text", "raw": "없어"}, "", 0.1, 1, (_pending(1), False)),
        # 핵심(goals.list) 상한 도달·비지 않은 답 → best-effort 채택·진행
        (
            "goals.list",
            "text",
            {"type": "text", "raw": "그냥 뭐라도"},
            "",
            0.1,
            3,
            ({"type": "text", "raw": "그냥 뭐라도"}, True),
        ),
        # 핵심(goals.heaviest) LLM 못 고름·상한 전 → 재질문(pending)
        (
            "goals.heaviest",
            "select",
            {"type": "text", "raw": "이것저것"},
            None,
            0.2,
            2,
            (_pending(2), False),
        ),
    ],
)
def test_decide_storage(
    slot_key: str,
    answer_type: str,
    last_answer: dict[str, Any] | None,
    normalized: Any,
    clarity: float,
    attempts: int,
    expected: tuple[dict[str, Any] | None, bool],
) -> None:
    assert (
        _decide_storage(slot_key, answer_type, last_answer, normalized, clarity, attempts)
        == expected
    )


def test_critical_slots_are_goal_defining() -> None:
    """핵심 슬롯 정의가 '핵심 목표'(goals.list/heaviest)인지 — 스킵 거부 대상."""
    assert frozenset({"goals.list", "goals.heaviest"}) == interview.CRITICAL_SLOTS
