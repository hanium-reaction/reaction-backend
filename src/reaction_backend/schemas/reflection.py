"""Reflection 도메인 스키마 (api-contract §11) — S17 저녁 회고 / S18 실패 사유.

#19-B 범위: 13종 마스터 조회 + 실행 1건 태깅 (0~2개, memo at-rest 암호화).
batch(S17 일괄 처리)는 후속.
"""

from __future__ import annotations

from pydantic import Field

from reaction_backend.schemas.common import CamelModel


class FailureTagMaster(CamelModel):
    """GET /reflection/failure-tags 응답 row — S18 칩의 원본 (13종, is_active=true)."""

    tag_code: str
    label_ko: str
    description: str | None
    sort_order: int


class FailureTagRequest(CamelModel):
    """POST /reflection/failure-tags/{executionId} — 실패 사유 0~2개 + 메모."""

    tag_codes: list[str] = Field(max_length=2)
    memo: str | None = Field(default=None, max_length=300)


class FailureTagResponse(CamelModel):
    """태깅 결과."""

    execution_id: str
    tag_codes: list[str]
    has_memo: bool
