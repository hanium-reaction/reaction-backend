"""규칙이 채워 둔 '이어가기' 자리표시자에 **지금의 진행 상황으로** 내용을 넣는다 (#454).

## 왜 이 모듈이 있나

첫 계획을 세울 때 `extend_action_plan_to_horizon` 이 마감까지의 빈 자리를 번호만 붙인
자리표시자로 채운다. 제목은 `{목표} 21회차` 로 번호만 다르고 first_step 은 여덟 장이
글자까지 같다. **그때는 그게 맞았다** — 사용자가 어디까지 갈지 모르는 시점에 내용을
지어내면 사용자가 정하지 않은 걸 정해 버린다.

제품은 그 사실을 사용자에게 고지하면서 "매주 재계획에서 채워집니다" 라고 약속했는데,
**재계획은 그 일을 하지 않았다** — 제목을 그대로 옮기는 결정적 스케줄러였다. 이 모듈이
그 약속을 지킨다.

## 언제 도나

**사용자가 재계획을 누를 때만.** 재계획에는 크론이 없다(스케줄러 잡 11종 중 없음,
`scheduler/runtime.py`). 그래서 고지 문구도 "다음 재계획 때" 로 함께 고쳤다 — 없는
주기를 약속하지 않는다.

## 무엇을 자리표시자로 보나

`goal_nodes.source == "rule"`. 초안 시절의 `tmp-continue` 접두사는 승인 때 사라지므로
(초안 node_id 를 보존하는 컬럼이 없다) 이 컬럼이 유일한 단서다. 채우고 나면 `llm` 로
바꾼다 — 컬럼의 뜻이 "**누가 채웠는가**" 이고, 채운 건 LLM 이기 때문이다. 그래서 같은
카드가 두 번 채워지지 않는다.

## 무엇을 안 하나

- **status 를 안 건드린다** (AGENTS §2). 내용만 바꾼다.
- 사용자가 손댄 카드는 애초에 재계획 후보에서 빠진다(`protected_card_ids`).
- LLM 이 실패하면 **자리표시자를 그대로 둔다.** 억지로 채우면 사용자가 안 정한 걸 정하는
  것이라, 원래 상태가 폴백으로서 옳다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from reaction_backend.llm import aiClient
from reaction_backend.schemas.planning import ContinuationFill

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["FilledCard", "PlaceholderCard", "fill_cards", "format_completed", "select_fillable"]

# 자리표시자가 이 개수를 넘으면 앞에서부터만 채운다. 한 계획의 확장 구간은 실측 최대
# 28장인데, 전부 실으면 프롬프트가 길어지고 뒤쪽은 어차피 다음 재계획에서 다시 온다.
MAX_FILL_PER_RUN = 12

# 진행 맥락으로 프롬프트에 싣는 완료 카드 수. 최근 것이 정보가 많다.
MAX_COMPLETED_CONTEXT = 12

# 재계획은 사용자가 기다리는 동기 요청이다. 분해용 45초를 그대로 쓰면 재시도까지 135초가
# 되어 사용자가 계획을 못 받는다 — 이 호출은 카드 몇 장이라 짧게 잡는다.
FILL_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class PlaceholderCard:
    """채울 자리 하나 — 라우터가 DB 행에서 뽑아 넘긴다."""

    action_id: uuid.UUID
    title: str
    node_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class FilledCard:
    """채워진 내용 하나. `action_id` 는 **보낸 것 중 하나임이 검증된** 값이다."""

    action_id: uuid.UUID
    title: str
    first_step: str


def select_fillable(
    placeholders: list[PlaceholderCard], *, limit: int = MAX_FILL_PER_RUN
) -> list[PlaceholderCard]:
    """이번 실행에서 채울 것만 앞에서부터 고른다.

    뒤쪽을 남기는 게 손해가 아닌 이유: 남은 자리표시자는 **다음 재계획에서 다시 후보**가
    되고, 그때는 더 많은 진행 기록을 근거로 채울 수 있다. 먼 미래를 지금 억지로 정하는
    것보다 낫다.
    """
    return placeholders[:limit]


def format_completed(titles: list[str], *, limit: int = MAX_COMPLETED_CONTEXT) -> str:
    """완료 카드 제목을 프롬프트용 목록으로. 없으면 그 사실을 **말한다**.

    빈 문자열을 넣으면 LLM 이 '완료: ' 뒤의 공백을 보고 아무거나 지어낸다 — 아직 시작
    전이라는 것도 정보다.
    """
    if not titles:
        return "  (아직 완료한 카드가 없다 — 이 목표를 이제 시작하는 참이다)"
    return "\n".join(f"  - {t}" for t in titles[:limit])


def _format_placeholders(cards: list[PlaceholderCard]) -> str:
    return "\n".join(f'  - action_id={c.action_id} (지금 제목: "{c.title}")' for c in cards)


def _format_remaining(titles: list[str], *, limit: int = MAX_COMPLETED_CONTEXT) -> str:
    if not titles:
        return "  (없다 — 남은 것이 아래 자리표시자뿐이다)"
    return "\n".join(f"  - {t}" for t in titles[:limit])


async def fill_cards(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    goal_title: str,
    horizon: str,
    placeholders: list[PlaceholderCard],
    completed_titles: list[str],
    remaining_titles: list[str],
    tone_mode: str | None = None,
) -> list[FilledCard]:
    """자리표시자들을 **한 번의 호출로** 채운다. 실패하면 빈 목록(= 그대로 둔다).

    한 번에 묶는 이유는 자리표시자끼리 순서가 있어서다 — 장당 따로 물으면 같은 말이 번호만
    바꿔 나온다. 그게 애초에 고치려던 상태다.

    ⚠️ **돌아온 `action_id` 를 반드시 대조한다.** 스키마는 그 id 가 실제로 보낸 것인지
    못 본다. 지어낸 id 를 그대로 쓰면 **엉뚱한 카드를 덮어쓴다.**
    """
    if not placeholders:
        return []

    result = await aiClient.run(
        module="planning",
        schema=ContinuationFill,
        prompt_id="planning/continuation_fill",
        # 폴백은 "아무것도 안 채운다" 다 — 자리표시자를 그대로 두는 쪽이 지어내는 것보다 낫다.
        fallback=lambda: ContinuationFill(cards=[]),
        timeout=FILL_TIMEOUT_SECONDS,
        thinking_budget=0,
        variables={
            "goal_title": goal_title,
            "horizon": horizon or "(마감 없음 — 이번 계획 구간까지)",
            "completed": format_completed(completed_titles),
            "remaining": _format_remaining(remaining_titles),
            "placeholders": _format_placeholders(placeholders),
        },
        user_id=user_id,
        session=session,
        tone_mode=tone_mode,
    )
    # ⚠️ `fell_back` 을 따로 안 본다 — `_fallback` 이 **항상** 폴백 값으로 갈아끼우고
    # 우리 폴백은 빈 목록이라, 폴백이면 아래가 자동으로 빈 목록이 된다. 조건을 하나 더 두면
    # 테스트가 못 건드리는 죽은 가지가 생기고(변이해도 안 빨개진다), 나중에 폴백이 내용을
    # 갖게 되면 그 값을 **조용히 버리는** 쪽으로 잘못 작동한다.
    return match_to_placeholders(result.value, placeholders)


def match_to_placeholders(
    filled: ContinuationFill, placeholders: list[PlaceholderCard]
) -> list[FilledCard]:
    """LLM 출력을 **보낸 자리표시자에만** 짝지어 준다. 나머지는 버린다.

    버리는 경우 셋:
    - 안 보낸 `action_id` (지어냈거나 잘못 옮겨 적었다) → 엉뚱한 카드 덮어쓰기 방지
    - 같은 id 를 두 번 → 처음 것만
    - 지금 제목·첫걸음과 **똑같이** 돌려준 것 → 채운 게 아니라 되돌려준 것이다
    """
    by_id = {str(p.action_id): p for p in placeholders}
    seen: set[str] = set()
    out: list[FilledCard] = []
    for card in filled.cards:
        key = card.action_id.strip()
        target = by_id.get(key)
        if target is None or key in seen:
            continue
        title = card.title.strip()
        first_step = card.first_step.strip()
        if not title or not first_step or title == target.title:
            continue
        seen.add(key)
        out.append(FilledCard(action_id=target.action_id, title=title, first_step=first_step))
    return out
