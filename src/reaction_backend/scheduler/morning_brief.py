"""Morning Brief cron job — S10 daily_briefs precompute (Issue #19-C).

매일 사용자별 `notification_settings.morning_brief_time` 기준 1회 실행 (룰 + LLM 1회, 8s fallback).
`daily_briefs` 1행 INSERT. **idempotent** — 같은 (user, date) 이미 있으면 skip.

⚠️ 본 모듈은 **job 함수**다. 실제 스케줄 트리거(매일 06:00 등록)는 Issue #24 운영준비에서
APScheduler/Arq 로 `run_morning_brief_for_user` 를 등록한다 (scheduler/README.md 시간표).

LLM 은 `aiClient.run` 단일 게이트 (AGENTS.md §2). `GEMINI_API_KEY` 없으면 룰 fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.llm import aiClient
from reaction_backend.schemas.today import MorningBriefDraft

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from reaction_backend.repositories.action_item_repo import ActionItemRepo
    from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo


def _rule_brief(cards: list[ActionItem]) -> MorningBriefDraft:
    """LLM 실패 시 — 카드 수 기반 결정적 헤드라인 (금지어 없음, 따뜻한 톤)."""
    if not cards:
        return MorningBriefDraft(
            headline_ko="오늘은 아직 잡힌 카드가 없어요. 가볍게 하나만 정해볼까요?",
            first_step="지금 떠오르는 일 하나를 Inbox에 적어보기",
            reason_why_now="작게 시작하는 게 오늘의 첫 걸음이에요.",
            adjustment_hints=[],
        )
    top = cards[0]
    return MorningBriefDraft(
        headline_ko=f"오늘 {len(cards)}개의 카드가 있어요. '{top.title}'부터 가볍게 시작해볼까요?",
        first_step=top.first_step or "5분만 들여다보기",
        reason_why_now=top.why_now or "가장 우선인 카드예요.",
        adjustment_hints=[],
    )


def _expires_next_day(brief_date_dt: datetime) -> datetime:
    """다음 날 새벽까지 유효 (브리프는 당일용)."""
    return brief_date_dt + timedelta(days=1)


async def run_morning_brief_for_user(
    user_id: UUID,
    now_kst_dt: datetime,
    *,
    action_repo: ActionItemRepo,
    brief_repo: DailyBriefRepo,
    session: AsyncSession,
) -> DailyBrief:
    """사용자 1명의 오늘 Morning Brief 생성 (idempotent).

    이미 오늘 brief 가 있으면 그대로 반환 (재실행 안전). 없으면 LLM(+룰 fallback)으로 생성.
    """
    brief_date = now_kst_dt.date()
    existing = await brief_repo.get_by_date(user_id, brief_date)
    if existing is not None:
        return existing  # idempotent — 같은 날 재실행 skip

    cards = await action_repo.list_by_date(user_id, brief_date)
    focus_titles = ", ".join(c.title for c in cards[:3]) or "(없음)"

    result = await aiClient.run(
        module="brief",
        schema=MorningBriefDraft,
        prompt_id="brief/morning_brief",
        fallback=lambda: _rule_brief(cards),
        timeout=8.0,
        variables={
            "today_kst": brief_date.isoformat(),
            "yesterday_summary": "(데이터 없음)",
            "today_focus_cards": focus_titles,
            "today_maintain_cards": "(없음)",
            "behavioral_summary": "(없음)",
        },
        user_id=user_id,
        session=session,
    )
    draft = result.value
    hints = [{"text": h} for h in draft.adjustment_hints]

    return await brief_repo.create(
        user_id,
        brief_date,
        headline_text=draft.headline_ko,
        big_rock_action_item_id=cards[0].id if cards else None,
        adjustment_hints=hints,
        fallback_used=result.fell_back,
        expires_at=_expires_next_day(now_kst_dt),
    )
