"""비싼 엔드포인트 사용자별 일일 호출 횟수 상한 (#325, FE #237 §8).

`llm_budget.py`(전역/사용자 **토큰** 예산)와는 다른 축이다 — 그쪽은 "AI 가 오늘 예산을
다 써서" 조용히 룰 폴백으로 내려가는 신호(Tool Executor 가 잡아서 처리)고, 이건 "이
사용자가 오늘 이 기능을 너무 많이 눌러서" 막는 신호다. 후자는 룰 폴백으로 대신할 수
없다 — 매번 진짜로 실행(오케스트레이터 상태 전이, LLM 호출 시도)되는 것 자체를 막아야
하므로, **라우터가 오케스트레이터를 부르기 전에** 이 가드를 통과시킨다(Tool Executor
안이 아니다). 초과 시 429 — 자동으로 룰 결과를 내려주지 않고 명시 거절한다(#325 팀 결정:
전역 토큰 예산 초과와 달리 이건 "언제 다시 되는지 아는 게 나은" 상황이라고 판단).

데이터 출처는 `llm_runs`(기존 테이블, `llm_budget.py` 와 공유) — 새 카운터 테이블을
안 만든다.

⚠️ **행을 세면 안 된다. 요청을 세야 한다 (#370).** 처음엔 이 COUNT 가 행 수였다. 그런데
`llm_runs` 는 **LLM 호출 1회당** 1행이고 엔드포인트 1회는 호출을 여러 번 한다:

| 엔드포인트 1회 | llm_runs 행 |
| --- | --- |
| `POST /interview/.../answers` (칩 답) | 2 |
| `POST /interview/.../answers` (자유서술) | 3 |
| `POST /plans/generate` | 최대 7 (`MAX_REPLAN=2`) |

그래서 상한 20 은 "20회 실행"이 아니라 **인터뷰 8턴**이었고, 필수 슬롯 18개짜리 계획
인터뷰(완주에 49콜)를 **아무도 끝낼 수 없었다** — 신규 사용자의 온보딩이 통째로 막혔다.

지금은 `COUNT(DISTINCT trace_id)` 로 **요청**을 센다. `observability/correlation` 미들웨어가
요청마다 trace_id 를 심고 그 요청 안의 모든 LLM 호출이 같은 값을 달기 때문에, 호출자가
LLM 을 몇 번 부르든 상한이 조여지지 않는다. 노드를 하나 더 붙였다고 상한이 25% 줄어드는
결합을 끊는 것이 요점이다(`harvest_slots` 추가로 2콜→3콜이 된 게 이번 사고의 경로다).

trace_id 가 NULL 인 행(요청 밖 — cron·스크립트, 그리고 이 수정 이전에 쌓인 과거 행)은
`COALESCE` 로 **각각 1회로** 센다. NULL 을 그냥 무시하면 `COUNT(DISTINCT)` 가 그 경로를
전부 0 으로 세어 **가드가 조용히 꺼진다** — 틀리더라도 과거처럼 빡빡한 쪽으로 틀린다.

⚠️ **module 만으로 세면 상한과 무관한 호출까지 센다 (llm-4).** `planning` module 에는 상한을
거는 엔드포인트(`/plans/generate`·`/mandala/subgoals`·`/mandala/generate`·`/mandala/next-cycle`·
`/replan`) 말고도 상한을 **안 거는** 호출이 같이 기록된다 — 자료 검색(`materials_search`,
그라운딩 예산이 따로 있고 그 예산에 걸려 **호출 없이 거절된 것까지** 행이 남는다),
마일스톤 초안·학습 방식 추천·만다라 링 재생성. 실측: 계획을 한 번도 안 만든 사용자가 자료
검색을 20번 눌렀다는 이유로 `/plans/generate` 가 429 로 막혔다. 그래서 그 호출들의 prompt 는
`_UNGATED_PROMPTS` 로 빼고 센다. 상한을 거는 엔드포인트의 요청은 그대로 1회로 잡힌다 —
분해·검토·만다라 prompt 가 같은 trace 에 남기 때문이다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any

from sqlalchemy import String, cast, distinct, func, not_, or_, select
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


# 상한을 걸지 않는 엔드포인트가 같은 module 로 남기는 prompt (llm-4, 모듈 독스트링).
# `llm_runs.prompt_id` 는 보통 버전 없는 id 지만 프롬프트 렌더 실패 행은 호출부가 넘긴
# `...@v2` 모양 그대로라 둘 다 뺀다.
_UNGATED_PROMPTS: dict[str, tuple[str, ...]] = {
    "planning": (
        "planning/materials_search",  # POST /plans/materials/search — 그라운딩 예산이 따로 있다
        "planning/plan_milestones",  # POST /plans/milestones
        "planning/study_method",  # POST /plans/materials/study-method
        "planning/mandala_cells_branch",  # POST /plans/mandala/{id}/regenerate-branch
    ),
}


def _counted_prompt_filter(module: str) -> list[Any]:
    ungated = _UNGATED_PROMPTS.get(module, ())
    if not ungated:
        return []
    excluded = or_(
        LlmRun.prompt_id.in_(ungated),
        *(LlmRun.prompt_id.like(f"{p}@%") for p in ungated),
    )
    # prompt_id 가 NULL 인 행은 어디서 왔는지 모른다 — 빡빡한 쪽으로(센다).
    return [or_(LlmRun.prompt_id.is_(None), not_(excluded))]


async def _used_calls_today(session: AsyncSession, *, user_id: uuid.UUID, module: str) -> int:
    """KST 기준 오늘 0시부터 이 user_id × module 이 이 엔드포인트를 **실행한 횟수**.

    행 수가 아니라 `DISTINCT trace_id` 다 — 이유는 모듈 독스트링 참조 (#370).
    trace_id 가 NULL 인 행은 `id` 로 대체해 각각 1회로 센다(요청 밖 호출·과거 행).
    상한을 걸지 않는 엔드포인트의 prompt 는 뺀다(`_UNGATED_PROMPTS`, llm-4).
    """
    start_of_day_kst = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    invocation_key = func.coalesce(LlmRun.trace_id, cast(LlmRun.id, String))
    stmt = (
        select(func.count(distinct(invocation_key)))
        .select_from(LlmRun)
        .where(
            LlmRun.created_at >= start_of_day_kst,
            LlmRun.user_id == user_id,
            LlmRun.module == module,
            *_counted_prompt_filter(module),
        )
    )
    result = await session.execute(stmt)
    return int(result.scalar_one() or 0)


async def check(session: AsyncSession, *, user_id: uuid.UUID, module: str) -> None:
    """비싼 엔드포인트 사용자별 일일 호출 한도 (#325). 오케스트레이터 호출 **전에** 부른다.

    `LLM_ENDPOINT_DAILY_CALL_LIMIT` <= 0 이면 무제한(다른 예산 가드들과 같은 관례).
    상한은 module 별로 다르다 (#370) — `config.endpoint_call_limit_for_module` 참조.
    """
    limit = get_settings().endpoint_call_limit_for_module(module)
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
            # 사용자를 탓하지 않는다('너무 많이 하셨어요' → 준비된 횟수를 다 썼다, llm-4).
            f"오늘 준비된 {label} 횟수를 다 썼어요. 내일 다시 열려요.",
            http_status=HTTPStatus.TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        ) from exc
