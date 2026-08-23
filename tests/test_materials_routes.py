"""자료 검색 3단계 HITL 흐름 (#259 §5).

이 파일이 지키는 것은 세 결정이다.

  ① **프라이버시** — 1단계는 아무것도 외부로 보내지 않는다. 2단계는 사용자가 준 검색어
     **그것만** 보낸다. 서버가 목표 텍스트를 몰래 덧붙이면 사용자가 편집할 기회가 없어진다.
  ② **HITL** — 2단계 결과는 저장되지 않는다. 3단계 확정을 거쳐야 계획에 닿는다.
  ③ **트리거** — 사용자가 눌러야 돈다(전부 POST). 자동 실행 경로가 없다.

그리고 라이브 실측(2026-08-23)이 만든 요구: **폐기 사유마다 사용자가 할 행동이 다르므로
상태와 문구가 갈라져야 한다.** 저작권 차단은 재시도해도 영영 안 되는데 "잠시 후 다시" 로
안내하면 사용자는 되지 않을 버튼을 계속 누른다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from reaction_backend.api.routes import materials
from reaction_backend.db.session import get_db
from reaction_backend.llm.provider import GroundingSource
from reaction_backend.llm.tool_executor import GroundedResult
from reaction_backend.safety.banned_words import scan
from reaction_backend.schemas.common import now_kst
from reaction_backend.schemas.interview import GoalCandidate
from tests.conftest import DEMO_USER_UUID, FakeInterviewRepo, _FakeSession

_TOC = "섹션 8. 자바 메모리 구조와 static\n섹션 9. final"
_GOAL = "김영한의 실전 자바 - 기본편 완강"


# ─────────────────────────────── 헬퍼 ───────────────────────────────


async def _seed_finished_interview(repo: FakeInterviewRepo, *, goal: str = _GOAL) -> UUID:
    """종료된 인터뷰 1건 — 자료가 붙을 '가장 무거운 목표'가 있어야 한다."""
    session = await repo.create_session(DEMO_USER_UUID, "gemini-3.5-flash-lite")
    session.end_reason = "completed"
    session.ended_at = now_kst()
    await repo.upsert_slot_answer(
        session.id,
        "goals.list",
        {"type": "text", "raw": goal, "normalized": [goal]},
        is_required=True,
    )
    await repo.upsert_slot_answer(
        session.id, "goals.heaviest", {"type": "text", "raw": goal}, is_required=True
    )
    return session.id


def _use_session(client: TestClient, session: _FakeSession) -> None:
    async def _gen() -> AsyncIterator[_FakeSession]:
        yield session

    client.app.dependency_overrides[get_db] = _gen  # type: ignore[attr-defined]


def _result(
    *,
    text: str | None = _TOC,
    reason: str | None = None,
    sources: tuple[GroundingSource, ...] = (GroundingSource("인프런", "https://inflearn.com/x"),),
) -> GroundedResult:
    return GroundedResult(
        text=text,
        sources=sources,
        search_queries=("자바 기본편 커리큘럼",),
        reason=reason,
        prompt_id="planning/materials_search",
        prompt_version="1",
        tokens_in=93,
        tokens_out=307,
        latency_ms=6_500,
        grounding_requests=1,
    )


def _patch_search(monkeypatch: pytest.MonkeyPatch, result: GroundedResult) -> dict[str, Any]:
    """`run_grounded` 를 가로채고 **무엇이 넘어갔는지** 캡처한다."""
    seen: dict[str, Any] = {}

    async def _fake(module: str, prompt_id: str, **kwargs: Any) -> GroundedResult:
        seen["module"] = module
        seen["prompt_id"] = prompt_id
        seen["variables"] = kwargs.get("variables")
        return result

    monkeypatch.setattr(materials.aiClient, "run_grounded", _fake)
    return seen


# ─────────────────────── ① 검색어 제안 ───────────────────────


def test_suggested_query_strips_the_goal_verb() -> None:
    """ "완강"·"완독" 을 그대로 검색하면 자료가 아니라 후기·회고가 먼저 잡힌다."""
    assert (
        materials.suggest_query(GoalCandidate(title=_GOAL, category="study", confidence=0.5))
        == "김영한의 실전 자바 - 기본편 목차 커리큘럼"
    )
    assert (
        materials.suggest_query(
            GoalCandidate(title="모던 자바스크립트 딥다이브 완독", category="study", confidence=0.5)
        )
        == "모던 자바스크립트 딥다이브 목차 커리큘럼"
    )
    # 행위 표현이 없으면 제목을 그대로 둔다.
    assert (
        materials.suggest_query(
            GoalCandidate(title="운영체제 공룡책", category="study", confidence=0.5)
        )
        == "운영체제 공룡책 목차 커리큘럼"
    )


def test_proposing_a_query_never_calls_out(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**1단계는 아무것도 내보내지 않는다** — ① 프라이버시 결정의 핵심.

    사용자가 문자열을 보고 고칠 기회를 갖기 전에 한 글자라도 나가면 그 결정이 무의미해진다.
    `run_grounded` 를 폭발하게 만들어 두고 제안을 받아 본다.
    """

    async def _explode(*a: Any, **k: Any) -> GroundedResult:
        raise AssertionError("1단계에서 외부 호출이 일어났다")

    monkeypatch.setattr(materials.aiClient, "run_grounded", _explode)
    client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post("/plans/materials/search-query", json={})

    assert res.status_code == 200
    body = res.json()
    assert body["suggestedQuery"] == "김영한의 실전 자바 - 기본편 목차 커리큘럼"
    assert body["goalTitle"] == _GOAL
    # 무엇이 나가는지 사용자가 알아야 편집할 이유가 생긴다.
    assert "검색" in body["notice"]


