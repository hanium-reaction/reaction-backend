"""Calendar 라우터 — 스위치가 꺼진 기본 상태의 동작.

연결·freebusy 는 `GOOGLE_CALENDAR_ENABLED` 가 꺼져 있으면 501 이다(배포 기본값).
켜진 상태의 동작은 `test_calendar_connect.py` · `test_calendar_freebusy.py` 가 다룬다.
sync-preview / approve-insert 는 write-back(P1)이라 아직 mock 이다.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_connect_returns_501_while_disabled(client: TestClient) -> None:
    """스위치 OFF(기본) — Cloud 콘솔 셋업 전에는 예전 동작 그대로."""
    resp = client.post("/calendar/connect", json={"code": "oauth-code"})
    assert resp.status_code == 501
    assert resp.json()["code"] == "COMMON_NOT_IMPLEMENTED"


def test_connect_rejects_empty_code(client: TestClient) -> None:
    """Pydantic Field min_length=1 — 본문 검증이 라우터 진입 전에 422."""
    resp = client.post("/calendar/connect", json={"code": ""})
    assert resp.status_code == 422


def test_disconnect_returns_501_while_disabled(client: TestClient) -> None:
    resp = client.delete("/calendar/connect")
    assert resp.status_code == 501
    assert resp.json()["code"] == "COMMON_NOT_IMPLEMENTED"


def test_freebusy_returns_501_while_disabled(client: TestClient) -> None:
    """예전엔 데모 구간을 돌려주는 mock 이었다.

    실구현으로 바뀌면서 **가짜 일정을 돌려주지 않는다** — 연결도 안 된 상태에서 그럴듯한
    busy 구간을 주면 FE 가 "캘린더가 되고 있다" 고 믿고, 그 위에 계획이 잡힌다.
    """
    resp = client.get("/calendar/freebusy", params={"from": "2026-05-25", "to": "2026-05-26"})
    assert resp.status_code == 501


def test_freebusy_requires_range(client: TestClient) -> None:
    resp = client.get("/calendar/freebusy")
    assert resp.status_code == 422


def test_sync_preview_mock(client: TestClient) -> None:
    resp = client.post("/calendar/sync-preview")
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "conflictCount" in body


def test_approve_insert_requires_idempotency_key(client: TestClient) -> None:
    resp = client.post("/calendar/events/approve-insert")
    assert resp.status_code == 400
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_approve_insert_with_key_mock(client: TestClient) -> None:
    resp = client.post(
        "/calendar/events/approve-insert", headers={"Idempotency-Key": "calendar-demo-1"}
    )
    assert resp.status_code == 200
    assert resp.json()["insertedCount"] == 2
