"""비싼 엔드포인트 사용자별 일일 호출 횟수 상한 (#325, FE #237 §8).

`llm_budget.py`(전역/사용자 **토큰** 예산)와는 다른 축이다 — 그쪽은 "AI 가 오늘 예산을
다 써서" 조용히 룰 폴백으로 내려가는 신호(Tool Executor 가 잡아서 처리)고, 이건 "이
사용자가 오늘 이 기능을 너무 많이 눌러서" 막는 신호다. 후자는 룰 폴백으로 대신할 수
없다 — 매번 진짜로 실행(오케스트레이터 상태 전이, LLM 호출 시도)되는 것 자체를 막아야
하므로, **라우터가 오케스트레이터를 부르기 전에** 이 가드를 통과시킨다(Tool Executor
안이 아니다). 초과 시 429 — 자동으로 룰 결과를 내려주지 않고 명시 거절한다(#325 팀 결정:
전역 토큰 예산 초과와 달리 이건 "언제 다시 되는지 아는 게 나은" 상황이라고 판단).

데이터 출처는 `llm_runs`(기존 테이블, `llm_budget.py` 와 공유) — 새 카운터 테이블을
안 만든다. 그 모듈이 호출 시도마다(성공이든 룰 폴백이든) 정확히 1행을 남기므로, 이
COUNT 는 "오늘 이 엔드포인트를 몇 번 실제로 실행했는가"의 정확한 사후 지표다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from http import HTTPStatus

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reaction_backend.config import get_settings
from reaction_backend.db.models.llm_run import LlmRun
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.errors import ApiError, ErrorCode


class EndpointCallLimitExceeded(RuntimeError):
    """사용자별 엔드포인트 일일 호출 상한 초과. 라우터가 잡아서 429 로 변환한다."""

    def __init__(self, module: str, used: int, limit: int) -> None:
        super().__init__(
            f"daily call limit exceeded for module={module}: used={used}, limit={limit}"
        )
        self.module = module
        self.used = used
        self.limit = limit


def seconds_until_kst_midnight(now: datetime | None = None) -> int:
    """지금부터 다음 KST 00:00 까지 남은 초 — `Retry-After` 헤더 값(#325).

    `now` 를 인자로 받는 이유는 순수 함수로 단위 테스트하기 위함(기본값은 `now_kst()`).
    """
    current = now if now is not None else now_kst()
    tomorrow_midnight = (current + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(int((tomorrow_midnight - current).total_seconds()), 0)


async def _used_calls_today(session: AsyncSession, *, user_id: uuid.UUID, module: str) -> int:
    """KST 기준 오늘 0시부터 이 user_id × module 로 `llm_runs` 에 쌓인 행 수."""
    start_of_day_kst = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(func.count())
        .select_from(LlmRun)
        .where(
            LlmRun.created_at >= start_of_day_kst,
            LlmRun.user_id == user_id,
            LlmRun.module == module,
        )
    )
    result = await session.execute(stmt)
    return int(result.scalar_one() or 0)


async def check(session: AsyncSession, *, user_id: uuid.UUID, module: str) -> None:
    """비싼 엔드포인트 사용자별 일일 호출 한도 (#325). 오케스트레이터 호출 **전에** 부른다.

    `LLM_ENDPOINT_DAILY_CALL_LIMIT` <= 0 이면 무제한(다른 예산 가드들과 같은 관례).
    """
    limit = get_settings().llm_endpoint_daily_call_limit
    if limit <= 0:
        return
    used = await _used_calls_today(session, user_id=user_id, module=module)
    if used >= limit:
        raise EndpointCallLimitExceeded(module=module, used=used, limit=limit)


# 사람이 읽는 기능 이름 — 429 메시지용. `LLM_MODULE_VALUES` 전부를 다루되(#325 는 이 중
# interview/planning/recovery 만 대상으로 지정했다), 라벨 자체는 어느 module 이 와도
# 안전하게 대응하도록 전부 채워 둔다.
_MODULE_LABEL_KO = {
    "interview": "인터뷰",
    "planning": "계획·만다라트 생성",
    "brief": "모닝 브리프",
    "recovery": "회복 제안",
    "inbox": "인박스 자료 추천",
}


async def enforce(session: AsyncSession, *, user_id: uuid.UUID, module: str) -> None:
    """`check()` 를 부르고 초과 시 바로 `ApiError`(429 + `Retry-After`) 로 변환한다.

    라우터가 오케스트레이터를 부르기 **직전**(가능하면 lock 획득보다도 먼저)에 호출한다
    — 어차피 거절할 요청을 위해 lock 대기·오케스트레이터 상태 전이를 시작할 이유가 없다.
    """
    try:
        await check(session, user_id=user_id, module=module)
    except EndpointCallLimitExceeded as exc:
        retry_after = seconds_until_kst_midnight()
        label = _MODULE_LABEL_KO.get(module, module)
        raise ApiError(
            ErrorCode.RATE_LIMIT_DAILY_CALLS_EXCEEDED,
            f"오늘 {label} 요청을 너무 많이 하셨어요. 내일 다시 시도해 주세요.",
            http_status=HTTPStatus.TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        ) from exc
