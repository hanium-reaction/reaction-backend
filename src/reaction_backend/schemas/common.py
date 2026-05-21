"""공통 스키마.

re:action 응답 규약 (api-contract.md 진실 소스):
- 성공 응답: 도메인 객체를 **직접** 반환 (envelope 없음). OpenAPI 친화적.
- 에러 응답: 항상 `ErrorResponse` (HTTP 4xx/5xx).
- 시간 필드: KST 표시(+09:00), ISO 8601. 서버 내부는 UTC 저장.
- 에러 코드: 도메인 prefix UPPER_SNAKE (AUTH_*, GOAL_*, INTERVIEW_*, LLM_*, RECOVERY_*, ...).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    """현재 시각을 KST(+09:00) 기준 aware datetime으로 반환."""
    return datetime.now(KST)


class ErrorResponse(BaseModel):
    """공통 에러 envelope. HTTP 4xx/5xx 응답에서 사용.

    Example:
        {
          "code": "AUTH_INVALID_TOKEN",
          "message": "Access token is invalid or expired.",
          "field": null,
          "server_time": "2026-05-21T01:23:45.678+09:00"
        }
    """

    code: str = Field(
        ...,
        description="도메인 prefix UPPER_SNAKE_CASE 에러 코드",
        examples=["AUTH_INVALID_TOKEN", "INTERVIEW_SLOT_LOCKED", "RECOVERY_NO_PROPOSAL"],
    )
    message: str = Field(
        ...,
        description="사람이 읽는 메시지 (한국어 가능, 사용자 노출 가능)",
    )
    field: str | None = Field(
        default=None,
        description="입력 검증 에러일 때 해당 필드명, 그 외 null",
    )
    server_time: datetime = Field(default_factory=now_kst)


class HealthResponse(BaseModel):
    """`GET /health` 응답."""

    status: str = Field(default="ok", description="고정값")
    app: str
    version: str
    env: str
    server_time: datetime = Field(default_factory=now_kst)
