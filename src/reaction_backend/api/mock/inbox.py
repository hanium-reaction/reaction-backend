"""Inbox mock fixture — #3-D 스텁용 (S24 Life Inbox).

자연어 1줄 캡처. AI 카테고리 추정·실제 암호화는 후속 (#22 실구현).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DemoInboxItem:
    """Inbox 항목 한 개 (api-contract §18). raw_text 는 실제로는 암호화 저장."""

    inbox_id: str
    raw_text: str  # 실제: raw_text_encrypted — 스텁은 평문 노출
    ai_category_guess: str | None
    user_category: str | None
    status: str  # captured | classified | archived | promoted
    promoted_goal_id: str | None


DEMO_INBOX_ITEMS: tuple[DemoInboxItem, ...] = (
    DemoInboxItem("inbox_01", "프로젝트 발표 자료 만들기", "프로젝트", None, "classified", None),
    DemoInboxItem("inbox_02", "할머니 생신 선물 사기", None, None, "captured", None),
)
