"""Demo data seed — local/dev 만.

마스터 seed (failure tags / recovery strategies) 는 alembic 마이그레이션이 처리.
이 스크립트는 데모 시연용 단일 사용자 + 중간발표 시나리오 데이터를 만든다:

  "어제 실패한 'GROUP BY 실습'이, 오늘 아침 브리프에서 다시 태어나 완료되는 90초"
  (중간발표 데모 시나리오 v1.0 · G6)

생성물:
  - demo user 1명 (ACTIVE) + 행동 프로필 / 상호작용 스타일 / 알림 설정
  - 목표 1개 (SQL 학습, focus tier)
  - 어제 실패한 ActionItem 'GROUP BY 실습' + ScheduledBlock + ExecutionEvent(failed)
    + ExecutionFailureTag(AMBIGUITY)   ← "실패를 기억" 의 데이터 근거
  - 오늘 계획 ActionItem 3개 (재도전 GROUP BY + 2개) + ScheduledBlock
  - 오늘 Morning Brief (big_rock = 재도전 카드)

실행:
  uv run python -m scripts.db_seed_demo

idempotent: 같은 email 의 demo user / 목표가 이미 있으면 각 단계 skip (여러 번 실행 안전).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.db.models import (
    ActionItem,
    BehavioralProfile,
    DailyBrief,
    ExecutionEvent,
    ExecutionFailureTag,
    FailureReasonTag,
    Goal,
    InteractionStyle,
    NotificationSetting,
    ScheduledBlock,
    User,
)
from reaction_backend.db.session import get_sessionmaker

DEMO_EMAIL = "demo@reaction.local"
KST = timezone(timedelta(hours=9))

GOAL_TITLE = "SQL 로 데이터 분석하기"
GROUP_BY_TITLE = "GROUP BY 실습 — 집계 함수 5문제"
FAILURE_TAG_CODE = "AMBIGUITY"  # "막막해서 시작 못 함" (alembic 마스터 시드)


def _kst(d: date, hour: int, minute: int = 0) -> datetime:
    """KST 기준 tz-aware datetime (DB 는 timestamptz 로 UTC 저장)."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=KST)


async def _get_or_create_user(session: AsyncSession) -> tuple[User, bool]:
    existing = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
    if existing is not None:
        return existing, False

    user = User(
        email=DEMO_EMAIL,
        onboarding_state="ACTIVE",
        tone_mode="gentle",
        timezone="Asia/Seoul",
        is_beta=True,
    )
    session.add(user)
    await session.flush()  # user.id 확정

    session.add(
        BehavioralProfile(
            user_id=user.id,
            attention_span=30,
            energy_cycle="morning",
            time_chunk_preference=30,
            success_buffer=0.2,
        )
    )
    session.add(
        InteractionStyle(
            user_id=user.id,
            suggestion_style="neutral",
            recovery_tone="gentle",
            explanation_depth="medium",
            reminder_frequency="medium",
        )
    )
    session.add(NotificationSetting(user_id=user.id))
    return user, True


