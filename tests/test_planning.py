"""Planning — `POST /plans/generate` 라우터 ↔ Goal Structuring 오케스트레이터 연동 (#18).

- Happy: 데모 정책+습관 으로 호출 시 비활성 초안 계획을 반환한다.
- Validation: 활성 수면 정책이 없으면 400 `PLANNING_VALIDATION_ERROR`.
- 메서드 다른 placeholder: `GET /plans/{plan_id}` 는 아직 501.
"""

from fastapi.testclient import TestClient

from reaction_backend.api.routes.planning import get_time_policies
from reaction_backend.main import create_app


def test_generate_plan_returns_inactive_draft() -> None:
    """오케스트레이터가 만든 초안 계획이 camelCase 응답으로 직렬화된다."""
    with TestClient(create_app()) as client:
        # 2026-05-25 (월) — 데모 알고리즘 수업(09:00–10:30) 가 잡혀 있는 요일.
        resp = client.post("/plans/generate", json={"targetDate": "2026-05-25"})

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Draft Layer 잠금 — 자동 활성화 금지 (AGENTS §1).
    assert body["isActive"] is False
    assert body["orchestratorState"] == "reviewing"
    assert body["targetDate"] == "2026-05-25"
    assert body["planId"].startswith("draft_")

    # 데모 습관 2종(아침 러닝/저녁 독서)이 각자 선호 윈도우 안에 배치되었는지.
    titles = [b["title"] for b in body["blocks"]]
    assert "아침 러닝" in titles
    assert "저녁 독서" in titles
    for block in body["blocks"]:
        assert block["origin"] == "habit"
        assert block["blockStatus"] == "scheduled"
        assert block["source"] == "ai_plan"
        assert block["durationMinutes"] > 0
        # KST 직렬화 — `+09:00` 오프셋 확인 (ADR-0002 §2.4).
        assert block["startAt"].endswith("+09:00")

    # busy 에는 최소한 수면이 잡혀 있어야 한다 (DevBaseline §1.4).
    busy_sources = {b["source"] for b in body["busyBlocks"]}
    assert "sleep" in busy_sources
    # 월요일 데모 고정 일정("알고리즘 수업") 도 busy 로 노출.
    assert "fixed_schedule" in busy_sources

    # free 블록이 적어도 하나 — 하루가 통째로 busy 일 리는 없다.
    assert len(body["freeBlocks"]) >= 1


def test_generate_plan_without_active_sleep_policy_is_400() -> None:
    """활성 수면 정책이 없으면 오케스트레이터 validate 가 막아 400 을 돌려준다."""
    app = create_app()
    app.dependency_overrides[get_time_policies] = lambda: ()
    try:
        with TestClient(app) as client:
            resp = client.post("/plans/generate", json={"targetDate": "2026-05-25"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "PLANNING_VALIDATION_ERROR"
    assert body["field"] == "time_policies.sleep"


def test_generate_plan_uses_today_when_target_date_omitted() -> None:
    """`targetDate` 누락 시 KST 오늘로 기본값. 응답은 여전히 200 비활성 초안."""
    with TestClient(create_app()) as client:
        resp = client.post("/plans/generate", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["isActive"] is False


def test_get_plan_placeholder_is_501() -> None:
    """`GET /plans/{plan_id}` 미리보기는 아직 placeholder."""
    with TestClient(create_app()) as client:
        resp = client.get("/plans/draft_anything")
    assert resp.status_code == 501
