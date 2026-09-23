"""제목·숫자 입력 상한 — 긴 제목이 500 대신 한국어 422 로 막히는지 (goals-5·contract-4·abuse-5·goals-12).

예전엔 요청 스키마에 길이 상한이 없어 200자를 넘는 제목이 DB(`String(200)`)에서
`StringDataRightTruncation` → 500 이 났다. FE 는 "잠시 후 다시 시도해 주세요" 를 띄웠지만
다시 눌러도 영영 성공할 수 없었다. 만다라 축·칸은 상한이 있었지만 pydantic 영어 문구가
나가 FE 가 "입력값을 확인해 주세요." 로만 보여 줬다.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.orchestrator import ultimate_adapter
from tests.conftest import DEMO_USER_UUID
from tests.test_ultimate_adapter import _GoalSession, _outcome_with_statement


def _goal_body(**over: Any) -> dict[str, Any]:
    return {
        "title": "캡스톤",
        "category": "project",
        "goalTier": "parked",
        "priorityLevel": 1,
        **over,
    }


def _habit_body(**over: Any) -> dict[str, Any]:
    return {
        "title": "운동",
        "category": "health",
        "frequencyPerWeek": 3,
        "minutesPerSession": 30,
        "timePreference": "morning",
        "priorityLevel": 2,
        **over,
    }


def _assert_korean_422(resp: Any, field: str) -> str:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "COMMON_VALIDATION_ERROR"
    assert body["field"] == field
    message: str = body["message"]
    # 영어 pydantic 문구("String should have …")가 새지 않는다.
    assert "String" not in message and "should" not in message, message
    return message


# ───────────────────────────── 목표 ─────────────────────────────


def test_goal_title_over_200_is_korean_422(client: TestClient) -> None:
    resp = client.post("/goals", json=_goal_body(title="가" * 201))
    message = _assert_korean_422(resp, "title")
    assert "200자" in message


def test_goal_title_of_exactly_200_is_created(client: TestClient) -> None:
    resp = client.post("/goals", json=_goal_body(title="가" * 200))
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["title"]) == 200


def test_goal_title_blank_is_rejected_and_edges_are_trimmed(client: TestClient) -> None:
    _assert_korean_422(client.post("/goals", json=_goal_body(title="   ")), "title")

    resp = client.post("/goals", json=_goal_body(title="  캡스톤  "))
    assert resp.status_code == 201, resp.text
    assert resp.json()["title"] == "캡스톤"


def test_goal_patch_title_empty_or_long_is_422(client: TestClient) -> None:
    created = client.post("/goals", json=_goal_body()).json()
    gid = created["goalId"]

    _assert_korean_422(client.patch(f"/goals/{gid}", json={"title": ""}), "title")
    _assert_korean_422(client.patch(f"/goals/{gid}", json={"title": "가" * 201}), "title")
    # title 을 빼면 그대로(부분 수정) — 새 검사가 생략을 막지 않는다.
    resp = client.patch(f"/goals/{gid}", json={"priorityLevel": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "캡스톤"


def test_goal_estimated_minutes_out_of_range_is_422(client: TestClient) -> None:
    for bad in (2**31, -100):
        resp = client.post("/goals", json=_goal_body(estimatedMinutes=bad))
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["field"] == "estimatedMinutes"


# ─────────────────────── 궁극목표(인터뷰 문장) ───────────────────────


async def test_long_ultimate_statement_is_clipped_to_title_length() -> None:
    """인터뷰 문장은 길이 제한이 없다 — 제목만 200자에 맞춰 저장이 영영 실패하지 않게."""
    statement = "매일 조금씩 " * 50  # 350자
    session = _GoalSession()
    goal = await ultimate_adapter.materialize_ultimate_goal(
        session,  # type: ignore[arg-type]
        user_id=DEMO_USER_UUID,
        outcome=_outcome_with_statement(statement),
    )
    assert len(goal.title) <= 200
    assert goal.title.endswith("…")
    assert goal.title.startswith("매일 조금씩")


def test_ultimate_route_with_long_inline_statement_is_201(client: TestClient) -> None:
    resp = client.post(
        "/goals/ultimate",
        json={
            "outcome": {
                "sessionId": "iv_inline",
                "generatedAt": "2026-08-20T10:00:00+09:00",
                "endReason": "completed",
                "ambiguityFinal": 0.1,
                "analysisSource": "llm",
                "statement": "나" * 250,
                "domain": "역량",
                "horizonYears": 3,
                "measure": "판정 기준",
                "successImage": "성공 이미지",
                "identityNote": "정체성",
                "currentPosition": "현재 위치",
                "constraints": [],
                "values": [],
                "assets": None,
                "pillarsHint": [],
                "unresolvedSlots": [],
            }
        },
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["title"]) == 200


# ───────────────────────────── 습관 ─────────────────────────────


def test_habit_title_over_200_is_korean_422(client: TestClient) -> None:
    message = _assert_korean_422(
        client.post("/habits", json=_habit_body(title="가" * 201)), "title"
    )
    assert "습관 이름" in message
    _assert_korean_422(client.post("/habits", json=_habit_body(title="  ")), "title")


def test_habit_minutes_per_session_over_a_day_is_422(client: TestClient) -> None:
    resp = client.post("/habits", json=_habit_body(minutesPerSession=2**31))
    assert resp.status_code == 422, resp.text
    assert resp.json()["field"] == "minutesPerSession"


def test_habit_patch_empty_title_is_422(client: TestClient) -> None:
    created = client.post("/habits", json=_habit_body()).json()
    resp = client.patch(f"/habits/{created['habitId']}", json={"title": ""})
    _assert_korean_422(resp, "title")


# ───────────────────────────── 만다라 ─────────────────────────────


def _subgoals(**titles: str) -> list[dict[str, Any]]:
    return [
        {"orderIndex": i, "title": titles.get(f"t{i}", f"축{i}"), "source": "user"}
        for i in range(8)
    ]


def test_mandala_blank_axis_says_which_axis_in_korean(client: TestClient) -> None:
    resp = client.post(
        "/plans/mandala/generate",
        json={"goalId": f"goal_{uuid4()}", "subgoals": _subgoals(t3="")},
    )
    message = _assert_korean_422(resp, "subgoals.3.title")
    assert message == "축 이름은 1~10자로 적어 주세요."


def test_mandala_axis_over_10_chars_is_korean_422(client: TestClient) -> None:
    resp = client.post(
        "/plans/mandala/generate",
        json={"goalId": f"goal_{uuid4()}", "subgoals": _subgoals(t0="가" * 11)},
    )
    _assert_korean_422(resp, "subgoals.0.title")


def test_mandala_habit_link_long_title_is_korean_422(client: TestClient) -> None:
    resp = client.post(
        f"/goals/mandala/nodes/node_{uuid4()}/habit",
        json={"title": "가" * 201, "frequencyPerWeek": 3, "minutesPerSession": 20},
    )
    _assert_korean_422(resp, "title")


# ── 보이지 않는 문자 (감사 재검증에서 발견) ─────────────────────────────────────
#
# NUL(`\x00`)은 Postgres text 컬럼에 저장되지 않아 그대로 500 이었다. 인박스는 암호화
# 컬럼이라 **저장은 201 로 성공**하고, 그 메모를 목표·할 일로 옮길 때마다 영영 500 이 났다.


def test_nul_byte_in_title_is_stripped_not_500(client: TestClient) -> None:
    res = client.post("/goals", json=_goal_body(title="자료\x00구조 과제"))
    assert res.status_code == 201, res.text
    assert res.json()["title"] == "자료구조 과제"


def test_zero_width_only_title_is_blank(client: TestClient) -> None:
    """폭 없는 공백만 적은 제목은 '보이지 않는 제목'으로 저장되지 않는다."""
    res = client.post("/goals", json=_goal_body(title="​‌"))
    assert res.status_code == 422
    assert res.json()["message"] == "목표 이름을 적어 주세요."


def test_habit_title_strips_control_characters(client: TestClient) -> None:
    res = client.post("/habits", json=_habit_body(title="스트\x07레칭"))
    assert res.status_code == 201, res.text
    assert res.json()["title"] == "스트레칭"


def test_inbox_memo_with_nul_can_still_become_a_goal(client: TestClient) -> None:
    """인박스 메모에 섞인 NUL 이 저장돼 변환이 영구히 막히지 않는다."""
    created = client.post("/inbox", json={"rawText": "널바이트\x00 정리하기"})
    assert created.status_code == 201, created.text
    assert created.json()["rawText"] == "널바이트 정리하기"

    inbox_id = created.json()["inboxId"]
    converted = client.post(
        f"/inbox/{inbox_id}/convert-to-goal",
        json={},
        headers={"Idempotency-Key": f"nul-{uuid4()}"},
    )
    assert converted.status_code in (200, 201), converted.text