async def _seed_scenario(session: AsyncSession, user: User) -> None:
    """중간발표 시나리오 데이터. 이미 시드된 목표가 있으면 전체 skip."""
    existing_goal = await session.scalar(
        select(Goal).where(Goal.user_id == user.id, Goal.title == GOAL_TITLE)
    )
    if existing_goal is not None:
        print("  - 시나리오 데이터 이미 존재. skip.")
        return

    today = datetime.now(KST).date()
    yesterday = today - timedelta(days=1)

    # 1) 목표 (focus tier)
    goal = Goal(
        user_id=user.id,
        title=GOAL_TITLE,
        category="study",
        goal_tier="focus",
        priority_level=1,
        why_now="이번 학기 데이터베이스 과목의 핵심이에요.",
        first_step="강의자료 5장 예제부터.",
    )
    session.add(goal)
    await session.flush()

    # 2) 어제 실패한 GROUP BY 실습 (실행 기록 + 실패 사유)
    yday_action = ActionItem(
        user_id=user.id,
        title=GROUP_BY_TITLE,
        target_date=yesterday,
        estimated_minutes=30,
        status="failed",
        source="goal",
        goal_id=goal.id,
        category="study",
        priority=1,
        why_now="집계 함수가 약하다고 했어요.",
        first_step="예제 1번 문제 읽기.",
    )
    session.add(yday_action)
    await session.flush()

    yday_block = ScheduledBlock(
        user_id=user.id,
        action_item_id=yday_action.id,
        start_at=_kst(yesterday, 14, 0),
        end_at=_kst(yesterday, 14, 30),
        block_status="finished",
        source="ai_plan",
    )
    session.add(yday_block)
    await session.flush()

    yday_exec = ExecutionEvent(
        action_item_id=yday_action.id,
        scheduled_block_id=yday_block.id,
        user_id=user.id,
        plan_start_at=_kst(yesterday, 14, 0),
        plan_end_at=_kst(yesterday, 14, 30),
        actual_start_at=None,  # AMBIGUITY — 막막해서 시작조차 못 함
        actual_end_at=None,
        actual_duration_minutes=None,
        completion_status="failed",
    )
    session.add(yday_exec)
    await session.flush()

    # 실패 사유 태그 — 마스터에 있을 때만 (alembic upgrade 선행 필요)
    tag = await session.scalar(
        select(FailureReasonTag).where(FailureReasonTag.tag_code == FAILURE_TAG_CODE)
    )
    if tag is not None:
        session.add(ExecutionFailureTag(execution_id=yday_exec.id, tag_code=FAILURE_TAG_CODE))
    else:
        print(
            f"  ! 경고: 마스터 태그 '{FAILURE_TAG_CODE}' 없음 (alembic upgrade 먼저 실행 필요). 태그 skip.",
            file=sys.stderr,
        )

    # 3) 오늘 계획 3개 (어제 막힌 GROUP BY 재도전 + 2개)
    retry_action = ActionItem(
        user_id=user.id,
        title=GROUP_BY_TITLE,
        target_date=today,
        estimated_minutes=30,
        status="planned",
        source="goal",
        goal_id=goal.id,
        category="study",
        priority=1,
        why_now="어제 막혔던 부분이에요. 오늘은 더 작게 시작해요.",
        first_step="예제 1번 문제만 소리내어 읽기.",
    )
    today_actions = [
        retry_action,
        ActionItem(
            user_id=user.id,
            title="JOIN 개념 한 장 정리",
            target_date=today,
            estimated_minutes=25,
            status="planned",
            source="goal",
            goal_id=goal.id,
            category="study",
            priority=3,
        ),
        ActionItem(
            user_id=user.id,
            title="ERD 손으로 그려보기",
            target_date=today,
            estimated_minutes=20,
            status="planned",
            source="goal",
            goal_id=goal.id,
            category="study",
            priority=3,
        ),
    ]
    session.add_all(today_actions)
    await session.flush()

    for idx, act in enumerate(today_actions):
        session.add(
            ScheduledBlock(
                user_id=user.id,
                action_item_id=act.id,
                start_at=_kst(today, 10 + idx, 0),
                end_at=_kst(today, 10 + idx, 30),
                block_status="scheduled",
                source="ai_plan",
            )
        )

    # 4) 오늘 Morning Brief — big_rock = 재도전 카드
    session.add(
        DailyBrief(
            user_id=user.id,
            brief_date=today,
            headline_text="어제 못한 'GROUP BY 실습', 오늘은 딱 5분 예제 하나로 다시 시작해볼까요?",
            big_rock_action_item_id=retry_action.id,
            expires_at=_kst(today + timedelta(days=1), 6, 0),
            fallback_used=True,  # 시드 데이터 (LLM 미경유)
        )
    )

    print(
        f"  + 시나리오 시드: 목표 1 / 어제 실패 1 (+{FAILURE_TAG_CODE}) "
        f"/ 오늘 계획 {len(today_actions)} / Morning Brief 1"
    )


async def seed() -> int:
    settings = get_settings()
    if settings.app_env == "prod":
        print("REFUSED: app_env=prod 에서는 demo seed 금지.", file=sys.stderr)
        return 2

    factory = get_sessionmaker()
    async with factory() as session:
        user, created = await _get_or_create_user(session)
        if created:
            await session.flush()
            print(f"[OK] demo user 생성: {DEMO_EMAIL} (id={user.id})")
        else:
            print(f"demo user 이미 존재 (id={user.id}).")

        await _seed_scenario(session, user)
        await session.commit()

    return 0


def main() -> int:
    return asyncio.run(seed())


if __name__ == "__main__":
    sys.exit(main())
