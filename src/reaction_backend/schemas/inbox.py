"""Inbox 도메인 스키마 (api-contract §18) — S24 Life Inbox / S25 Triage."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from reaction_backend.schemas.common import CamelModel

# Inbox 항목 라이프사이클 + category enum (DB 모델과 일치)
InboxStatus = Literal["captured", "classified", "archived", "promoted"]
InboxCategory = Literal["study", "project", "health", "routine", "schedule", "other"]
# 항목 출처 (BE #171) — system 은 목표 카테고리에 맞춰 자동으로 넣어 준 추천 자료.
InboxSource = Literal["user", "system"]
InboxAdviceCategory = Literal["recovery", "today", "goal", "habit"]
InboxAdviceActionType = Literal["OPEN_TODAY", "OPEN_WEEKLY_PLAN", "OPEN_GOAL"]


class InboxAdviceAction(CamelModel):
    """조언을 확인한 사용자가 직접 선택할 수 있는 다음 화면."""

    type: InboxAdviceActionType
    label: str
    target_id: str | None = None


class InboxCoachingAdvice(CamelModel):
    """사용자 기록에서 서버가 산출한 근거 기반 Inbox 조언."""

    advice_id: str
    category: InboxAdviceCategory
    title: str
    body: str
    rationale: str
    evidence: list[str]
    action: InboxAdviceAction
    generated_at: datetime
    source: Literal["rules"] = "rules"
    fallback_used: bool = False


class InboxItem(CamelModel):
    """Inbox 항목 — GET/POST/PATCH/convert-* 응답.

    `raw_text` 는 응답에서 복호화된 평문. DB 는 `raw_text_encrypted` (AES-256-GCM).
    """

    inbox_id: str
    raw_text: str
    ai_category_guess: str | None
    user_category: str | None
    status: str
    promoted_goal_id: str | None
    # 승격 대상 구분 — status=promoted 일 때만 'goal'/'action', 그 외 null.
    # goal 은 promoted_goal_id 로 딥링크, action 은 오늘 실행 화면으로 이동.
    promoted_to: Literal["goal", "action"] | None = None
    # 출처와 자료 slug (#171). `promoted_to` 와 달리 **파생이 아니라 저장 컬럼 그대로**다.
    # `resource_slug` 에는 ID prefix 를 붙이지 않는다 — FE 가 그대로 URL 에 넣는다.
    source: InboxSource = "user"
    resource_slug: str | None = None


class InboxCreateRequest(CamelModel):
    """POST /inbox — 1줄 캡처."""

    raw_text: str = Field(min_length=1)


class InboxUpdateRequest(CamelModel):
    """PATCH /inbox/{id} — userCategory override 또는 status 변경."""

    user_category: InboxCategory | None = None
    status: InboxStatus | None = None


class InboxClassification(CamelModel):
    """LLM Structured Output — `aiClient.run("inbox/classify")` 응답 schema (내부 사용).

    Tool Executor 가 강제 검증. fallback 룰도 같은 schema 로 반환.

    ## ⚠️ 필드가 하나뿐인 이유 (#428)

    원래 넷이었는데 **셋을 아무도 읽지 않았다.** 최초 구현(#40) 이후 지금까지
    `ai_category_guess` 만 소비된다 — DB 에 컬럼이 없고, API 응답에도 안 나가고,
    FE 도 `aiCategoryGuess` 만 본다.

    | 필드 | 왜 뺐나 |
    |---|---|
    | `needs_user_override` | **`confidence < 0.5` 의 파생값**이다. LLM 이 자기 출력에서 유도되는 불리언을 스스로 계산했고, 어긋나도(예: confidence 0.3 인데 false) 아무도 못 잡았다 |
    | `confidence` | 그 파생의 근거였을 뿐 읽는 곳이 없다 |
    | `suggested_title` | 읽는 곳이 없다. **fallback 이 `raw_text[:10]` 로 사용자 입력을 되돌려주던 유일한 경로**이기도 했다(`test_banned_words` 참고) |

    되살릴 거라면 **쓰는 쪽과 함께** 되살린다 — 안 그러면 LLM 이 매 호출 아무도 안 읽는
    값을 만들고, 그 값이 틀려도 조용하다.
    """

    ai_category_guess: InboxCategory


class InboxResourceDetail(CamelModel):
    """GET /inbox/resources/{slug} — 시스템 항목이 가리키는 정적 자료 본문.

    `markdown` 은 frontmatter 를 걷어낸 본문이다(파일 원문이 아니다). FE 는 첫 H1 이
    `title` 과 같을 때만 본문에서 덜어내므로 **둘 다 가공 없이** 실어야 한다.

    `steps` 는 이 자료가 제안하는 한 걸음들 — 사용자가 골라 오늘 할 일로 채택한다
    (`POST /inbox/{id}/adopt-step`). 자료가 읽고 끝나지 않게 하는 유일한 출구다.
    """

    slug: str
    title: str
    markdown: str
    steps: list[str]


class InboxAdoptStepRequest(CamelModel):
    """POST /inbox/{id}/adopt-step — 자료의 몇 번째 한 걸음을 채택할지."""

    step_index: int = Field(ge=0)


class InboxAdoptedStep(CamelModel):
    """채택 결과 — 오늘 할 일로 만들어진 카드.

    자료 항목 자체는 `promoted` 로 바뀌지 않는다. 한 걸음을 채택한 것이지 자료를
    승격한 게 아니고, 사용자가 나중에 다른 걸음을 또 채택하거나 다시 읽을 수 있다.
    """

    action_id: str
    title: str
    target_date: str  # YYYY-MM-DD
    resource_slug: str
