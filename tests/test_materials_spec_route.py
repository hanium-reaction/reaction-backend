"""ADR-0010 자료 상세 확정 — `/plans/materials/book-detail` · `video-detail` · `spec-confirm`.

이 파일이 지키는 것:

  ① **book/video-detail 은 저장하지 않는다** — 조회만. `spec-confirm` 을 사용자가 따로
     눌러야 `goals.materials` 슬롯에 쓰인다(② HITL 왕복).
  ② **spec-confirm 은 계획 생성에 반영된다(ADR-0010 §5)** — `interview_adapter.
     _materials_note` 가 `type="spec"` 값을 텍스트로 풀어 기존 `materials_for_prompt`
     경로를 그대로 태운다. 그 반영 자체(분해 결과가 실제로 달라지는지)는
     `test_interview_adapter_materials_spec.py` 가 검증한다 — 여기는 라우터 경계만 본다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reaction_backend.api.routes import materials
from reaction_backend.schemas.materials_spec import (
    BookChapter,
    BookDetailResponse,
    BookSpecDetail,
    VideoDetailResponse,
    VideoSpecDetail,
    VideoSpecItem,
)
from tests.conftest import FakeInterviewRepo, _FakeSession
from tests.test_materials_routes import _GOAL, _seed_finished_interview, _use_session


def _stub_book_detail(response: BookDetailResponse) -> Any:
    async def _stub(*a: Any, **k: Any) -> BookDetailResponse:
        return response

    return _stub


def _stub_video_detail(response: VideoDetailResponse) -> Any:
    async def _stub(*a: Any, **k: Any) -> VideoDetailResponse:
        return response

    return _stub


# ─────────────────────────── book-detail ───────────────────────────


def test_book_detail_returns_pages_and_toc(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    detail = BookSpecDetail(
        title="Java의 정석 : 기초편",
        author="남궁성",
        isbn13="9788994492049",
        page_count=1000,
        chapters=[BookChapter(title="Chapter 1. 자바를 시작하기 전에", end_page=30)],
        toc_source="seoji",
    )
    monkeypatch.setattr(
        materials.materials_spec,
        "book_detail",
        _stub_book_detail(BookDetailResponse(detail=detail)),
    )

    res = client.post("/plans/materials/book-detail", json={"isbn13": "9788994492049"})

    assert res.status_code == 200
    body = res.json()
    assert body["detail"]["pageCount"] == 1000
    assert body["detail"]["tocSource"] == "seoji"
    assert body["detail"]["chapters"] == [
        {"title": "Chapter 1. 자바를 시작하기 전에", "endPage": 30}
    ]
    assert body["notice"] is None


def test_book_detail_failure_reports_notice_without_500(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setattr(
        materials.materials_spec,
        "book_detail",
        _stub_book_detail(BookDetailResponse(notice="이 도서 정보를 찾지 못했어요.")),
    )

    res = client.post("/plans/materials/book-detail", json={"isbn13": "0000000000000"})

    assert res.status_code == 200
    body = res.json()
    assert body["detail"] is None
    assert body["notice"] == "이 도서 정보를 찾지 못했어요."


def test_book_detail_requires_auth(unauthed_client: TestClient) -> None:
    res = unauthed_client.post("/plans/materials/book-detail", json={"isbn13": "9788994492049"})
    assert res.status_code == 401


# ─────────────────────────── video-detail ───────────────────────────


def test_video_detail_returns_curriculum_and_minutes(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    detail = VideoSpecDetail(
        title="구자연의 자연스러운 문법",
        channel_title="해커스토익",
        playlist_id="PL1",
        playlist_url="https://www.youtube.com/playlist?list=PL1",
        video_count=10,
        total_minutes=622,
        curriculum=[VideoSpecItem(title="1일차 시제", minutes=54)],
    )
    monkeypatch.setattr(
        materials.materials_spec,
        "video_detail",
        _stub_video_detail(VideoDetailResponse(detail=detail)),
    )

    res = client.post("/plans/materials/video-detail", json={"playlistId": "PL1"})

    assert res.status_code == 200
    body = res.json()
    assert body["detail"]["totalMinutes"] == 622
    assert body["detail"]["curriculum"][0]["title"] == "1일차 시제"
    assert body["notice"] is None


def test_video_detail_quota_exceeded_reports_notice_without_500(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setattr(
        materials.materials_spec,
        "video_detail",
        _stub_video_detail(
            VideoDetailResponse(
                notice="오늘 쓸 수 있는 영상 조회를 다 썼어요. 내일 다시 시도해 주세요."
            )
        ),
    )

    res = client.post("/plans/materials/video-detail", json={"playlistId": "PL1"})

    assert res.status_code == 200
    body = res.json()
    assert body["detail"] is None
    assert "다 썼어요" in body["notice"]


def test_video_detail_requires_auth(unauthed_client: TestClient) -> None:
    res = unauthed_client.post("/plans/materials/video-detail", json={"playlistId": "PL1"})
    assert res.status_code == 401


# ─────────────────────────── spec-confirm ───────────────────────────


_BOOK_DETAIL_JSON = {
    "kind": "book",
    "title": "Java의 정석 : 기초편",
    "author": "남궁성",
    "isbn13": "9788994492049",
    "pageCount": 1000,
    "chapters": [{"title": "Chapter 1. 자바를 시작하기 전에", "endPage": 30}],
    "tocSource": "seoji",
}
_VIDEO_DETAIL_JSON = {
    "kind": "video",
    "title": "구자연의 자연스러운 문법",
    "channelTitle": "해커스토익",
    "playlistId": "PL1",
    "playlistUrl": "https://www.youtube.com/playlist?list=PL1",
    "videoCount": 10,
    "totalMinutes": 622,
    "curriculum": [{"title": "1일차 시제", "minutes": 54}],
    "truncated": False,
}


def test_spec_confirm_writes_book_spec_into_the_materials_slot(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    _use_session(client, _FakeSession())
    session_id = client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post("/plans/materials/spec-confirm", json={"details": [_BOOK_DETAIL_JSON]})

    assert res.status_code == 200
    body = res.json()
    assert body["goalTitle"] == _GOAL
    assert body["kinds"] == ["book"]
    # ADR-0010 §5 — 이제 실제로 반영된다(interview_adapter._materials_note 가 텍스트로
    # 풀어 기존 materials_for_prompt 경로를 그대로 탄다). "저장만 하고 반영 안 함" 이 아니다.
    assert "반영" in body["notice"]
    # 이 인터뷰는 마감을 안 물었다 — 나눌 기간이 없으니 진도를 지어내지 않는다.
    assert body["bookPace"] is None

    rows = client.portal.call(fake_interview_repo.list_slot_answers, session_id)  # type: ignore[attr-defined]
    saved = {r.slot_key: r.value for r in rows}
    assert saved["goals.materials"]["type"] == "spec"
    assert saved["goals.materials"]["items"][0]["kind"] == "book"
    assert saved["goals.materials"]["items"][0]["page_count"] == 1000


def test_spec_confirm_book_pace_respects_chapter_boundaries(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    """ "목차가 있으면 챕터별 실제 페이지 체크포인트로 진행하는가" — 마감·빈도가 있는
    인터뷰에서 목차 전 챕터에 페이지가 있으면, `bookPace.chapters` 가 챕터마다 정수 세션을
    배정하고(균등 분할이 아니다) 그 합이 `bookPace.totalSessions` 와 같아야 한다. 슬롯에
    저장되는 값도 같은 배정이어야 한다(다음 계획 생성에 그대로 실린다)."""
    _use_session(client, _FakeSession())
    session_id = client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]
    deadline = (date.today() + timedelta(days=70)).isoformat()

    async def _seed_deadline_and_frequency() -> None:
        await fake_interview_repo.upsert_slot_answer(
            session_id, "goals.deadlines", {"type": "text", "raw": deadline}, is_required=True
        )
        await fake_interview_repo.upsert_slot_answer(
            session_id,
            "goals.frequency",
            {"type": "chip", "values": ["주 5회"]},
            is_required=True,
        )

    client.portal.call(_seed_deadline_and_frequency)  # type: ignore[attr-defined]

    book = {
        "kind": "book",
        "title": "테스트북",
        "author": "저자",
        "isbn13": "9780000000000",
        "pageCount": 1000,
        "chapters": [
            {"title": "Chapter 1", "endPage": 200},
            {"title": "Chapter 2", "endPage": 500},
            {"title": "Chapter 3", "endPage": 1000},
        ],
        "tocSource": "seoji",
    }

    res = client.post("/plans/materials/spec-confirm", json={"details": [book]})

    assert res.status_code == 200
    pace = res.json()["bookPace"]
    assert pace is not None
    assert [c["title"] for c in pace["chapters"]] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    # 1000쪽이 목차로 전부 커버되니(마지막 endPage == pageCount) 나머지 항목은 없다.
    assert sum(c["sessions"] for c in pace["chapters"]) == pace["totalSessions"]
    assert all(c["sessions"] >= 1 for c in pace["chapters"])

    rows = client.portal.call(fake_interview_repo.list_slot_answers, session_id)  # type: ignore[attr-defined]
    saved = {r.slot_key: r.value for r in rows}
    saved_chapters = saved["goals.materials"]["items"][0]["chapters"]
    assert saved_chapters == [
        {"title": c["title"], "end_page": c["endPage"], "sessions": c["sessions"]}
        for c in pace["chapters"]
    ]


def test_spec_confirm_writes_video_spec_into_the_materials_slot(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    _use_session(client, _FakeSession())
    session_id = client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post("/plans/materials/spec-confirm", json={"details": [_VIDEO_DETAIL_JSON]})

    assert res.status_code == 200
    assert res.json()["kinds"] == ["video"]

    rows = client.portal.call(fake_interview_repo.list_slot_answers, session_id)  # type: ignore[attr-defined]
    saved = {r.slot_key: r.value for r in rows}
    assert saved["goals.materials"]["items"][0]["kind"] == "video"
    assert saved["goals.materials"]["items"][0]["curriculum"] == [
        {"title": "1일차 시제", "minutes": 54}
    ]


def test_spec_confirm_combines_book_and_video_when_both_are_sent(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    """materialMix="both" 경로 — 사용자가 책과 영상을 같이 확정하면 둘 다 슬롯에 담긴다."""
    _use_session(client, _FakeSession())
    session_id = client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]

    res = client.post(
        "/plans/materials/spec-confirm",
        json={"details": [_BOOK_DETAIL_JSON, _VIDEO_DETAIL_JSON]},
    )

    assert res.status_code == 200
    assert res.json()["kinds"] == ["book", "video"]

    rows = client.portal.call(fake_interview_repo.list_slot_answers, session_id)  # type: ignore[attr-defined]
    saved = {r.slot_key: r.value for r in rows}
    assert [item["kind"] for item in saved["goals.materials"]["items"]] == ["book", "video"]


def test_spec_confirm_rejects_two_books(client: TestClient) -> None:
    """책 1개·영상 1개까지 — 같은 종류를 두 번 보내면 스키마 단계에서 거절된다."""
    other_book = {**_BOOK_DETAIL_JSON, "isbn13": "9999999999999"}
    res = client.post(
        "/plans/materials/spec-confirm",
        json={"details": [_BOOK_DETAIL_JSON, other_book]},
    )
    assert res.status_code == 422


def test_spec_confirm_replaces_a_previously_confirmed_text_material(
    client: TestClient, fake_interview_repo: FakeInterviewRepo
) -> None:
    """③(`/confirm`, 텍스트)과 spec-confirm 은 같은 슬롯을 쓴다 — 마지막 확정이 이긴다."""
    _use_session(client, _FakeSession())
    session_id = client.portal.call(_seed_finished_interview, fake_interview_repo)  # type: ignore[attr-defined]
    client.post("/plans/materials/confirm", json={"text": "1장 목차"})

    client.post("/plans/materials/spec-confirm", json={"details": [_BOOK_DETAIL_JSON]})

    rows = client.portal.call(fake_interview_repo.list_slot_answers, session_id)  # type: ignore[attr-defined]
    saved = {r.slot_key: r.value for r in rows}
    assert saved["goals.materials"]["type"] == "spec"


def test_spec_confirm_without_an_interview_is_a_clear_422(client: TestClient) -> None:
    _use_session(client, _FakeSession())
    res = client.post("/plans/materials/spec-confirm", json={"details": [_BOOK_DETAIL_JSON]})
    assert res.status_code == 422


def test_spec_confirm_rejects_an_unknown_kind(client: TestClient) -> None:
    """discriminator 가 book/video 가 아닌 값은 스키마 단계에서 거절된다."""
    res = client.post(
        "/plans/materials/spec-confirm",
        json={"details": [{"kind": "podcast", "title": "팟캐스트"}]},
    )
    assert res.status_code == 422


def test_spec_confirm_requires_auth(unauthed_client: TestClient) -> None:
    res = unauthed_client.post(
        "/plans/materials/spec-confirm", json={"details": [_BOOK_DETAIL_JSON]}
    )
    assert res.status_code == 401