def test_query_proposal_without_an_interview_is_a_clear_422(client: TestClient) -> None:
    assert client.post("/plans/materials/search-query", json={}).status_code == 422


# ─────────────────────── ② 검색 실행 ───────────────────────


def test_search_sends_only_the_user_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """서버가 목표 텍스트를 덧붙이지 않는다 — 나가는 건 사용자가 편집한 문자열뿐이다."""
    _use_session(client, _FakeSession())
    seen = _patch_search(monkeypatch, _result())

    res = client.post("/plans/materials/search", json={"query": "자바 기본편 커리큘럼"})

    assert res.status_code == 200
    assert seen["variables"] == {"query": "자바 기본편 커리큘럼"}
    assert seen["prompt_id"] == "planning/materials_search"


def test_found_result_carries_text_and_sources(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_session(client, _FakeSession())
    _patch_search(monkeypatch, _result())

    body = client.post("/plans/materials/search", json={"query": "자바"}).json()

    assert body["status"] == "found"
    assert body["text"] == _TOC
    # 출처는 사용자 고지용(#259 ⑩) — 없으면 "이 자료를 참고했어요" 를 보여줄 수 없다.
    assert body["sources"] == [{"title": "인프런", "uri": "https://inflearn.com/x"}]
    assert body["searchQueries"] == ["자바 기본편 커리큘럼"]


def test_search_result_is_a_draft(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """② HITL — 검색만으로는 계획에 아무 영향이 없다. 3단계 확정이 게이트다."""
    _use_session(client, _FakeSession())
    _patch_search(monkeypatch, _result())

    body = client.post("/plans/materials/search", json={"query": "자바"}).json()

    assert body["isDraft"] is True
    assert body["aiSource"] == "llm"


def test_search_does_not_touch_the_materials_slot(
    client: TestClient, fake_interview_repo: FakeInterviewRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검색 결과가 슬롯에 새면 사용자가 확정하기 전에 계획이 바뀐다 — ② 결정 위반."""
    _use_session(client, _FakeSession())
    _patch_search(monkeypatch, _result())
    session_id = client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    client.post("/plans/materials/search", json={"query": "자바"})

    rows = client.portal.call(fake_interview_repo.list_slot_answers, session_id)  # type: ignore[attr-defined]
    assert "goals.materials" not in {r.slot_key for r in rows}


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("ungrounded", "not_found"),
        ("empty", "not_found"),
        ("recitation", "blocked_copyright"),
        ("grounding_budget", "quota_exceeded"),
        ("budget", "quota_exceeded"),
        ("timeout", "unavailable"),
        ("provider_error", "unavailable"),
        ("unavailable", "unavailable"),
        (None, "unavailable"),
    ],
)
def test_each_discard_reason_maps_to_the_action_the_user_should_take(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, reason: str | None, status: str
) -> None:
    """**다음에 할 행동이 다르면 상태도 달라야 한다.**

    저작권 차단은 재시도해도 영영 안 되고(provider 가 막는다), 한도 초과는 내일이면 되고,
    못 찾은 건 검색어를 고치면 될 수 있다. 하나로 뭉치면 사용자는 되지 않을 버튼을 누른다.
    """
    _use_session(client, _FakeSession())
    _patch_search(monkeypatch, _result(text=None, reason=reason, sources=()))

    body = client.post("/plans/materials/search", json={"query": "자바"}).json()

    assert body["status"] == status
    assert body["text"] is None
    assert body["notice"], "폐기했으면 왜 그런지 사용자에게 말해야 한다"


def test_copyright_block_offers_the_alternative_instead_of_a_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """라이브 실측: 상업 교재 목차는 provider 가 3/4 로 막는다. 재시도는 소용없다."""
    _use_session(client, _FakeSession())
    _patch_search(monkeypatch, _result(text=None, reason="recitation", sources=()))

    body = client.post("/plans/materials/search", json={"query": "자바"}).json()

    assert body["status"] == "blocked_copyright"
    assert "붙여넣" in body["notice"], "대안(직접 붙여넣기)을 제시해야 한다"


def test_empty_query_is_rejected_before_any_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """빈 검색어로 그라운딩을 태우면 건당 과금만 쓰고 아무것도 못 얻는다."""
    _use_session(client, _FakeSession())
    seen = _patch_search(monkeypatch, _result())

    res = client.post("/plans/materials/search", json={"query": ""})

    assert res.status_code == 422
    assert seen == {}, "검증 전에 provider 를 불렀다"


def test_notices_pass_the_banned_word_filter() -> None:
    """안내 문구는 **우리가 쓴 사용자 노출 문자열**이다 — 잠금된 톤 사전을 지켜야 한다.

    LLM 출력이 아니라 `enforce()` 를 거치지 않으므로 여기서 직접 확인한다
    (DevBaseline §4.2).
    """
    notices = [n for _, n in materials._DISCARD_NOTICE.values()]
    notices += [materials._UNAVAILABLE_NOTICE, materials._FOUND_NOTICE]
    for notice in notices:
        assert scan(notice) == (), f"금지어가 들어간 문구: {notice!r}"


# ─────────────────────── ③ 사용자 확정 ───────────────────────


def test_confirm_writes_into_the_same_slot_a_paste_would(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    """확정 자료는 **붙여넣기와 같은 자리**(`goals.materials`)에 들어간다.

    그래서 계획 생성 쪽에 검색 전용 분기가 없다 — 기존 경로가 그대로 집어간다.
    """
    _use_session(client, _FakeSession())
    session_id = client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post("/plans/materials/confirm", json={"text": _TOC})

    assert res.status_code == 200
    assert res.json()["savedChars"] == len(_TOC)
    rows = client.portal.call(fake_interview_repo.list_slot_answers, session_id)  # type: ignore[attr-defined]
    saved = {r.slot_key: r.value for r in rows}
    assert saved["goals.materials"] == {"type": "text", "raw": _TOC}


def test_confirm_accepts_user_edits(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    """검색이 다른 판을 가져왔을 때 지우고 붙일 수 있어야 HITL 이다."""
    _use_session(client, _FakeSession())
    session_id = client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]
    edited = "1장 내가 직접 고친 목차"

    client.post("/plans/materials/confirm", json={"text": edited})

    rows = client.portal.call(fake_interview_repo.list_slot_answers, session_id)  # type: ignore[attr-defined]
    assert {r.slot_key: r.value for r in rows}["goals.materials"]["raw"] == edited


def test_confirm_without_an_interview_is_a_clear_422(client: TestClient) -> None:
    _use_session(client, _FakeSession())
    assert client.post("/plans/materials/confirm", json={"text": _TOC}).status_code == 422


# ─────────────────────── ③ 트리거 · 인증 ───────────────────────


def test_all_three_steps_are_post_only(client: TestClient) -> None:
    """③ 트리거 결정 — 사용자가 눌러야 돈다. GET 으로 새어 자동 실행되면 안 된다."""
    for path in (
        "/plans/materials/search-query",
        "/plans/materials/search",
        "/plans/materials/confirm",
    ):
        assert client.get(path).status_code == 405, path


def test_search_requires_auth(unauthed_client: TestClient) -> None:
    res = unauthed_client.post("/plans/materials/search", json={"query": "자바"})
    assert res.status_code in (401, 403)
