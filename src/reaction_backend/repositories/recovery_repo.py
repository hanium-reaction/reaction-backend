"""Recovery repository — S19/S20 (Issue #20-A).

규칙:
- user_id scope 자동 (execution / attempt 조회).
- 원본 `action_item.status` 는 본 repo 가 절대 건드리지 않는다 (AGENTS.md §2).
- commit 은 호출자 책임.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Annotated, cast
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.db.models.action_item import ActionItem
from reaction_backend.db.models.execution_event import ExecutionEvent
from reaction_backend.db.models.execution_failure_tag import ExecutionFailureTag
from reaction_backend.db.models.recovery_attempt import (
    RECOVERY_SUCCESS_STATUSES,
    RecoveryAttempt,
)
from reaction_backend.db.models.recovery_strategy_catalog import RecoveryStrategyCatalog
from reaction_backend.db.models.scheduled_block import ScheduledBlock
from reaction_backend.db.session import get_db

# 회고 창의 단일 기준식 — `abandon_stale` 이 만료 cron 과 **같은 식**을 써야 한다(#20).
from reaction_backend.repositories.execution_repo import reflectable_from
from reaction_backend.schemas.common import KST

# `expire_undecided` 가 자동 종결한 카드의 표식 — 사용자가 직접 쓴 거절 사유(`decision_reason`)
# 와 섞이지 않게 접두어를 둔다. FE 는 이 값을 화면에 표시하지 않는다(RecoveryCard 응답에
# decision_reason 필드 자체가 없다) — 순전히 내부/운영 조회용.
_UNDECIDED_EXPIRY_REASON = "system: 회고 창 밖 — 결정 없이 자동 정리"

if TYPE_CHECKING:
    from reaction_backend.orchestrator.escalation import (
        ExecutionOutcome,
        RecoveryDecisionOutcome,
        RecoveryResultOutcome,
    )


class RecoveryRepo:
    """ExecutionEvent 조회 + RecoveryAttempt 영속화 + 전략 카탈로그."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_execution(self, user_id: UUID, execution_id: UUID) -> ExecutionEvent | None:
        stmt = select(ExecutionEvent).where(
            ExecutionEvent.id == execution_id,
            ExecutionEvent.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_failure_tag_codes(self, execution_id: UUID) -> list[str]:
        stmt = select(ExecutionFailureTag.tag_code).where(
            ExecutionFailureTag.execution_id == execution_id
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_lineage_outcomes_for_tag(
        self,
        user_id: UUID,
        action_item_id: UUID,
        tag_code: str,
        *,
        limit: int = 20,
    ) -> list[ExecutionOutcome]:
        """L2 에스컬레이션(근거 대장 §5.2 "동일 (계보, tag_code)") 이력 — 시간 역순.

        **"계보"의 정의**: `recovery-evidence-base.md` §5.16 의 `recovery_followthrough_rate`
        (PARK) 계산 SQL이 이미 "같은 goal 계보"를 `a4.goal_id = orig_a.goal_id` 로 구현해
        둔 것과 같은 뜻으로 쓴다 — 같은 `goal_id` 를 가진 action_item 전체. `goal_id` 가
        없는 카드(습관/인박스/수동)는 "계보가 없어" 자기 자신 하나만(같은 SQL이 이 경우를
        "항상 미완주"로 두는 것과 같은 이유 — goal 없이는 계보를 정의할 방법이 없다).

        **tag_code 가 다른 `failed` 를 만나면**: 이 태그 관점에서는 "무관한 사건"이라
        `partial_done` 과 같은 **동결**로 접어(카운트도 리셋도 안 함)
        `orchestrator.escalation.compute_consecutive_failure_count` 로 그대로 흘려보낸다
        — done/over_done 이 아닌 이상 "다른 태그로 실패했다"는 사실 자체가 "이 태그로는
        아직 실패도 성공도 안 했다"는 뜻이기 때문이다.

        회복으로 파생된 카드까지 재귀적으로 잇는 계보 그래프는(`orchestrator/escalation.py`
        모듈 docstring이 이미 명시한 대로) 이번 스코프 밖이다 — goal_id 단위 근사.
        """
        action = await self._session.get(ActionItem, action_item_id)
        if action is None or action.user_id != user_id:
            return []

        lineage_ids = (
            select(ActionItem.id).where(
                ActionItem.user_id == user_id, ActionItem.goal_id == action.goal_id
            )
            if action.goal_id is not None
            else select(ActionItem.id).where(ActionItem.id == action_item_id)
        )

        tag_matched = (
            select(ExecutionFailureTag.id)
            .where(
                ExecutionFailureTag.execution_id == ExecutionEvent.id,
                ExecutionFailureTag.tag_code == tag_code,
            )
            .exists()
        )
        stmt = (
            select(ExecutionEvent.completion_status, tag_matched)
            .where(
                ExecutionEvent.user_id == user_id,
                ExecutionEvent.action_item_id.in_(lineage_ids),
                ExecutionEvent.completion_status != "in_progress",
            )
            # `plan_start_at` — 언제 그 실행이 실제로 벌어졌는가(`created_at` 은 INSERT 시각일
            # 뿐이고, 같은 트랜잭션 안에서 여러 행이 들어가면 `now()` 가 전부 같은 값을 줘
            # 순서가 안정적이지 않다). `list_pending_reflection` 등 이 모듈 밖에서도 실행
            # 시각 정렬은 이미 `plan_start_at` 기준(execution_repo.py) — 같은 관례.
            .order_by(ExecutionEvent.plan_start_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()

        outcomes: list[ExecutionOutcome] = []
        for completion_status, matched in rows:
            if completion_status == "failed" and not matched:
                outcomes.append("partial_done")
            else:
                outcomes.append(cast("ExecutionOutcome", completion_status))
        return outcomes

    async def list_same_card_outcomes(
        self,
        user_id: UUID,
        action_item_id: UUID,
        *,
        limit: int = 20,
    ) -> list[ExecutionOutcome]:
        """L1 에스컬레이션(근거 대장 §5.2 "동일 카드 2회 연속 실패")용 이력 — 시간 역순.

        `list_lineage_outcomes_for_tag` 와 달리 계보·태그 무관 — 이 action_item_id **자기
        자신**의 실행 이력만 그대로 본다. `plan_start_at` 정렬 이유는 그쪽과 동일.
        """
        stmt = (
            select(ExecutionEvent.completion_status)
            .where(
                ExecutionEvent.user_id == user_id,
                ExecutionEvent.action_item_id == action_item_id,
                ExecutionEvent.completion_status != "in_progress",
            )
            .order_by(ExecutionEvent.plan_start_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return cast("list[ExecutionOutcome]", list(result.scalars().all()))

    async def list_recovery_results(
        self,
        user_id: UUID,
        *,
        limit: int = 20,
    ) -> list[RecoveryResultOutcome]:
        """L1 에스컬레이션(근거 대장 §5.2 "회복 1회 abandoned")용 이력 — 시간 역순.

        §5.1 상태 변수 표는 `consecutive_failure_count`/`same_tag_failure_count` 만
        "동일 카드"/"동일 (계보,tag_code)" 로 명시한다 — `recovery_abandoned_streak` 는
        그런 한정이 없어, 이 사용자의 **회복 결정 전체**(카드 무관)에서 본다: "최근 회복을
        연달아 완주 못 하고 있는가"라는 사용자 단위 신호로 읽었다. `recovery_decided_at`
        기준 정렬 — 결정(수락) 시점이 이 흐름의 자연스러운 시간축이고(`recovery_started_at`
        도 같은 시각을 쓴다 — `create_attempt`/`_adopt` 참고), `recovery_result` 가
        `pending` 인(아직 결정 안 됐거나 결정됐지만 안 끝난) 행은 애초에 제외한다.
        """
        stmt = (
            select(RecoveryAttempt.recovery_result)
            .where(
                RecoveryAttempt.user_id == user_id,
                RecoveryAttempt.recovery_result != "pending",
            )
            .order_by(RecoveryAttempt.recovery_decided_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return cast("list[RecoveryResultOutcome]", list(result.scalars().all()))

    async def list_goal_outcomes(
        self,
        user_id: UUID,
        goal_id: UUID,
        *,
        limit: int = 20,
    ) -> list[ExecutionOutcome]:
        """L3 에스컬레이션(근거 대장 §5.2 "동일 goal 4회 연속 실패")용 이력 — 시간 역순.

        `list_lineage_outcomes_for_tag`(L2)와 달리 **태그 무관** — 같은 goal 산하 전체
        action_item 의 실행 이력을 그대로 본다("다른 태그 실패는 동결" 트릭이 없다, 그건
        "동일 (계보,tag_code)" 전용). `list_same_card_outcomes`(L1, "동일 카드")보다
        넓고 `list_lineage_outcomes_for_tag`(L2, "동일 (계보,tag_code)")보다 좁지 않은
        — 계보 전체를 태그로 좁히지 않는 — 세 번째 모집단이다.
        """
        stmt = (
            select(ExecutionEvent.completion_status)
            .join(ActionItem, ActionItem.id == ExecutionEvent.action_item_id)
            .where(
                ExecutionEvent.user_id == user_id,
                ActionItem.goal_id == goal_id,
                ExecutionEvent.completion_status != "in_progress",
            )
            .order_by(ExecutionEvent.plan_start_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return cast("list[ExecutionOutcome]", list(result.scalars().all()))

    async def list_recovery_decisions(
        self,
        user_id: UUID,
        *,
        limit: int = 20,
    ) -> list[RecoveryDecisionOutcome]:
        """L3 에스컬레이션(근거 대장 §5.2 "회복 2회 연속 rejected")용 이력 — 시간 역순.

        `list_recovery_results`(abandoned_streak)와 같은 스코프 — 카드/goal 무관,
        이 사용자의 **회복 결정 전체**에서 본다. `pending`(아직 결정 안 됨)은 제외.
        """
        stmt = (
            select(RecoveryAttempt.user_decision)
            .where(
                RecoveryAttempt.user_id == user_id,
                RecoveryAttempt.user_decision != "pending",
            )
            .order_by(RecoveryAttempt.recovery_decided_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return cast("list[RecoveryDecisionOutcome]", list(result.scalars().all()))

    async def list_active_strategies(self) -> list[RecoveryStrategyCatalog]:
        stmt = (
            select(RecoveryStrategyCatalog)
            .where(RecoveryStrategyCatalog.is_active.is_(True))
            .order_by(RecoveryStrategyCatalog.display_priority)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_attempts(self, user_id: UUID, execution_id: UUID) -> list[RecoveryAttempt]:
        """이 실행의 회복 카드 — **순서가 고정돼야 한다.**

        ⚠️ 예전엔 `ORDER BY created_at` 하나였다. 한 세트는 **같은 트랜잭션**에서 만들어져
        `created_at` 이 마이크로초까지 같으므로(실측: 두 건 모두 `21:25:58.352381`) 동점이
        되고, 그러면 Postgres 는 순서를 보장하지 않는다. 같은 요청이 매번 다른 순서로 왔다.

        그게 화면에서 뜻하는 것: FE 는 **목록의 첫 카드**에 "추천" 배지를 붙인다
        (`RecoveryScreen.tsx` — `recommended={i === 0}`). 즉 **새 정보 없이 새로고침만으로
        추천이 뒤집혔다.** 추천은 제품이 내리는 판단이라, 흔들리면 그 판단을 못 믿게 된다.

        정렬 기준은 룰이 이미 쓰는 것과 같게 뒀다:

        1. `created_at` — 세트가 여러 번 생성됐으면 옛 세트가 먼저.
        2. **`trigger_tag` 가 있는 것 먼저** — 실패 태그에 매칭된 전략이 `select_strategies`
           의 1순위다(매칭 없이 패딩된 카드는 `trigger_tag` 가 비어 있다). "왜 실패했는지에
           맞는 카드"가 추천이 되는 게 옳다.
        3. `display_priority` — 카탈로그가 정한 순서. `select_strategies` 의 동점 처리와 같다.
        4. `id` — 위가 다 같아도 결과가 흔들리지 않게 하는 최종 고정핀.
        """
        stmt = (
            select(RecoveryAttempt)
            .outerjoin(
                RecoveryStrategyCatalog,
                RecoveryStrategyCatalog.strategy_type == RecoveryAttempt.recovery_strategy_type,
            )
            .where(
                RecoveryAttempt.execution_id == execution_id,
                RecoveryAttempt.user_id == user_id,
            )
            .order_by(
                RecoveryAttempt.created_at,
                RecoveryAttempt.trigger_tag.is_(None),
                RecoveryStrategyCatalog.display_priority,
                RecoveryAttempt.id,
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_due_re_engagement(
        self, user_id: UUID, target_date: date
    ) -> list[RecoveryAttempt]:
        """오늘이 `re_engagement_anchor_at` 날짜인 채택 PARK/CARRY_OVER — T2 알림 대상.

        `_adopt()`(routes/recovery.py) 가 채택(accepted/edited) 시에만 anchor 를 채우므로
        `IS NOT NULL` 만으로 이미 "채택된 PARK/CARRY_OVER" 로 좁혀진다 — 별도 decision
        필터가 필요 없다. KST 달력일 경계로 잰다 — 발송 게이트의 class_dedup 과 같은 기준
        (근거 대장 §6.2 T2).
        """
        start = datetime.combine(target_date, time.min, tzinfo=KST)
        end = start + timedelta(days=1)
        stmt = select(RecoveryAttempt).where(
            RecoveryAttempt.user_id == user_id,
            RecoveryAttempt.re_engagement_anchor_at >= start,
            RecoveryAttempt.re_engagement_anchor_at < end,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_strategy(self, strategy_type: str) -> RecoveryStrategyCatalog | None:
        """활성 전략 1건. 카탈로그 전체를 긁어 파이썬에서 찾던 것을 대체한다.

        ⚠️ `is_active` 필터를 빼지 말 것 — 비활성 전략이면 None 이 나와 호출자가 기본
        회복 단위(5분)로 떨어지는 것이 의도다. 필터를 빼면 새 카드의 estimated_minutes 가
        비활성 전략의 min_recovery_unit_minutes 로 바뀐다.
        """
        stmt = select(RecoveryStrategyCatalog).where(
            RecoveryStrategyCatalog.strategy_type == strategy_type,
            RecoveryStrategyCatalog.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_attempt(
        self,
        *,
        user_id: UUID,
        execution_id: UUID,
        option_group: str,
        strategy_type: str,
        suggested_action_text: str,
        trigger_tag: str | None,
        llm_fallback_used: bool,
        prompt_version: str | None = None,
        obstacle: str | None = None,
        coping_clause: str | None = None,
        acknowledgment: str | None = None,
    ) -> RecoveryAttempt:
        attempt = RecoveryAttempt(
            user_id=user_id,
            execution_id=execution_id,
            recovery_option_group=option_group,
            recovery_strategy_type=strategy_type,
            suggested_action_text=suggested_action_text,
            trigger_tag=trigger_tag,
            llm_fallback_used=llm_fallback_used,
            prompt_version=prompt_version,
            obstacle=obstacle,
            coping_clause=coping_clause,
            acknowledgment=acknowledgment,
        )
        self._session.add(attempt)
        await self._session.flush()
        await self._session.refresh(attempt)
        return attempt

    async def stamp_first_viewed(
        self, attempts: list[RecoveryAttempt], viewed_at: datetime
    ) -> None:
        """카드가 API 응답으로 나가는 시점에 `first_viewed_at` 을 최초 1회만 채운다 (P4/P6).

        "노출"의 근사치다 — 이 시점에 응답이 만들어졌다는 것뿐, 클라이언트가 실제로
        받아 렌더링했는지는 FE 계측 없이는 모른다(그래도 지금의 "생성됨" 분모보다는
        "노출 시도됨"에 가깝다). 같은 pending 카드가 멱등 재호출로 다시 나가도 최초
        1회만 스탬프하고 그 뒤엔 건드리지 않는다 — 그래서 이름이 first.

        commit 은 호출자 책임(이 repo 의 다른 쓰기 메서드와 같은 관례).
        """
        for a in attempts:
            if a.first_viewed_at is None:
                a.first_viewed_at = viewed_at

    async def complete_for_action(
        self,
        user_id: UUID,
        action_item_id: UUID,
        *,
        completed_at: datetime,
        completion_status: str,
    ) -> RecoveryAttempt | None:
        """회복 카드의 실행이 종결되면 그 RecoveryAttempt 에 완료 스탬프 (#20).

        **average_recovery_minutes 의 유일한 생산자.** 회복을 채택하면(ADOPTED) 새 카드가
        생기고(`resulting_action_item_id`), 그 카드를 done/over_done 으로 마치면 여기서
        `recovery_completed_at` + `recovery_duration_minutes`(= completed_at −
        `recovery_started_at`, 결정 시각) + `recovery_result='completed'` 를 기록한다.
        `recovery_started_at` 이 결정 시각이므로 duration 은 **결정→회복 완주 경과 시간**이다
        (설계서 §5.16 "종료 시각 − 시작 시각"; CARRY_OVER 는 하루 넘겨 큰 값이 정상).

        failed·partial_done 은 `result='abandoned'` (duration 없음 → 평균에서 제외).
        멱등 — 이미 종결된(`result != 'pending'`) attempt 는 재체크인으로 덮지 않는다.
        `resulting_action_item_id` 는 채택 시에만 채워지므로 그 매칭 자체가 ADOPTED 필터다.

        반환: 스탬프한 attempt (매칭 없거나 이미 종결이면 None — 대다수 카드는 회복이
        아니라 None 이 정상).
        """
        stmt = select(RecoveryAttempt).where(
            RecoveryAttempt.user_id == user_id,
            RecoveryAttempt.resulting_action_item_id == action_item_id,
            RecoveryAttempt.recovery_result == "pending",
        )
        attempt = (await self._session.execute(stmt)).scalar_one_or_none()
        if attempt is None:
            return None

        if completion_status in RECOVERY_SUCCESS_STATUSES:
            attempt.recovery_result = "completed"
            attempt.recovery_completed_at = completed_at
            if attempt.recovery_started_at is not None:
                delta = completed_at - attempt.recovery_started_at
                attempt.recovery_duration_minutes = max(int(delta.total_seconds() // 60), 0)
        else:
            attempt.recovery_result = "abandoned"
        await self._session.flush()
        return attempt

    async def abandon_stale(self, *, before: datetime) -> int:
        """회고 창을 벗어났는데 아직 완주 안 된 회복을 포기 처리. 반환: 처리 건수 (#20).

        **왜 필요한가**: `complete_for_action` 은 회복 카드를 **체크인했을 때만** 스탬프한다.
        그래서 (a) 시작만 하고 체크인을 잊은 회복과 (b) **시작조차 안 한** 회복은 영영
        `result='pending'`(아직 진행 중) 으로 남는다 — 특히 (b) 는 execution 이 없어
        `expire_unreflected` 대상도 아니다(그건 `in_progress` 실행만 본다).

        **경계도 기준식도 만료 cron과 같은 단일 소스** — 경계값은
        `pending_reflection_since(오늘)`, 기준식은 `execution_repo.reflectable_from()`.
        예전엔 경계'값'만 공유하고 실제로 재는 **대상**은 `ActionItem.target_date` 였다.
        그 비대칭이 데이터를 파괴했다: `find_open_block` 에 날짜 필터가 없어 지난 카드를
        뒤늦게 [▶시작] 하면 `target_date` 는 창 밖인데 회고는 아직 가능하다. 그 상태에서
        여기가 'abandoned' 로 확정해 버리면, 이후 사용자가 실제로 그 회복을 done 으로
        마쳐도 `complete_for_action` 의 `recovery_result='pending'` 가드에 막혀
        `recovery_duration_minutes` 가 영영 NULL 로 남는다 — **완주한 회복이
        `average_recovery_minutes` 에서 통째로 사라진다**(#20 이 만든 KPI 자체의 손실).

        가드 5개 (전부 데이터 보호 목적):
        1. `recovery_result='pending'` — 멱등. **이미 completed 인 회복을 덮으면 지표가 파괴**된다.
        2. 카드 status 가 완주(done/over_done)면 제외 — `complete_for_action` 이 생기기 전
           데이터나 스탬프를 놓친 경우, 실제로 해낸 회복을 '포기' 로 오염시키지 않는다.
        3. `resulting_action_item_id` 매칭 자체가 채택(ADOPTED) 필터 — 그 컬럼은 채택 시에만 찬다.
        4. **아직 회고할 수 있는 실행이 남은 카드는 제외** — `reflectable_from() >= before`.
           `expire_unreflected` 와 같은 식이라 "사용자가 회고할 수 있는 건 안 건드린다" 가
           두 cron 에서 같은 뜻이 된다.
        5. 경계 이후에 살아있는 블록(scheduled/started)이 남은 카드도 제외 — `_after_block_time`
           이 `plan_start_at` 기준이라 블록 날짜가 `target_date` 와 어긋날 수 있고, S15 이동으로
           일정을 미래로 옮긴 회복도 여기 해당한다. 아직 하기로 되어 있는 회복을 포기로 덮지 않는다.

        ⚠️ `action_item.status` 는 **건드리지 않는다** (AGENTS.md §2) — 포기는
        `recovery_attempts.recovery_result` 에만 기록한다. 전역(모든 사용자) 일괄 — cron 전용.
        """
        # 가드 4 — 회고 창 안의 실행이 하나라도 남았으면 아직 완주할 수 있는 회복이다.
        still_reflectable = (
            select(ExecutionEvent.id)
            .where(
                ExecutionEvent.action_item_id == ActionItem.id,
                reflectable_from() >= before,
            )
            .exists()
        )
        # 가드 5 — 경계 이후에 아직 살아있는 블록이 남은 카드는 '진행 중인 계획'.
        has_live_block = (
            select(ScheduledBlock.id)
            .where(
                ScheduledBlock.action_item_id == ActionItem.id,
                ScheduledBlock.block_status.in_(("scheduled", "started")),
                ScheduledBlock.start_at >= before,
            )
            .exists()
        )
        stale_cards = select(ActionItem.id).where(
            ActionItem.target_date < before.date(),
            ActionItem.status.notin_(RECOVERY_SUCCESS_STATUSES),
            ~still_reflectable,
            ~has_live_block,
        )
        stmt = (
            update(RecoveryAttempt)
            .where(
                RecoveryAttempt.recovery_result == "pending",
                RecoveryAttempt.resulting_action_item_id.in_(stale_cards),
            )
            .values(recovery_result="abandoned")
            .returning(RecoveryAttempt.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        return len(list(result.scalars().all()))

    # ── 결정 자체가 없는 카드 정리 (아래 expire_undecided 전용) ──────────────

    async def expire_undecided(self, *, before: datetime, decided_at: datetime) -> int:
        """회고 창 밖인데 사용자가 한 번도 결정하지 않은 회복 카드를 자동 종결. 반환: 처리 건수.

        **`abandon_stale` 과 다른 문제를 푼다**: 그쪽은 **채택된**(ADOPTED) 회복이 완주되지
        않았을 때만 처리한다(`resulting_action_item_id` 매칭이 곧 채택 필터). 카드가
        노출됐는데도 사용자가 [수락/수정/거절] 중 아무것도 안 누른 경우
        (`user_decision='pending'`)는 그 필터를 아예 안 만나 **영영 pending** 으로 남는다 —
        카드가 만들어진 시점에 이미 그 실행의 `completion_status` 는 failed/partial_done
        (회고가 끝났다는 뜻)인데, 카드 자체의 결정만 무한정 열려 있는 상태다.

        **한 실행의 카드는 항상 함께 움직인다** — `_adopt`/`_reject_siblings`/`_skip_all`
        이 결정 시 그 실행의 pending 카드 **전부**를 같은 트랜잭션에서 갱신한다. 그래서
        이 시점에 `user_decision='pending'` 인 카드는 그 실행의 카드 전부가 pending 이거나
        전부 아니거나 둘 중 하나다(부분 상태가 없다) — `execution_id`/`action_item` 조인 없이
        `created_at` 만으로 판정해도 안전하다.

        **경계는 만료 cron 과 같은 3일 창**(`pending_reflection_since`) — 새 상수를 만들지
        않는다. **`created_at` 부터 재고 `first_viewed_at` 부터 재지 않는 이유**: 노출은
        API 응답이 나간 순간일 뿐 사용자가 실제로 봤다는 보장이 아니고(`stamp_first_viewed`
        docstring), 노출 시각을 기준으로 하면 한 번도 안 열어본 카드는 영원히 이 창을
        못 만난다.

        **새 enum 값을 만들지 않고 `rejected` 로 닫는 이유**: `_reject_siblings` 가 이미
        "사용자가 이 카드를 개별적으로 클릭하지 않았어도 시스템이 `rejected` 로 채운다"는
        선례를 갖고 있다(형제 카드 자동 거절). 새 상태(예: `expired`)를 추가하면 스키마
        마이그레이션 + FE 계약(`RecoveryDecision` literal)까지 같이 바뀌어야 하고,
        `recovery_rejected_streak`(근거 대장 §5.1)처럼 `rejected`/`skipped` 를 "이탈 신호"
        로 묶어 보는 기존 집계와도 끊어진다 — 카드를 열어보지도 않은 것과 열고 거절한
        것을 에스컬레이션 관점에서 다르게 볼 근거가 없다(둘 다 "이 회복이 안 먹혔다").

        ⚠️ `action_item.status`/`recovery_result` 는 건드리지 않는다 — 이 카드는 채택된
        적이 없어 실행할 회복 자체가 없다(`recovery_result` 는 애초에 채택 카드만의 것).
        멱등 — `user_decision='pending'` 가드가 재실행에서 이미 닫힌 카드를 걸러낸다.
        """
        stmt = (
            update(RecoveryAttempt)
            .where(
                RecoveryAttempt.user_decision == "pending",
                RecoveryAttempt.created_at < before,
            )
            .values(
                user_decision="rejected",
                recovery_decided_at=decided_at,
                decision_reason=_UNDECIDED_EXPIRY_REASON,
            )
            .returning(RecoveryAttempt.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        return len(list(result.scalars().all()))


SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_recovery_repo(session: SessionDep) -> RecoveryRepo:
    return RecoveryRepo(session)
