"""habit_instances 주간 생성 cron — S27 주별 인스턴스 (Issue #22 / #24).

`habit_instances` 는 (habit, 주) 하나당 한 행인 **그 주의 카운터**(`target_count` /
`done_count`)다. 행이 없으면 그 주는 존재하지 않는 주가 된다 — 소비자 셋이 전부
`week_start` 등가 비교이기 때문:

- `GET /today/agenda`(`api/routes/today.py`)가 `list_for_user_week(user_id,
  current_week_start_kst())` 로 읽는다 → 행이 없으면 습관 섹션이 통째로 빈다.
- `POST /habit-instances/{id}/check` 는 **존재하는** 인스턴스의 `done_count` 만 올린다 →
  체크할 대상 자체가 없다.
- S22 빈도 재설계(`orchestrator/habit_penalty.evaluate_penalty`)는 **연속 3주** 인스턴스를
  요구한다 → 1행뿐이면 `len < 3` 에서 영영 `None`.

그동안 유일한 생성자는 `POST /habits`(`api/routes/habits.py`)의 **등록한 그 주 1행**이었고,
핸들러 docstring 이 스스로 "cron 도입 전 임시"라고 적어 뒀다. 그래서 등록 다음 주부터
습관 트랙이 통째로 사라졌고, §1.4 잠금("3주 연속 미달 → 비난 없는 빈도 재설계")은 판정
로직·`apply_penalty`·`/reviews/habit-penalty` 가 전부 완성돼 있는데 **먹일 데이터가 없어
한 번도 발화하지 못했다.** 우회로도 없다 — 주간 계획이 만든 습관 블록은 승인 시
`orchestrator/first_plan_adapter.py` 가 "habit 등은 별도 경로"로 버리는데, 그 별도 경로가
바로 이 sweep 이다.

멱등: `(habit_id, week_start)` UNIQUE + `create_or_get_for_week` 의 get-or-create 라 같은 주를
몇 번 돌려도 1행이다 (AGENTS §2 "cron 은 idempotent"에 추가 코드가 필요 없다).

`target_count` 는 `habit.target_count` 를 읽는다 — 컬럼 정의 자체가 "이번 주 목표 횟수"이고
S22 수락이 재설계 결과를 쓰는 자리다(`habit_repo.apply_penalty`). 지금은 `create` /
`update` / `apply_penalty` 세 곳이 `frequency_per_week` 와 항상 함께 쓰므로 값이 같지만,
의미상 이쪽이 원본이다 — 페널티를 되살리려고 만든 cron 이 페널티 결과를 안 읽으면 앞뒤가
맞지 않는다.

트랜잭션 규약은 `notify_sweeps.py` 와 같다 — **사용자 단위 commit + except 에서 rollback**.
배치 말미 일괄 commit 은 한 사용자 실패로 그 주 전원의 인스턴스를 잃고, rollback 이 없으면
DB 예외로 aborted 된 세션이 뒤따르는 사용자를 전부 `PendingRollbackError` 로 죽여 실패
격리가 허상이 된다. 순회도 같은 이유로 **미리 떠 둔 사용자 id** 로 한다 — rollback 이 만료시킨
ORM 객체의 `user.id` 를 다시 읽으면 비동기 세션이 `MissingGreenlet` 로 죽는다.

`week_start` 는 호출자가 주입한다 — 런타임 job 이 `GET /today/agenda` 와 **같은 함수**
(`habit_repo.current_week_start_kst`)를 쓰게 해서 생성과 조회가 어긋날 수 없게 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from reaction_backend.repositories.habit_instance_repo import HabitInstanceRepo
    from reaction_backend.repositories.habit_repo import HabitRepo
    from reaction_backend.repositories.user_repo import UserRepo

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class HabitSweepResult:
    """sweep 결과 — 관측/로그용.

    `created` 는 **이번 실행에서 새로 만든 행 수**다. 정상 주기라면 월요일 첫 실행에만
    양수이고 그 뒤 재실행은 0 — "cron 이 돌긴 했는데 아무것도 안 했다"와 "cron 이 아예
    안 돌았다"를 로그에서 구분하는 유일한 신호다.
    """

    total: int
    ok: int
    failed: int
    created: int


async def run_habit_instances_sweep(
    week_start: date,
    *,
    user_repo: UserRepo,
    habit_repo: HabitRepo,
    instance_repo: HabitInstanceRepo,
    session: AsyncSession,
) -> HabitSweepResult:
    """활성 사용자 × 활성 습관의 `week_start` 주 인스턴스를 생성(idempotent).

    이미 있으면 건너뛴다 — `done_count` 가 쌓인 행을 절대 덮어쓰지 않기 위해 `get_for_week`
    로 먼저 확인한다(`create_or_get_for_week` 도 같은 보장을 하지만, 여기서 확인해야
    `created` 를 셀 수 있고 이미 있는 주에는 INSERT 를 시도조차 안 한다).
    """
    users = await user_repo.list_active()
    user_ids = [u.id for u in users]  # rollback 뒤에도 읽을 수 있게 원시값으로 (모듈 docstring)
    ok = failed = created = 0
    for user_id in user_ids:
        try:
            made = 0
            for habit in await habit_repo.list_active(user_id):
                if await instance_repo.get_for_week(habit.id, week_start) is not None:
                    continue
                await instance_repo.create_or_get_for_week(
                    habit_id=habit.id,
                    week_start=week_start,
                    target_count=habit.target_count,
                )
                made += 1
            # 사용자 단위 commit — 뒤 사용자의 실패가 앞 사용자의 인스턴스를 되돌리지 않게.
            await session.commit()
            # commit 이 끝난 뒤에 센다 — 도중에 실패해 rollback 된 행을 `created` 에 넣으면
            # "만들었다"는 로그가 거짓이 된다.
            created += made
            ok += 1
        except Exception:  # noqa: BLE001 — 한 사용자 실패가 배치를 멈추지 않게
            failed += 1
            _log.exception("habit_instances sweep failed for user %s", user_id)
            await session.rollback()  # aborted 세션이 다음 사용자를 전멸시키지 않게
    if created or failed:
        _log.info(
            "habit_instances sweep: week_start=%s total=%d ok=%d failed=%d created=%d",
            week_start,
            len(user_ids),
            ok,
            failed,
            created,
        )
    return HabitSweepResult(total=len(user_ids), ok=ok, failed=failed, created=created)
