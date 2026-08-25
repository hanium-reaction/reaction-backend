"""블록 후 미체크 판정 — 단일 진실 소스 (근거 대장 §6.2 T1).

**T1(블록 후 미체크)**: 블록 시작 +20분이 지났는데 아직 체크인(=[▶ 시작]조차) 안 한 카드
— **push 가 아니라 인앱 배지/인박스**로 알린다(잠금 3규칙이 push 클래스를 3종으로
고정해 새 push 클래스를 못 만든다 — `NOTIFICATION_CLASSES`, `safety/push_gate.py`).

이 모듈은 "배지를 몇 번 보여줄지", "언제 사라지는지" 는 모른다 — 그건 FE 의
몫이다(reaction-frontend#224). 여기는 **판정 하나만** 한다: 지금 이 블록이 미체크
상태인가. `GET /today/agenda` 가 카드마다 이 판정을 `missedCheckIn` 필드로 실어
보내면, FE 가 배지를 그릴지 말지 스스로 정한다 — `action_cancel.py`(#214)가 이미
쓰고 있는 "판정은 서버 하나, 표현은 FE" 원칙과 같다.

⚠️ **스코프 경계**: 근거 대장 §6.2 의 중단 조건("최근 앱 세션 있음 → skip", "무응답
누적 → pause") 은 여기 없다. 그 조건들은 계산 불가능하다고 문서가 명시했다
(`app_sessions` 테이블 부재 — §6.2 "선행 조건" 각주). 이 판정은 그 억제 없이 "블록이
지났고 시작 안 함"만 본다 — 과다 발송(같은 카드를 계속 미체크로 보여줌) 여부는 FE 가
배지 노출 빈도로 조절해야 한다.

프레임워크·ORM 의존성 없음(AGENTS §4) — 원시값만 받는다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from datetime import datetime

# 근거 대장 §6.2 T1 — "시작 +20분". D3(근접창 60분) 안에서 설계자가 고른 값(정직 표기).
MISSED_CHECK_IN_DELAY: Final[timedelta] = timedelta(minutes=20)

# [▶ 시작] 이전에만 이 상태다 — `today.py::start_action` 이 누르는 순간 'started' 로
# 바뀐다. 이 판정은 그 전이가 **아직 안 일어난** 블록만 본다.
_UNSTARTED_BLOCK_STATUS: Final[str] = "scheduled"


def is_missed_check_in(*, block_status: str, start_at: datetime, now: datetime) -> bool:
    """이 블록이 지금 "미체크" 상태인가 — 시작 안 했고, 시작 예정 +20분이 지났는가.

    `block_status != 'scheduled'` 면 이미 시작했거나(started) 끝났거나(finished)
    취소됐다(cancelled) — 어느 쪽이든 "아직 안 함" 신호가 아니므로 False.
    """
    return block_status == _UNSTARTED_BLOCK_STATUS and now >= start_at + MISSED_CHECK_IN_DELAY
