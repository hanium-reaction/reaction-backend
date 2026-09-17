"""Morning Brief cron job — S10 daily_briefs precompute (Issue #19-C).

매일 사용자별 `notification_settings.morning_brief_time` 기준 1회 실행 (룰 + LLM 1회, 8s fallback).
`daily_briefs` 1행 INSERT. **idempotent** — 같은 (user, date) 이미 있으면 skip.

⚠️ 본 모듈은 **job 함수**다. 실제 스케줄 트리거(매일 06:00 등록)는 Issue #24 운영준비에서
APScheduler/Arq 로 `run_morning_brief_for_user` 를 등록한다 (scheduler/README.md 시간표).

LLM 은 `aiClient.run` 단일 게이트 (AGENTS.md §2). `GEMINI_API_KEY` 없으면 룰 fallback.

프롬프트 변수는 **아는 것만 싣는다** (#224): 예전엔 5개 중 3개가 하드코딩 스텁이라
LLM 이 "(데이터 없음)" 자리를 "어제는 조용히 잘 보냈어요" 같은 문장으로 메웠다 — 어제
실패한 사용자에게 앱이 자기를 안 보고 있다고 광고하는 꼴. 어제 요약은 어제 카드의
실제 상태로 계산하고, 못 채우는 변수는 프롬프트 규칙으로 언급 자체를 금지한다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.daily_brief import DailyBrief
from reaction_backend.domain import calendar_conflict
from reaction_backend.integrations.google_calendar import freebusy, oauth
from reaction_backend.llm import aiClient
from reaction_backend.orchestrator import mandala_adapter
from reaction_backend.schemas.common import KST, to_kst
from reaction_backend.schemas.today import MorningBriefDraft

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from reaction_backend.repositories.action_item_repo import ActionItemRepo
    from reaction_backend.repositories.daily_brief_repo import DailyBriefRepo
    from reaction_backend.repositories.execution_repo import ExecutionRepo
    from reaction_backend.repositories.goal_repo import GoalRepo

# 완료로 세는 상태. partial_done 은 별도 집계(했지만 다 못 함 — 뭉뚱그리면 회고와 어긋난다).
_DONE_STATUSES = frozenset({"done", "over_done"})
# 아침에 '시작할' 수 있는 카드 — 이미 done 인 카드를 big rock 으로 앉히지 않는다 (#224).
_ACTIONABLE_STATUSES = frozenset({"planned", "in_progress"})


def _yesterday_summary(cards: list[ActionItem]) -> str:
    """어제 카드의 실제 상태 → 결정적 한 줄. 카드가 없으면 명시 마커(프롬프트가 언급 금지).

    실제 데이터만 말한다 — 실패 사유·회고 내용까지 끌어오는 건 후속(리뷰 연계). 여기서는
    "몇 장 중 몇 장" 수준의 사실이면 LLM 이 어제를 지어내는 것을 막기에 충분하다.
    """
    if not cards:
        return "(어제 카드 없음)"
    done = sum(1 for c in cards if c.status in _DONE_STATUSES)
    partial = sum(1 for c in cards if c.status == "partial_done")
    rest = len(cards) - done - partial
    parts = [f"완료 {done}장"]
    if partial:
        parts.append(f"부분 완료 {partial}장")
    if rest:
        parts.append(f"못 마친 카드 {rest}장")
    return f"어제 카드 {len(cards)}장 — " + ", ".join(parts)


def _titles(cards: list[ActionItem], limit: int) -> str:
    return ", ".join(c.title for c in cards[:limit]) or "(없음)"


def _rule_brief(cards: list[ActionItem]) -> MorningBriefDraft:
    """LLM 실패 시 — 카드 수 기반 결정적 헤드라인 (금지어 없음, 따뜻한 톤).

    `cards` 는 **오늘 시작할 수 있는** 카드만(호출자가 done 제외) — 완료한 카드부터
    시작해보라는 헤드라인을 만들지 않는다.
    """
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


#: 브리프에 싣는 겹침 문장 수 — 나머지는 한 줄로 묶는다(힌트가 겹침 목록이 되면 안 된다).
_MAX_CONFLICT_HINTS = 2


async def _calendar_conflict_hints(
    session: AsyncSession,
    *,
    user_id: UUID,
    now_kst_dt: datetime,
    cards: list[ActionItem],
    execution_repo: ExecutionRepo | None,
) -> list[str]:
    """오늘 카드의 아직 시작 안 한 블록 중 Google 캘린더 일정과 겹치는 것 → 브리프 문장.

    계획은 만들 때 캘린더를 피하지만 **그 뒤 생긴 약속**은 모른다. 아침에 한 번 확인해
    알려준다 — 옮기지는 않는다(자동 적용 금지). 새 알림을 보내지도 않는다 — 알림 클래스는
    3종 잠금이라(AGENTS §1) 이미 있는 브리프 안에서만 말한다.

    연결이 없거나 못 읽으면 빈 목록이다 — 브리프가 캘린더 때문에 실패하면 안 된다.
    """
    if execution_repo is None or not cards or not oauth.is_enabled():
        return []
    today = to_kst(now_kst_dt).date()
    day_start = datetime.combine(today, time(0, 0), tzinfo=KST)
    result = await freebusy.fetch_busy(
        session, user_id=user_id, start=day_start, end=day_start + timedelta(days=1)
    )
    if result.status != "ok" or not result.intervals:
        return []

    blocks = await execution_repo.list_active_blocks_for_actions(user_id, [c.id for c in cards])
    keyed = [((aid, start), status, start, end) for aid, status, start, end in blocks]
    hits = calendar_conflict.conflicting_keys(
        keyed, [(iv.start, iv.end) for iv in result.intervals], now=now_kst_dt
    )
    # 카드당 한 번 — 한 카드가 세션 여러 개로 나뉘어도 문장은 가장 이른 블록 하나로.
    first_hit: dict[UUID, datetime] = {}
    for aid, start in hits:
        if aid not in first_hit or start < first_hit[aid]:
            first_hit[aid] = start
    titles = {c.id: c.title for c in cards}
    ordered = sorted(first_hit.items(), key=lambda item: item[1])
    hints = [
        f"오늘 {to_kst(start):%H:%M} '{titles.get(aid, '카드')}' 시간이 캘린더 일정과 겹쳐요. "
        "시간을 옮겨 볼까요?"
        for aid, start in ordered[:_MAX_CONFLICT_HINTS]
    ]
    if len(ordered) > _MAX_CONFLICT_HINTS:
        hints.append(f"그 밖에 {len(ordered) - _MAX_CONFLICT_HINTS}개 카드도 캘린더 일정과 겹쳐요.")
    return hints


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
    goal_repo: GoalRepo | None = None,
    tone_mode: str | None = None,
    execution_repo: ExecutionRepo | None = None,
) -> DailyBrief:
    """사용자 1명의 오늘 Morning Brief 생성 (idempotent).

    이미 오늘 brief 가 있으면 그대로 반환 (재실행 안전). 없으면 LLM(+룰 fallback)으로 생성.
    `tone_mode` 는 LLM 시스템 프롬프트 톤 prefix 용 (#23) — #24 cron wrapper 가 사용자별로 전달.
    `goal_repo` 가 있으면 목표 tier 로 focus/maintain 카드를 가른다(없으면 전부 focus 취급 —
    기존 호출부 하위호환). `execution_repo` 가 있으면 오늘 블록과 Google 캘린더 겹침을
    `adjustment_hints` 맨 앞에 싣는다(없으면 캘린더를 보지 않는다 — 하위호환).
    """
    brief_date = now_kst_dt.date()
    existing = await brief_repo.get_by_date(user_id, brief_date)
    if existing is not None:
        return existing  # idempotent — 같은 날 재실행 skip

    cards = await action_repo.list_by_date(user_id, brief_date)
    yesterday_cards = await action_repo.list_by_date(user_id, brief_date - timedelta(days=1))

    # 아침에 시작할 수 있는 카드만 — done 카드가 big rock/focus 목록에 앉으면
    # "이미 끝낸 일부터 시작해보라"는 브리프가 나온다 (#224 5번).
    actionable = [c for c in cards if c.status in _ACTIONABLE_STATUSES]
    tiers: dict[UUID, str] = {}
    if goal_repo is not None:
        tiers = {g.id: g.goal_tier for g in await goal_repo.list_active(user_id)}
    maintain_cards = [
        c for c in actionable if c.goal_id is not None and tiers.get(c.goal_id) == "maintain"
    ]
    focus_cards = [c for c in actionable if c.goal_id is None or tiers.get(c.goal_id) != "maintain"]
    # 만다라 → 브리프 연결(PR7) — 승격된 축 중 실제로 active 인 게 있으면 "이번 주 굴리는
    # 축" 한 줄로 언급될 수 있게. 없으면 None → "(없음)" 마커라 프롬프트가 언급 자체를 금지한다
    # (behavioral_summary 와 같은 "못 채우는 변수는 지어내지 않는다" 원칙, #224).
    active_axis = await mandala_adapter.find_active_axis_label(session, user_id)

    result = await aiClient.run(
        module="brief",
        schema=MorningBriefDraft,
        prompt_id="brief/morning_brief",
        fallback=lambda: _rule_brief(actionable),
        timeout=8.0,
        variables={
            "today_kst": brief_date.isoformat(),
            "yesterday_summary": _yesterday_summary(yesterday_cards),
            "today_focus_cards": _titles(focus_cards, 3),
            "today_maintain_cards": _titles(maintain_cards, 5),
            # 행동 프로파일(피크/드레인 등) 연계는 후속 — 지금은 마커를 보내고 프롬프트가
            # 상상 금지를 강제한다. 주간 리뷰 지표가 계획-시각 기반 왜곡을 벗은 뒤(#222
            # 이후 데이터부터) 연결하는 게 안전하다.
            "behavioral_summary": "(데이터 없음)",
            "active_axis_hint": active_axis or "(없음)",
        },
        user_id=user_id,
        session=session,
        tone_mode=tone_mode,
    )
    draft = result.value
    # 캘린더 겹침은 LLM 힌트보다 앞에 둔다 — 사실이고, 아침에 바로 손쓸 수 있는 것이다.
    calendar_hints = await _calendar_conflict_hints(
        session,
        user_id=user_id,
        now_kst_dt=now_kst_dt,
        cards=actionable,
        execution_repo=execution_repo,
    )
    hints = [{"text": h} for h in (*calendar_hints, *draft.adjustment_hints)]

    big_rock = focus_cards[0] if focus_cards else (actionable[0] if actionable else None)
    return await brief_repo.create(
        user_id,
        brief_date,
        headline_text=draft.headline_ko,
        big_rock_action_item_id=big_rock.id if big_rock is not None else None,
        adjustment_hints=hints,
        fallback_used=result.fell_back,
        expires_at=_expires_next_day(now_kst_dt),
    )
