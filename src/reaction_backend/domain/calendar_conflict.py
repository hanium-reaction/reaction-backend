"""예정 블록 × Google 캘린더 일정 겹침 판정 — 단일 진실 소스.

계획은 **만들 때** 캘린더를 피한다(ADR-0009 D4). 그 뒤에 캘린더에 약속이 생기면 이미
승인한 블록은 그 사실을 모른다. 화면(오늘·주간)과 아침 브리프가 이 판정으로 "지금 겹친다"
를 알려준다 — **옮기지는 않는다**(AI 결과 자동 적용 금지, AGENTS §1). 옮기는 건 사용자가
블록 편집·재계획으로 한다.

`missed_check_in.py` 와 같은 원칙이다 — 판정은 서버 하나, 표현(배지·문구)은 FE·브리프.

프레임워크·ORM 의존성 없음(AGENTS §4) — 원시값만 받는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

# 아직 시작 안 한 블록만 — 시작·완료한 블록은 이미 벌어진 일이고, 취소된 블록은 없는 일이다.
_MOVABLE_STATUS: Final = "scheduled"


def overlaps(start: datetime, end: datetime, busy_start: datetime, busy_end: datetime) -> bool:
    """반열린 구간 [start, end) 끼리 겹치는가. **맞닿기만 한 건 겹침이 아니다**.

    10:00 에 끝나는 수업 바로 뒤 10:00 블록은 스케줄러가 일부러 붙여 둔 자리다 —
    그걸 겹침으로 알리면 첫 계획부터 배지가 뜬다.
    """
    return start < busy_end and busy_start < end


def conflicting_keys[K](
    blocks: Iterable[tuple[K, str, datetime, datetime]],
    busy: Sequence[tuple[datetime, datetime]],
    *,
    now: datetime,
) -> set[K]:
    """캘린더 일정과 겹치는 블록의 key 집합. 블록은 `(key, block_status, start, end)`.

    - `scheduled` 만 본다 — 옮길 수 있는 블록만 알릴 가치가 있다.
    - **이미 끝난 블록(end <= now)은 뺀다** — 지나간 시간은 옮길 수 없고, 지난주 그리드에
      배지가 가득 차면 지금 봐야 할 겹침이 묻힌다.
    """
    if not busy:
        return set()
    return {
        key
        for key, status, start, end in blocks
        if status == _MOVABLE_STATUS
        and end > now
        and any(overlaps(start, end, b_start, b_end) for b_start, b_end in busy)
    }
