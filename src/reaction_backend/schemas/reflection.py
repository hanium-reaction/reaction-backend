"""Reflection 도메인 스키마 (api-contract §11) — S17 저녁 회고 / S18 실패 사유.

#19-B 범위: 13종 마스터 조회 + 실행 1건 태깅 (0~2개, memo at-rest 암호화).
batch(S17 일괄 처리)는 이 파일의 `ReflectionBatch*`.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from reaction_backend.schemas.common import CamelModel
from reaction_backend.schemas.today import ExecutionCompletion


class ReflectionPendingItem(CamelModel):
    """GET /reflection/pending 응답 row — S17 저녁 회고에서 처리할 미체크 실행 (#83).

    최근 3일(오늘+어제+그제) 중 아직 체크인되지 않은(in_progress) 실행. 사용자가
    저녁에 소급 체크인/일괄 회고(POST /reflection/batch)할 대상이다. 아직 결과가
    정해지지 않았으므로 completion_status 는 null.
    """

    execution_id: str
    action_item_id: str
    title: str
    scheduled_date: date  # 계획 시작일 (YYYY-MM-DD)
    scheduled_time: str | None  # "HH:MM" (KST) — 계획 시작 시각
    completion_status: str | None  # 미체크 → null


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


class ReflectionBatchItem(CamelModel):
    """POST /reflection/batch 항목 — 미체크 실행 1건의 최종 결과 + 선택적 실패 사유.

    `failure_tags`/`memo` 는 `completion_status` 가 failed/partial_done 일 때만 유효
    (그 외 값과 함께 오면 422). `memo` 는 서버가 at-rest 암호화한다.
    """

    execution_id: str
    completion_status: ExecutionCompletion  # done / partial_done / failed / over_done
    failure_tags: list[str] = Field(default_factory=list, max_length=2)
    memo: str | None = Field(default=None, max_length=300)


class ReflectionBatchRequest(CamelModel):
    """POST /reflection/batch — [모두 완료] 일괄 처리. Idempotency-Key 필수(미들웨어).

    빈 배열은 no-op(200, processedCount=0). 상한 50건.
    """

    items: list[ReflectionBatchItem] = Field(default_factory=list, max_length=50)


class ReflectionBatchResponse(CamelModel):
    """일괄 처리 결과 요약."""

    processed_count: int  # 종결(체크인) 처리된 실행 수
    tagged_count: int  # 실패 사유가 함께 기록된 실행 수
    needs_failure_tags: list[
        str
    ]  # failed/partial_done 인데 사유 미기록 → S18 유도 대상 executionId
