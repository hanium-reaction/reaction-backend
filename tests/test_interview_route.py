"""Interview route (#6 배선) — mock 걷어내고 엔진+영속화 연결 검증.

ADR-0005 §7.3 패턴대로 aiClient.run 만 stub. 라우터→interview_runner→노드 경로와
interview_sessions/slot_answers 재조립·영속(FakeInterviewRepo)을 HTTP 레벨로 검증한다.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from reaction_backend.llm import RunResult, aiClient
from reaction_backend.schemas.interview import (
    AmbiguityUpdate,
    InterviewSummary,
    NextQuestionSchema,
)
from tests.conftest import FakeInterviewRepo


def _stub(*, clarity: float = 0.9, fell_back: bool = False) -> Any:
    """aiClient.run stub — clarity 높게 두어 답을 저장, 종료는 FSM(필수 슬롯)이 운전."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(
                question="다음 질문이에요",
                clarity_score=clarity,
                normalized_value=None,
                empathy_one_liner="좋아요",
            )
        elif schema is AmbiguityUpdate:
            value = AmbiguityUpdate(
                slot_key=kwargs["variables"]["slot_key"],
                clarity_score=clarity,
                new_ambiguity=0.9,
            )
        elif schema is InterviewSummary:
            value = InterviewSummary(
                headline="요약",
                goal_summary="목표 요약",
                time_summary="시간 요약",
                preference_summary="선호 요약",
                confirm_question="이대로 계획을 세워볼까요?",
            )
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {schema}")
        return RunResult(
            value=value,
            fell_back=fell_back,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    return stub_run


def test_start_returns_first_question(client: TestClient, monkeypatch: Any) -> None:
    """세션 시작 → FSM 첫 필수 슬롯(identity.role) 질문 + chip 보기 동봉."""
    monkeypatch.setattr(aiClient, "run", _stub())

    res = client.post("/interview/sessions")
    assert res.status_code == 201
    body = res.json()

    assert body["currentQuestion"]["slotKey"] == "identity.role"
    assert body["currentQuestion"]["answerType"] == "chip"
    assert "1학년" in body["currentQuestion"]["options"]  # 카탈로그 보기 매핑
    assert body["ambiguityScore"] == 13  # 미해결 필수 슬롯 수
    assert body["endReason"] is None


def test_submit_advances_and_persists(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: Any
) -> None:
    """답 제출 → 다음 슬롯으로 진행 + DB(slot_answers) 영속."""
    monkeypatch.setattr(aiClient, "run", _stub())

    start = client.post("/interview/sessions").json()
    sid = start["sessionId"]

    res = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["currentQuestion"]["slotKey"] == "identity.season"  # 다음 필수 슬롯
    assert body["ambiguityScore"] == 12  # 하나 채워져 감소

    # 영속화 검증 — fake repo 에 세션 1개 + identity.role 답 저장
    assert len(fake_interview_repo._sessions) == 1
    stored = fake_interview_repo._answers[next(iter(fake_interview_repo._sessions))]
    assert "identity.role" in stored
    assert stored["identity.role"].value == {"type": "chip", "values": ["3학년"]}


def test_finish_returns_summary_and_outcome(client: TestClient, monkeypatch: Any) -> None:
    """[충분해요] 조기 종료 → end_reason + summary(S03) + outcome(First Plan 시드)."""
    monkeypatch.setattr(aiClient, "run", _stub())

    sid = client.post("/interview/sessions").json()["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )

    res = client.post(f"/interview/sessions/{sid}/finish")
    assert res.status_code == 200
    body = res.json()
    assert body["endReason"] == "early_user"
    assert body["currentQuestion"] is None
    assert body["summary"]["confirmQuestion"]  # S03 확인 카드
    assert body["outcome"]["sessionId"] == sid  # First Plan 시드
    assert "identity.role" not in body["outcome"]["unresolvedSlots"]  # 채운 슬롯


def test_slot_catalog_includes_options(client: TestClient) -> None:
    """슬롯 카탈로그가 chip 보기를 노출(텍스트 슬롯은 빈 배열)."""
    res = client.get("/interview/slot-catalog")
    assert res.status_code == 200
    by_key = {e["slotKey"]: e for e in res.json()}
    assert "1학년" in by_key["identity.role"]["options"]
    assert by_key["identity.major"]["options"] == []  # text 슬롯


def test_unknown_session_returns_404(client: TestClient, monkeypatch: Any) -> None:
    monkeypatch.setattr(aiClient, "run", _stub())
    res = client.post(
        f"/interview/sessions/{uuid4()}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    assert res.status_code == 404


def test_start_with_active_session_returns_409(client: TestClient, monkeypatch: Any) -> None:
    """단일 활성 세션 enforce — 진행 중 세션이 있는데 또 시작하면 409."""
    monkeypatch.setattr(aiClient, "run", _stub())

    first = client.post("/interview/sessions")
    assert first.status_code == 201  # 첫 세션은 생성

    second = client.post("/interview/sessions")  # 진행 중 세션 존재
    assert second.status_code == 409
    assert second.json()["code"] == "INTERVIEW_SESSION_EXISTS"


def test_start_after_finish_is_allowed(client: TestClient, monkeypatch: Any) -> None:
    """종료(end_reason 채워짐)된 세션은 활성으로 치지 않음 — 새 세션 시작 가능."""
    monkeypatch.setattr(aiClient, "run", _stub())

    sid = client.post("/interview/sessions").json()["sessionId"]
    assert client.post(f"/interview/sessions/{sid}/finish").status_code == 200

    again = client.post("/interview/sessions")  # 이전 세션 종료됨 → 충돌 없음
    assert again.status_code == 201


def test_concurrent_access_returns_409(client: TestClient, monkeypatch: Any) -> None:
    """동시성 lock — advisory lock 미획득 시 409 AGENT_CONCURRENT_ACCESS (ADR-0005 §7.6)."""
    from collections.abc import AsyncIterator

    from reaction_backend.db.session import get_db
    from tests.conftest import _FakeSession  # noqa: PLC0415

    monkeypatch.setattr(aiClient, "run", _stub())

    async def _locked_session() -> AsyncIterator[_FakeSession]:
        # 다른 디바이스가 lock 보유 중 → pg_try_advisory_lock 이 False.
        yield _FakeSession(lock_acquired=False)

    client.app.dependency_overrides[get_db] = _locked_session  # type: ignore[attr-defined]

    res = client.post("/interview/sessions")
    assert res.status_code == 409
    assert res.json()["code"] == "AGENT_CONCURRENT_ACCESS"
