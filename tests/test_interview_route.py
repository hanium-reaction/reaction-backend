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
    SlotHarvest,
)
from tests.conftest import FakeInterviewRepo


def _stub(
    *,
    clarity: float = 0.9,
    new_ambiguity: float = 0.9,
    suggested: tuple[str, ...] = (),
    fell_back: bool = False,
) -> Any:
    """aiClient.run stub — clarity 높게 두어 답을 저장, 종료는 FSM(필수 슬롯)이 운전."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(
                question="다음 질문이에요",
                empathy_one_liner="좋아요",
                suggested_answers=list(suggested),
            )
        elif schema is AmbiguityUpdate:
            value = AmbiguityUpdate(
                slot_key=kwargs["variables"]["slot_key"],
                clarity_score=clarity,
                new_ambiguity=new_ambiguity,
            )
        elif schema is InterviewSummary:
            value = InterviewSummary(
                headline="요약",
                goal_summary="목표 요약",
                time_summary="시간 요약",
                preference_summary="선호 요약",
                confirm_question="이대로 계획을 세워볼까요?",
            )
        elif schema is SlotHarvest:
            # 기본 stub 은 하베스팅 없음(빈 추출) — 기존 슬롯 진행 검증에 영향 없게.
            value = SlotHarvest(slots=[])
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
    assert body["ambiguityScore"] == 17  # 미해결 필수 슬롯 수 (goals.materials 추가 #materials)
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
    assert body["ambiguityScore"] == 16  # 하나 채워져 감소 (필수 17개, #materials)

    # 영속화 검증 — fake repo 에 세션 1개 + identity.role 답 저장
    assert len(fake_interview_repo._sessions) == 1
    stored = fake_interview_repo._answers[next(iter(fake_interview_repo._sessions))]
    assert "identity.role" in stored
    assert stored["identity.role"].value == {"type": "chip", "values": ["3학년"]}


def test_submit_does_not_finish_until_required_slots_are_filled(
    client: TestClient, monkeypatch: Any
) -> None:
    """LLM ambiguity 가 낮아도 미해결 필수 슬롯이 남아 있으면 다음 질문을 계속한다."""
    monkeypatch.setattr(aiClient, "run", _stub(new_ambiguity=0.1))

    start = client.post("/interview/sessions").json()
    sid = start["sessionId"]

    res = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ambiguityScore"] == 16
    assert body["endReason"] is None
    assert body["currentQuestion"]["slotKey"] == "identity.season"


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
    assert body["outcome"]["analysisSource"] == "llm"  # fallback 없음 → llm


def test_profile_persist_failure_does_not_break_finish(
    client: TestClient, monkeypatch: Any
) -> None:
    """프로필 영속이 예외를 던져도 인터뷰 완료(finish)는 200 으로 성공한다 (#130 best-effort).

    프로필 메모리 영속은 부가 기능이라, 실패가 절대 안 깨져야 하는 finalize 경로를 막으면 안 된다.
    """
    monkeypatch.setattr(aiClient, "run", _stub())

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("profile write blew up")

    monkeypatch.setattr(
        "reaction_backend.orchestrator.profile_memory.persist_profile_from_outcome", _boom
    )

    sid = client.post("/interview/sessions").json()["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    res = client.post(f"/interview/sessions/{sid}/finish")
    assert res.status_code == 200  # 프로필 실패에도 인터뷰 완료 성공
    body = res.json()
    assert body["endReason"] == "early_user"
    assert body["outcome"]["sessionId"] == sid


def test_finish_materializes_extracted_goals(client: TestClient, monkeypatch: Any) -> None:
    """[충분해요] 조기 종료도 추출한 목표를 materialize 한다 (완료 경로 submit_answer 와 대칭).

    회귀: 예전엔 finish_session 에 materialize_goals 가 없어, [충분해요] 로 끝낸 사용자는
    목표 분류 화면이 빈 상태가 됐다 (사용자 테스트로 발견).
    """
    monkeypatch.setattr(aiClient, "run", _stub())
    seen: dict[str, Any] = {}

    async def _spy(session: Any, *, user_id: Any, core_goals: Any) -> None:
        seen["called"] = True
        seen["n"] = len(core_goals)

    monkeypatch.setattr("reaction_backend.orchestrator.first_plan_adapter.materialize_goals", _spy)

    sid = client.post("/interview/sessions").json()["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "goals.list", "value": "포트폴리오 사이트, 토익", "clientTurn": 1},
    )
    res = client.post(f"/interview/sessions/{sid}/finish")
    assert res.status_code == 200
    assert seen.get("called") is True  # 조기 종료도 목표 영속 시도
    assert seen.get("n", 0) >= 1


def test_used_fallback_persists_to_analysis_source(client: TestClient, monkeypatch: Any) -> None:
    """이전 턴에 LLM 룰 fallback 이 있었으면, 재조립을 넘어 outcome.analysisSource='rule'.

    used_fallback 은 턴마다 재조립되는 transient 라 세션 컬럼에 OR 누적 영속돼야 전체 인터뷰
    기준으로 정확하다 (마지막 턴만 보고 'llm' 로 잘못 찍히던 것을 고침)."""
    monkeypatch.setattr(aiClient, "run", _stub(fell_back=True))  # 시작 턴부터 fallback
    sid = client.post("/interview/sessions").json()["sessionId"]

    # 이후 턴은 정상(fallback 아님) — 그래도 앞선 fallback 이 영속돼야 한다
    monkeypatch.setattr(aiClient, "run", _stub(fell_back=False))
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    body = client.post(f"/interview/sessions/{sid}/finish").json()
    assert body["outcome"]["analysisSource"] == "rule"

    # 재조회(이미 종료된 세션)도 동일하게 영속된 플래그 기준
    again = client.get(f"/interview/sessions/{sid}").json()
    assert again["outcome"]["analysisSource"] == "rule"


def test_critical_slot_reask_persists_attempts_across_db(
    client: TestClient, monkeypatch: Any
) -> None:
    """핵심 슬롯 재질문 시 시도 횟수가 DB 왕복(재조립)을 넘어 누적돼, 상한 후 진행한다.

    매 턴 라우터가 slot_answers 를 DB(fake)에 영속하고 다시 재조립하는데, pending 마커가
    그 왕복을 견뎌야 3회차에서 best-effort 로 goals.list 를 벗어난다(무한 재질문 방지)."""

    async def stub_run(**kwargs: Any) -> RunResult[Any]:
        schema = kwargs["schema"]
        if schema is NextQuestionSchema:
            value: Any = NextQuestionSchema(question="다음 질문", empathy_one_liner="좋아요")
        elif schema is SlotHarvest:
            value = SlotHarvest(slots=[])  # 자유서술 답이라 하베스팅 호출됨 — 추출 없음으로 고정
        else:  # AmbiguityUpdate — goals.list 는 계속 애매(재질문), 나머지는 유효
            slot = kwargs["variables"]["slot_key"]
            value = AmbiguityUpdate(
                slot_key=slot,
                clarity_score=0.1 if slot == "goals.list" else 0.9,
                new_ambiguity=0.5,
            )
        return RunResult(
            value=value,
            fell_back=False,
            reason=None,
            prompt_id=kwargs["prompt_id"],
            prompt_version="v1",
        )

    monkeypatch.setattr(aiClient, "run", stub_run)

    sid = client.post("/interview/sessions").json()["sessionId"]

    def answer(slot: str, val: Any) -> dict[str, Any]:
        return client.post(
            f"/interview/sessions/{sid}/answers",
            json={"slotKey": slot, "value": val, "clientTurn": 1},
        ).json()

    # identity 두 chip 슬롯을 유효하게 채워 goals.list 에 도달
    answer("identity.role", ["3학년"])
    body = answer("identity.season", ["방학"])
    assert body["currentQuestion"]["slotKey"] == "goals.list"
    amb_at_goals = body["ambiguityScore"]

    # 1·2회차: 애매한 자유서술 → 재질문 (ambiguityScore 그대로, 여전히 goals.list)
    body = answer("goals.list", "음 그냥 이것저것")
    assert body["currentQuestion"]["slotKey"] == "goals.list"
    assert body["ambiguityScore"] == amb_at_goals  # pending — 미충족 유지
    body = answer("goals.list", "여전히 잘 정리가 안돼")
    assert body["currentQuestion"]["slotKey"] == "goals.list"
    assert body["ambiguityScore"] == amb_at_goals

    # 3회차(상한): DB 를 넘어 누적된 시도 → best-effort 채택하고 다음 슬롯으로.
    # 목표가 1개(단일)라 goals.heaviest 가 자동 채워짐 → heaviest 질문을 건너뛰고
    # 남은 필수 슬롯이 2개 감소, 다음은 goals.current_level (#B — heaviest 다음 슬롯).
    body = answer("goals.list", "그럼 프로젝트 하나 할래")
    assert body["currentQuestion"]["slotKey"] == "goals.current_level"
    assert body["ambiguityScore"] == amb_at_goals - 2  # goals.list + goals.heaviest 충족


def test_suggested_answers_only_for_free_text_slots(client: TestClient, monkeypatch: Any) -> None:
    """LLM 추천 답변 카드(suggestedAnswers)는 고정 보기가 없는 자유서술 슬롯에서만 노출된다.

    chip(카탈로그 보기 있음) → suggestedAnswers 빈 배열 / goals.list(자유서술) → LLM 추천 노출."""
    monkeypatch.setattr(aiClient, "run", _stub(suggested=("캡스톤 마무리", "토익 900점")))

    # 첫 질문 identity.role — chip 보기가 있으니 추천 카드는 비어야
    start = client.post("/interview/sessions").json()
    assert start["currentQuestion"]["slotKey"] == "identity.role"
    assert start["currentQuestion"]["options"]  # 카탈로그 보기 존재
    assert start["currentQuestion"]["suggestedAnswers"] == []

    sid = start["sessionId"]
    client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.role", "value": ["3학년"], "clientTurn": 1},
    )
    body = client.post(
        f"/interview/sessions/{sid}/answers",
        json={"slotKey": "identity.season", "value": ["방학"], "clientTurn": 2},
    ).json()

    # goals.list — 자유서술(카탈로그 보기 없음) → LLM 추천 카드 노출
    assert body["currentQuestion"]["slotKey"] == "goals.list"
    assert body["currentQuestion"]["options"] == []
    assert body["currentQuestion"]["suggestedAnswers"] == ["캡스톤 마무리", "토익 900점"]


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


def test_start_with_active_session_abandons_old_and_creates(
    client: TestClient, monkeypatch: Any
) -> None:
    """재시작 승리(restart-wins) — 진행 중 세션이 있어도 abandoned 로 닫고 새로 시작(201).

    FE 가 sessionId 를 잃어도 재시작만으로 복구된다 (이전 409 는 영구 차단이었다).
    """
    monkeypatch.setattr(aiClient, "run", _stub())

    first = client.post("/interview/sessions")
    assert first.status_code == 201  # 첫 세션은 생성
    first_sid = first.json()["sessionId"]

    second = client.post("/interview/sessions")  # 진행 중 세션 존재 → abandon 후 새로
    assert second.status_code == 201
    second_sid = second.json()["sessionId"]
    assert second_sid != first_sid

    old = client.get(f"/interview/sessions/{first_sid}")
    assert old.status_code == 200
    assert old.json()["endReason"] == "abandoned"


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
