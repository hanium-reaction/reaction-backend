"""공통 스키마.

re:action 응답 규약 (api-contract.md 진실 소스):
- 성공 응답: 도메인 객체를 **직접** 반환 (envelope 없음). OpenAPI 친화적.
- 에러 응답: 항상 `ErrorResponse` (HTTP 4xx/5xx).
- 시간 필드: KST 표시(+09:00), ISO 8601. 서버 내부는 UTC 저장.
- 에러 코드: 도메인 prefix UPPER_SNAKE (AUTH_*, GOAL_*, INTERVIEW_*, LLM_*, RECOVERY_*, ...).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, WithJsonSchema
from pydantic.alias_generators import to_camel

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    """현재 시각을 KST(+09:00) 기준 aware datetime으로 반환."""
    return datetime.now(KST)


def to_kst(dt: datetime) -> datetime:
    """datetime 을 KST(+09:00) aware 로 변환. naive 는 UTC 로 간주 (ADR-0002 §2.4)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(KST)


def _serialize_kst(dt: datetime) -> str:
    return to_kst(dt).isoformat()


# 응답 datetime 필드용 타입. JSON 직렬화 시 KST(+09:00) ISO 8601 로 자동 변환된다.
# 서버 내부 저장은 UTC — 응답 스키마의 datetime 필드는 모두 이 타입을 쓴다 (ADR-0002 §2.4).
KstDatetime = Annotated[
    datetime,
    PlainSerializer(_serialize_kst, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]


class CamelModel(BaseModel):
    """도메인 요청/응답 스키마 베이스 — 필드를 camelCase 로 입출력 (api-contract §1.9).

    Python 코드는 snake_case 로 쓰고, JSON 직렬화·역직렬화는 camelCase 로 한다.
    `ErrorResponse` 등 공통 메타 응답은 본 베이스를 쓰지 않고 기존 필드명을 유지한다.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DraftMixin(CamelModel):
    """AI 산출물 응답 mixin — HITL Draft Layer 표기 (ADR-0005 §7.2, DevBaseline §1.4 잠금).

    AGENTS.md §1.4: "AI 출력 = Draft Layer + [수락/수정/거절] 3버튼. 자동 적용 금지."
    AI 가 생성한 모든 응답(`generate`/`proposals`/`preview`)은 사용자 명시 승인 전까지
    `is_draft=True` 로 반환된다. `is_draft=False` 는 명시 승인 endpoint
    (`/plans/{id}/approve`, `/recovery/decisions` 등)에서만 set 한다 (ADR-0005 §7.2).

    책임 분리:
    - Orchestrator Node 는 `used_fallback` 만 정확히 유지 (is_draft 는 신경 X).
    - 라우터 함수가 응답 빌드 시 `is_draft=True` + `ai_source` 강제.
    """

    is_draft: bool = True
    ai_source: Literal["llm", "rule"] = "llm"


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
    server_time: KstDatetime = Field(default_factory=now_kst)


class DbStatus(BaseModel):
    """DB 헬스 정보 (health 응답에 포함)."""

    ok: bool
    latency_ms: int | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    """`GET /health` 응답.

    `status`:
      - `"ok"`     — 앱 + DB 모두 정상
      - `"degraded"` — 앱은 살아있으나 의존성(DB) 비정상
    HTTP status는 항상 200 (앱 자체는 응답 가능). 503 분기는 readiness 엔드포인트로
    분리할 때 도입.
    """

    status: str = Field(description="ok or degraded")
    app: str
    version: str
    env: str
    server_time: KstDatetime = Field(default_factory=now_kst)
    db: DbStatus
