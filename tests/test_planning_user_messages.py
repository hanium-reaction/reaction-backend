"""계획 API 가 화면에 그대로 뜨는 문구로 거절한다.

- planA-16: 인터뷰를 안 마친 사용자에게 요청 필드 이름('outcome/interviewSessionId')을 말하지 않는다.
- planA-4: 너무 긴 중간 목표는 생성 단계에서 한국어로 거절한다 — 예전엔 생성은 통과하고
  승인(goal_nodes.title 200자)에서만 터져, 그 초안은 몇 번을 눌러도 승인되지 않았다.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from reaction_backend.llm import aiClient


@pytest.mark.parametrize("path", ["/plans/generate", "/plans/milestones"])
def test_no_finished_interview_is_explained_without_field_names(
    path: str, client: TestClient
) -> None:
    res = client.post(path, json={})

    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "COMMON_VALIDATION_ERROR"
    assert "인터뷰" in body["message"]
    for jargon in ("outcome", "interviewSessionId", "보내주세요"):
        assert jargon not in body["message"]


def _never_llm(monkeypatch: Any) -> None:
    async def never(**kwargs: Any) -> Any:
        raise AssertionError("검증에서 막혀야 할 요청이 LLM 까지 갔다")

    monkeypatch.setattr(aiClient, "run", never)


@pytest.mark.parametrize(
    ("milestones", "expected"),
    [
        ([{"title": "가" * 201, "summary": ""}], "중간 목표 이름은 200자까지"),
        ([{"title": "문법", "summary": "나" * 501}], "중간 목표 설명은 500자까지"),
        ([{"title": f"단계 {i}", "summary": ""} for i in range(11)], "중간 목표는 10개까지"),
    ],
)
def test_milestones_that_would_never_save_are_rejected_before_planning(
    milestones: list[dict[str, str]], expected: str, client: TestClient, monkeypatch: Any
) -> None:
    _never_llm(monkeypatch)

    res = client.post("/plans/generate", json={"milestones": milestones})

    assert res.status_code == 422
    assert res.json()["code"] == "COMMON_VALIDATION_ERROR"
    assert expected in res.json()["message"]
    assert res.json()["field"] == "milestones"


def test_milestones_at_the_limits_pass_validation(client: TestClient, monkeypatch: Any) -> None:
    """딱 200자 이름·500자 설명·10개는 받는다 — 그 다음(인터뷰 없음 422)까지 간다."""
    _never_llm(monkeypatch)
    milestones = [{"title": "가" * 200, "summary": "나" * 500}] + [
        {"title": f"단계 {i}", "summary": ""} for i in range(9)
    ]

    res = client.post("/plans/generate", json={"milestones": milestones})

    assert res.status_code == 422
    assert "인터뷰" in res.json()["message"]  # 검증은 통과, 인터뷰가 없어서 멈춘 것


def test_a_bad_week_start_is_explained_without_the_field_name(client: TestClient) -> None:
    """주간 그리드의 날짜 오류 문구에 'weekStart' 가 없다 — 사용자에겐 뜻 없는 말이다.

    어느 값이 문제인지는 `field` 가 그대로 들고 있다(FE 가 쓰는 자리).
    """
    res = client.get("/plans/weekly", params={"weekStart": "오늘"})

    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "PLAN_INVALID_TIME"
    assert body["field"] == "weekStart"
    assert "weekStart" not in body["message"]
    assert body["message"] == "날짜 형식이 올바르지 않아요 (YYYY-MM-DD)."
