"""재계획이 '이어가기' 자리표시자에 **실제로 내용을 채운다** (#454 방향 1).

## 무엇이 거짓이었나

제품은 확장 구간을 만들면서 사용자에게 이렇게 고지했다:

    "회차의 구체적인 내용은 매주 재계획에서 그때 진행 상황에 맞춰 채워집니다"

**두 겹으로 거짓이었다.** 재계획은 `title=c.title` 로 제목을 그대로 옮기는 결정적
스케줄러라 채우지 않았고, 크론이 없어 매주 돌지도 않았다(사용자가 눌러야 돈다).

이 파일이 지키는 것은 셋이다:

1. LLM 출력을 **보낸 자리표시자에만** 짝짓는가 (지어낸 id 로 엉뚱한 카드를 덮어쓰지 않는가)
2. 실패했을 때 **자리표시자를 그대로 두는가** (억지로 채우지 않는가)
3. 고지 문구가 이제 **하는 일을 말하는가**
"""

from __future__ import annotations

import uuid

from reaction_backend.orchestrator import first_plan_adapter as FPA
from reaction_backend.orchestrator.continuation_fill import (
    MAX_FILL_PER_RUN,
    PlaceholderCard,
    format_completed,
    match_to_placeholders,
    select_fillable,
)
from reaction_backend.schemas.planning import ContinuationCard, ContinuationFill

A1 = uuid.UUID("11111111-1111-4111-8111-111111111111")
A2 = uuid.UUID("22222222-2222-4222-8222-222222222222")
N1 = uuid.UUID("aaaaaaaa-1111-4111-8111-111111111111")


def _ph(aid: uuid.UUID, title: str) -> PlaceholderCard:
    return PlaceholderCard(action_id=aid, title=title, node_id=N1)


def _fill(*cards: tuple[str, str, str]) -> ContinuationFill:
    return ContinuationFill(
        cards=[ContinuationCard(action_id=a, title=t, first_step=f) for a, t, f in cards]
    )


# ── 짝짓기 — 여기서 새면 사용자 데이터가 깨진다 ─────────────────────────────


def test_filled_cards_are_matched_to_the_placeholders_we_sent() -> None:
    out = match_to_placeholders(
        _fill((str(A1), "2회독 오답만 다시 풀기", "채점한 시험지 펴서 틀린 문제에 표시하기")),
        [_ph(A1, "정보처리기사 실기 합격 21회차")],
    )
    assert len(out) == 1
    assert out[0].action_id == A1
    assert out[0].title == "2회독 오답만 다시 풀기"
    assert out[0].first_step == "채점한 시험지 펴서 틀린 문제에 표시하기"


def test_unknown_action_id_is_dropped() -> None:
    """⚠️ **엉뚱한 카드 덮어쓰기 방지.** 스키마는 이 id 가 우리가 보낸 것인지 못 본다.

    LLM 이 id 를 지어내거나 잘못 옮겨 적으면, 그대로 쓸 경우 **관계없는 사용자 카드의
    제목과 첫걸음이 통째로 바뀐다.**
    """
    out = match_to_placeholders(
        _fill((str(A2), "엉뚱한 카드", "엉뚱한 첫걸음")),
        [_ph(A1, "정보처리기사 실기 합격 21회차")],
    )
    assert out == []


def test_duplicate_action_id_keeps_only_the_first() -> None:
    out = match_to_placeholders(
        _fill((str(A1), "첫 번째", "첫 걸음"), (str(A1), "두 번째", "다른 걸음")),
        [_ph(A1, "21회차")],
    )
    assert [c.title for c in out] == ["첫 번째"]


def test_echoing_the_placeholder_title_back_is_not_a_fill() -> None:
    """제목을 그대로 돌려준 건 채운 게 아니다 — 저장하면 아무것도 안 달라지는데
    노드 출처만 `llm` 로 넘어가 **다음 재계획에서 다시 채울 기회를 잃는다.**"""
    out = match_to_placeholders(
        _fill((str(A1), "정보처리기사 실기 합격 21회차", "지난 회차에서 이어서 5분만 시작하기")),
        [_ph(A1, "정보처리기사 실기 합격 21회차")],
    )
    assert out == []


def test_blank_content_is_dropped() -> None:
    out = match_to_placeholders(
        _fill((str(A1), "   ", "첫 걸음"), (str(A2), "제목", "  ")),
        [_ph(A1, "21회차"), _ph(A2, "22회차")],
    )
    assert out == []


# ── 맥락 문자열 ─────────────────────────────────────────────────────────────


def test_no_completed_cards_says_so_instead_of_going_blank() -> None:
    """⚠️ 빈 문자열을 넣으면 LLM 이 빈 자리를 보고 지어낸다. '아직 없다'도 정보다."""
    text = format_completed([])
    assert text.strip()
    assert "없다" in text


def test_completed_list_is_capped() -> None:
    text = format_completed([f"카드 {i}" for i in range(40)], limit=3)
    assert len(text.splitlines()) == 3


# ── 이번 실행에서 채울 양 ───────────────────────────────────────────────────


def test_fill_is_capped_per_run() -> None:
    """남기는 게 손해가 아니다 — 남은 자리표시자는 다음 재계획에서 **더 많은 진행 기록**을
    근거로 채워진다. 먼 미래를 지금 억지로 정하는 것보다 낫다."""
    many = [_ph(uuid.uuid4(), f"{i}회차") for i in range(28)]
    assert len(select_fillable(many)) == MAX_FILL_PER_RUN
    assert select_fillable(many)[0] is many[0]  # 앞에서부터


# ── 고지 문구 (c) ───────────────────────────────────────────────────────────


def test_warning_no_longer_promises_a_weekly_job_that_does_not_exist() -> None:
    """⚠️ **거짓 약속 회귀 가드.**

    재계획에는 크론이 없다 — 등록된 스케줄러 잡 11종 중 없고, `scheduler_enabled` 기본값도
    `False` 다. "매주 재계획에서 채워집니다" 는 채우지도 않고 매주 돌지도 않는 약속이었다.
    """
    msg = FPA.coverage_extended_warning(8, "2026-11-30")
    assert msg is not None
    assert "매주" not in msg, msg
    # 사용자가 하는 행동으로 말한다 — 없는 주기가 아니라.
    assert "재계획" in msg
    assert "비어" in msg  # 지금 비어 있다는 사실을 숨기지 않는다


def test_warning_still_absent_when_nothing_was_extended() -> None:
    assert FPA.coverage_extended_warning(0, "2026-11-30") is None
