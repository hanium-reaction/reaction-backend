"""Inbox — 실 구현 (Issue #22-B, api-contract §18).

`GEMINI_API_KEY` 가 빈 상태이므로 `aiClient.run` 은 자동으로 룰 fallback 분기.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def _capture(client: TestClient, text: str = "캡스톤 설계 단계 정리") -> dict[str, Any]:
    resp = client.post("/inbox", json={"rawText": text})
    assert resp.status_code == 201, resp.json()
    return resp.json()


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/inbox")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_captures_and_classifies(client: TestClient) -> None:
    body = _capture(client, "캡스톤 설계 단계 정리")
    assert body["inboxId"].startswith("inbox_")
    assert body["rawText"] == "캡스톤 설계 단계 정리"
    # 룰 fallback 키워드 "캡스톤" → project
    assert body["aiCategoryGuess"] == "project"
    assert body["status"] == "classified"
    assert body["userCategory"] is None
    assert body["promotedGoalId"] is None


def test_create_rule_fallback_other(client: TestClient) -> None:
    """매칭되는 키워드 없으면 ai_category_guess=other."""
    body = _capture(client, "흠 뭔가 생각났는데")
    assert body["aiCategoryGuess"] == "other"


def test_create_rejects_empty_text(client: TestClient) -> None:
    resp = client.post("/inbox", json={"rawText": ""})
    assert resp.status_code == 422


def test_create_encrypts_raw_text(client: TestClient, fake_inbox_repo: Any) -> None:
    """DB에 저장되는 raw_text 는 평문 X — 응답만 복호화."""
    _capture(client, "운동 매일 30분")
    # fake repo 내부의 raw_text_encrypted 가 원문과 다름 (암호화)
    stored = next(iter(fake_inbox_repo._items.values()))
    assert stored.raw_text_encrypted != "운동 매일 30분"


def test_list_after_create(client: TestClient) -> None:
    _capture(client, "토익 단어 외우기")
    items = client.get("/inbox").json()
    assert len(items) == 1
    assert items[0]["aiCategoryGuess"] == "study"


def test_list_filter_by_status(client: TestClient) -> None:
    _capture(client, "운동")
    _capture(client, "프로젝트")
    classified = client.get("/inbox", params={"status": "classified"}).json()
    archived = client.get("/inbox", params={"status": "archived"}).json()
    assert len(classified) == 2
    assert archived == []  # 아직 보관한 항목 없음


def test_patch_user_category(client: TestClient) -> None:
    created = _capture(client)
    resp = client.patch(f"/inbox/{created['inboxId']}", json={"userCategory": "study"})
    assert resp.status_code == 200
    assert resp.json()["userCategory"] == "study"


def test_patch_rejects_bad_category(client: TestClient) -> None:
    created = _capture(client)
    resp = client.patch(f"/inbox/{created['inboxId']}", json={"userCategory": "bogus"})
    assert resp.status_code == 422


def test_patch_not_found(client: TestClient) -> None:
    resp = client.patch(
        "/inbox/inbox_99999999-9999-4999-8999-999999999999",
        json={"userCategory": "study"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "INBOX_NOT_FOUND"


def test_patch_bad_id_format(client: TestClient) -> None:
    resp = client.patch("/inbox/nonexistent", json={"userCategory": "study"})
    assert resp.status_code == 404


def test_convert_to_goal(client: TestClient, fake_goal_repo: Any) -> None:
    created = _capture(client, "캡스톤 마무리")
    resp = client.post(f"/inbox/{created['inboxId']}/convert-to-goal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "promoted"
    assert body["promotedGoalId"] is not None
    assert body["promotedGoalId"].startswith("goal_")
    assert body["promotedTo"] == "goal"  # FE 배지 구분용
    # Goal 실제 생성 확인
    goals = client.get("/goals").json()
    assert len(goals["maintain"]) == 1
    assert goals["maintain"][0]["title"] == "캡스톤 마무리"


def test_convert_to_goal_uses_user_category_override(client: TestClient) -> None:
    """user_category 가 있으면 우선 사용."""
    created = _capture(client, "어떤 텍스트")  # ai_category_guess=other
    client.patch(f"/inbox/{created['inboxId']}", json={"userCategory": "study"})
    client.post(f"/inbox/{created['inboxId']}/convert-to-goal")
    goal = client.get("/goals").json()["maintain"][0]
    assert goal["category"] == "study"


def test_convert_to_goal_rejects_over_maintain_limit(client: TestClient) -> None:
    """Maintain 한도 5 — 6번째 convert 시 422."""
    for i in range(5):
        client.post(
            "/goals",
            json={
                "title": f"m{i}",
                "category": "study",
                "goalTier": "maintain",
                "priorityLevel": 3,
            },
        )
    created = _capture(client, "over")
    resp = client.post(f"/inbox/{created['inboxId']}/convert-to-goal")
    assert resp.status_code == 422
    assert resp.json()["code"] == "GOAL_TIER_LIMIT_EXCEEDED"


def test_convert_to_goal_not_found(client: TestClient) -> None:
    resp = client.post("/inbox/inbox_99999999-9999-4999-8999-999999999999/convert-to-goal")
    assert resp.status_code == 404


def test_convert_to_action(client: TestClient, fake_action_item_repo: Any) -> None:
    created = _capture(client, "오늘 산책")
    resp = client.post(f"/inbox/{created['inboxId']}/convert-to-action")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "promoted"
    assert body["promotedGoalId"] is None  # action 변환은 goal 미연결
    assert body["promotedTo"] == "action"  # FE 배지 구분용
    # ActionItem 실제 생성 확인
    actions = list(fake_action_item_repo._items.values())
    assert len(actions) == 1
    assert actions[0].title == "오늘 산책"
    assert actions[0].source == "inbox"
    assert actions[0].inbox_item_id is not None


def test_convert_to_action_not_found(client: TestClient) -> None:
    resp = client.post("/inbox/inbox_99999999-9999-4999-8999-999999999999/convert-to-action")
    assert resp.status_code == 404


def test_archive(client: TestClient) -> None:
    created = _capture(client)
    resp = client.post(f"/inbox/{created['inboxId']}/archive")
    assert resp.status_code == 204
    assert client.get("/inbox").json() == []


def test_archive_not_found(client: TestClient) -> None:
    resp = client.post("/inbox/inbox_99999999-9999-4999-8999-999999999999/archive")
    assert resp.status_code == 404


def test_archive_bad_id_format(client: TestClient) -> None:
    resp = client.post("/inbox/nonexistent/archive")
    assert resp.status_code == 404


def test_archived_item_visible_in_archive_filter(client: TestClient) -> None:
    """보관한 항목은 기본 목록에서 빠지되 `?status=archived` 로는 조회된다."""
    created = _capture(client, "나중에 볼 것")
    client.post(f"/inbox/{created['inboxId']}/archive")

    assert client.get("/inbox").json() == []  # 활성 목록엔 없음
    archived = client.get("/inbox", params={"status": "archived"}).json()
    assert len(archived) == 1
    assert archived[0]["inboxId"] == created["inboxId"]
    assert archived[0]["status"] == "archived"


def test_restore_unarchives_item(client: TestClient) -> None:
    """복원하면 활성 목록으로 돌아오고 보관함에서 빠진다 (status=classified 복원)."""
    created = _capture(client, "다시 살릴 것")
    client.post(f"/inbox/{created['inboxId']}/archive")

    resp = client.post(f"/inbox/{created['inboxId']}/restore")
    assert resp.status_code == 200
    assert resp.json()["status"] == "classified"

    assert len(client.get("/inbox").json()) == 1  # 활성 목록 복귀
    assert client.get("/inbox", params={"status": "archived"}).json() == []  # 보관함 비었음


def test_restore_is_idempotent_on_active_item(client: TestClient) -> None:
    """활성 항목에 restore 해도 에러 없이 현재 상태 그대로 반환 (멱등)."""
    created = _capture(client, "이미 활성")
    resp = client.post(f"/inbox/{created['inboxId']}/restore")
    assert resp.status_code == 200
    assert resp.json()["status"] == "classified"


def test_restore_not_found(client: TestClient) -> None:
    resp = client.post("/inbox/inbox_99999999-9999-4999-8999-999999999999/restore")
    assert resp.status_code == 404
    assert resp.json()["code"] == "INBOX_NOT_FOUND"


# ─────────────────────────────────────────────────────────────────────────────
# 분류 스키마는 **쓰는 것만** 요구한다 (#428)
# ─────────────────────────────────────────────────────────────────────────────


def test_classification_schema_only_asks_for_what_is_used() -> None:
    """⚠️ LLM 이 **아무도 안 읽는 값**을 만들면, 틀려도 조용하다.

    원래 넷이었는데 셋을 아무도 읽지 않았다(최초 구현 #40 이후 지금까지):

    - `needs_user_override` — `confidence < 0.5` 의 **파생값**. LLM 이 자기 출력에서
      유도되는 불리언을 스스로 계산했고, 어긋나도(confidence 0.3 인데 false) 못 잡았다.
    - `confidence` — 그 파생의 근거였을 뿐 읽는 곳이 없다.
    - `suggested_title` — 읽는 곳이 없다. fallback 이 사용자 입력을 되돌려주던 경로이기도 했다.

    되살릴 거라면 **쓰는 쪽과 함께** 되살린다 — 이 테스트가 그 약속이다.
    """
    from reaction_backend.schemas.inbox import InboxClassification

    assert set(InboxClassification.model_fields) == {"ai_category_guess"}


def test_classification_prompt_matches_the_schema() -> None:
    """프롬프트가 스키마에 없는 필드를 요구하면 LLM 이 버려질 값을 만든다."""
    from reaction_backend.prompts import registry

    body = registry.get("inbox/classify").body
    for gone in ("confidence", "suggested_title", "needs_user_override"):
        assert gone not in body, f"프롬프트가 아직 `{gone}` 을 요구한다"
    assert "ai_category_guess" in body


def test_rule_fallback_does_not_echo_user_input() -> None:
    """룰 폴백이 사용자 입력을 되돌려주지 않는다.

    예전엔 `suggested_title=raw_text[:10]` 으로 캡처 원문을 그대로 실었다 —
    그게 fallback 이 금지어 필터를 우회하던 유일한 실 경로였다(#20 DoD 8).
    """
    from reaction_backend.api.routes.inbox import _rule_fallback_classify

    raw = "실패한 프로젝트를 다시 살려보자"
    got = _rule_fallback_classify(raw)

    # 카테고리 하나뿐이고, 원문 조각이 어디에도 실리지 않는다.
    assert set(got.model_dump()) == {"ai_category_guess"}
    assert raw[:10] not in str(got.model_dump())
