"""데모 사용자 fixture — Issue #3 mock/stub 응답의 공통 기준.

도메인 스텁(#3-B~#3-H)은 이 모듈의 `DEMO_USER` 를 참조해 일관된 데모 데이터를 반환한다.
DB 의 demo seed(`scripts/db_seed_demo.py`)와 속성(email·tone·timezone·ACTIVE)을 맞춘다.
단, mock 응답은 DB 를 거치지 않으므로 식별자는 **고정값**을 쓴다 (응답 결정성 보장).

식별자 표기: DB 는 UUID v4, API 응답은 도메인 prefix 를 붙인다 (ADR-0001 §3.1, api-contract §1.8).
페르소나: 김민수 (22, 컴공 4학년) — DevBaseline §3.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from reaction_backend.schemas.common import KST


@dataclass(frozen=True, slots=True)
class DemoUser:
    """데모 사용자 식별·프로필 (mock 응답용 고정값)."""

    id: str
    email: str
    name: str
    timezone: str
    onboarding_state: str
    tone_mode: str
    created_at: datetime


# 고정 demo user — Issue #3 스텁 전반에서 동일 사용자를 가리킨다.
DEMO_USER = DemoUser(
    id="user_11111111-1111-4111-8111-111111111111",
    email="demo@reaction.local",
    name="김민수",
    timezone="Asia/Seoul",
    onboarding_state="ACTIVE",
    tone_mode="gentle",
    created_at=datetime(2026, 5, 1, 9, 0, tzinfo=KST),
)

# 데모 인증 토큰 — 가짜 불투명 문자열. 실제 JWT 발급·검증은 #16 (Auth).
DEMO_ACCESS_TOKEN = "demo-access-token-stub"
DEMO_REFRESH_TOKEN = "demo-refresh-token-stub"
