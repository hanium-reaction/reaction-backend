"""KST 시간 직렬화 — 응답 datetime 이 KST(+09:00)로 나가는지 (ADR-0002 §2.4)."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from pydantic import BaseModel

from reaction_backend.schemas.common import KstDatetime, to_kst


def test_to_kst_treats_naive_as_utc() -> None:
    converted = to_kst(datetime(2026, 5, 22, 0, 0, 0))
    offset = converted.utcoffset()
    assert offset is not None and offset.total_seconds() == 9 * 3600
    assert converted.hour == 9  # 00:00 UTC → 09:00 KST


def test_to_kst_converts_aware_utc() -> None:
    converted = to_kst(datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    assert converted.hour == 21  # 12:00 UTC → 21:00 KST


class _Model(BaseModel):
    at: KstDatetime


def test_kst_datetime_serializes_with_offset() -> None:
    model = _Model(at=datetime(2026, 5, 22, 0, 0, 0, tzinfo=UTC))
    dumped = model.model_dump(mode="json")
    assert dumped["at"].endswith("+09:00")
    assert "T09:00" in dumped["at"]


def test_health_server_time_is_kst(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["server_time"].endswith("+09:00")
