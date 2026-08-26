"""90일 비활성 익명화 cron 사전 실측 — 켜기 전에 '누가 익명화될지' 읽기 전용으로 센다.

배경(#24 · `preview_expire_proposed_goals.py` 선례): `anonymize_inactive` cron(매일 04:00
KST)은 `SCHEDULER_ENABLED` 가 기본 OFF 라 라이브에서 돈 적이 없다. 켜는 순간 **첫 04:00 에
90일 넘게 안 돌아온 사용자의 자유서술 텍스트가 일괄 마스킹**되고 이건 **되돌릴 수 없다**
(`[anonymized]` sentinel 로 덮어쓰기 — 원문은 복구 불가). 그래서 다른 만료 cron 보다도
사전 실측이 중요하다.

**아무것도 쓰지 않는다** — SELECT 뿐. `--apply` 같은 옵션 자체가 없다.

정확성: 후보 판정을 미러링하지 않고 **cron 이 쓰는 바로 그 함수**
(`UserRepo.list_inactive_for_anonymization` + `inactive_anonymize_before`)를 그대로 부른다.
`expire_proposed_goals` 프리뷰는 저쪽이 UPDATE 라 WHERE 를 손으로 베끼고 동기화 테스트로
고정해야 했지만, 이쪽 후보 조회는 원래 SELECT 라 재사용이 되고 그러면 **드리프트가 애초에
불가능하다**.

실행 (라이브 EC2 self-hosted runner 에서 workflow_dispatch 로):
  uv run python -m scripts.preview_anonymize_inactive
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.session import get_sessionmaker
from reaction_backend.repositories.user_repo import UserRepo
from reaction_backend.scheduler.anonymize_inactive import (
    INACTIVE_ANONYMIZE_TTL_DAYS,
    inactive_anonymize_before,
)
from reaction_backend.schemas.common import now_kst, to_kst


async def _preview(session: AsyncSession) -> None:
    now = now_kst()
    before = inactive_anonymize_before(now)
    print(f"기준 시각: {now.isoformat()}")
    print(f"비활성 경계({INACTIVE_ANONYMIZE_TTL_DAYS}일): {before.isoformat()}")
    print("(cron 은 last_active_at 이 이 경계보다 이른 + 아직 익명화 안 된 사용자를 익명화한다)")
    print()

    users = await UserRepo(session).list_inactive_for_anonymization(before=before)
    if not users:
        print("익명화 대상 0건 — 지금 켜면 첫 04:00 에 아무도 익명화되지 않는다.")
        return

    print(f"익명화 대상 사용자: {len(users)}명")
    print()
    print(f"{'email':40s} {'마지막 활동':12s} {'경과':>6s}  onboarding")
    for u in sorted(users, key=lambda u: u.last_active_at):
        last = to_kst(u.last_active_at)
        days = (now - last).days
        print(f"  {u.email[:38]:38s} {last.date()!s:12s} {days:4d}일  {u.onboarding_state}")
    print()
    print("이들에게 일어날 일 (POST /settings/anonymize 와 동일):")
    print("  - *_encrypted 컬럼 6종 → '[anonymized]' 로 덮어쓰기 (회고 메모·실패 메모·")
    print("    인박스 원문·LLM 입출력 요약·캘린더 토큰·중단 사유 메모)")
    print("  - users.name → '[anonymized]',  is_anonymized=true,  anonymized_at=now")
    print("  - users.email 은 **안 건드린다** (로그인 1차 키 — 마스킹하면 사실상 계정 삭제)")
    print()
    print("⚠️ 마스킹은 되돌릴 수 없다 — 원문을 덮어쓰므로 복구 셀렉터가 없다.")
    print("   켜기 전에 위 명단이 '정말 90일 넘게 안 온 사람들' 이 맞는지 확인할 것.")


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await _preview(session)
        await session.rollback()  # 읽기 전용 — 방어적으로 명시


if __name__ == "__main__":
    print(f"[preview-anonymize-inactive] {to_kst(now_kst()).isoformat()} — READ-ONLY")
    asyncio.run(_main())
