"""90일 비활성 자동 익명화 cron job (#24, DevBaseline §1.4 잠금 결정).

베이스라인이 "90일 비활성 자동 익명화 매일 04:00 KST" 를 잠갔고 DB 컬럼
(`users.last_active_at` / `is_anonymized` / `anonymized_at`)·단건 마스킹
(`PrivacyRepo.anonymize_user`)·04:00 배치 슬롯까지 전부 준비돼 있었는데, **job 함수가
없어서 등록조차 못 하고 있었다** — `scheduler/README.md` cron 표에는 이미 올라가 있고
`runtime.py` 헤더가 "anonymize_inactive 는 job 함수 미구현 → 미등록" 이라고 자인하던
상태다. 그래서 `last_active_at` 을 **읽는** 코드가 레포 전체에 0곳이었다(로그인 시 쓰기만).

## 무엇을 하는가 — `POST /settings/anonymize`(#23-B)와 **같은 정의**의 익명화

사용자가 직접 누르는 익명화와 정확히 같은 일을 한다: `*_encrypted` 컬럼을
`[anonymized]` sentinel 로 덮고, 이름을 마스킹하고, `is_anonymized`/`anonymized_at` 을
세운다. hard delete 아님(AGENTS §2) — 행은 보존된다.

**email 은 건드리지 않는다.** 이건 계정 삭제(`POST /settings/delete-account`, #321)와
갈리는 지점이고 의도적이다:

- email 은 Google 로그인의 1차 식별 키다. 마스킹하면 91일 만에 돌아온 사용자가 로그인
  자체를 못 하고 새 계정이 생긴다 — 사용자 입장에선 "비활성" 이 곧 "계정 삭제" 가 된다.
  베이스라인이 잠근 건 *익명화*지 삭제가 아니다.
- 수동 익명화(`api/routes/settings.py`)가 이미 email 을 남기는 쪽으로 리뷰를 통과했다.
  같은 이름의 동작이 트리거(사람 vs cron)에 따라 다른 의미를 가지면 안 된다.

즉 이 job 이 지우는 것은 **떠난 사용자의 자유서술 텍스트**(회고 메모·실패 사유 메모·
인박스 원문·LLM 입출력 요약·캘린더 토큰)이고, 남기는 것은 로그인 가능성과 통계 집계다.

## 왜 사용자별 commit 인가

`expire_proposed_goals`(단일 테이블 bulk UPDATE + 1회 commit)와 달리 여기는 사용자 1명당
6개 테이블을 건드린다. 한 사용자에서 실패했다고 그날 배치 전체가 롤백되면, 다음 날 04:00
까지 나머지가 통째로 안 지워진다. `notify_sweeps` 의 **건당 commit + except 격리**
(ADR-0006 §8, 다중 인스턴스 안전성 근거이기도 하다)를 그대로 따른다.

**사용자 알림은 없다.** `expire_proposed_goals` 와 같은 근거(ADR-0005 §7.8 — 만료 자체는
자동, 알림은 X). 알림을 붙이려면 AGENTS §1 이 잠근 3클래스에 4번째를 더해야 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from reaction_backend.repositories.privacy_repo import PrivacyRepo
from reaction_backend.repositories.user_repo import UserRepo
from reaction_backend.safety.encryption import ANONYMIZED_SENTINEL
from reaction_backend.schemas.common import now_kst

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

# DevBaseline §1.4 잠금 — 90일. 다른 만료 TTL(초안 72h·회고 3일·잠정목표 14일)과 달리
# 이 값은 제품 결정이라 임의로 못 줄인다(AGENTS §1).
INACTIVE_ANONYMIZE_TTL_DAYS = 90


@dataclass(slots=True)
class AnonymizeResult:
    """배치 결과 — 관측/로그용 (`sweeps.SweepResult` 와 같은 역할)."""

    total: int
    """대상으로 뽑힌 사용자 수."""
    anonymized: int
    failed: int


def inactive_anonymize_before(now: datetime) -> datetime:
    """익명화 경계 — **단일 소스**.

    프리뷰 스크립트(`scripts/preview_anonymize_inactive.py`)가 이 함수를 그대로 import
    한다. 양쪽이 각자 계산하면 한쪽만 바뀌었을 때 "프리뷰가 보여준 명단"과 "실제로 지워질
    명단"이 어긋난다 — `proposed_goal_stale_before`·`pending_reflection_since` 와 같은 이유.
    """
    return now - timedelta(days=INACTIVE_ANONYMIZE_TTL_DAYS)


async def run_anonymize_inactive_users(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    user_repo: UserRepo | None = None,
    privacy_repo: PrivacyRepo | None = None,
) -> AnonymizeResult:
    """90일 넘게 안 돌아온 사용자를 익명화하고 **사용자별로** commit.

    repo 인자는 테스트 주입용(기본은 세션 기반). `now` 미지정 시 `now_kst()`.
    **idempotent** — `anonymized_at IS NULL` 필터가 이미 처리된 사용자를 걸러낸다.
    """
    users_repo = user_repo or UserRepo(session)
    priv_repo = privacy_repo or PrivacyRepo(session)
    now_dt = now or now_kst()

    users = await users_repo.list_inactive_for_anonymization(
        before=inactive_anonymize_before(now_dt)
    )
    anonymized = failed = 0
    for user in users:
        try:
            await priv_repo.anonymize_user(user.id)
            user.is_anonymized = True
            user.anonymized_at = now_dt
            user.name = ANONYMIZED_SENTINEL
            await session.commit()  # 사용자 단위 commit — 모듈 docstring
            anonymized += 1
        except Exception:  # noqa: BLE001 — 한 사용자 실패가 배치를 멈추지 않게
            failed += 1
            _log.exception("anonymize_inactive failed for user %s", user.id)
            await session.rollback()

    # 되돌릴 수 없는 마스킹이라 건수를 남긴다 — 사고 시 영향 범위 산정용
    # (`expire_proposed_goals` 와 같은 이유).
    if anonymized or failed:
        _log.info(
            "anonymize_inactive: total=%d anonymized=%d failed=%d",
            len(users),
            anonymized,
            failed,
        )
    return AnonymizeResult(total=len(users), anonymized=anonymized, failed=failed)
