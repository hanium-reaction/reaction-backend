"""YouTube 재생목록 클라이언트 (ADR-0010 §1 ②③) — 검색(후보만)·상세(커리큘럼+분량).

`quota_exceeded` 를 별도 사유로 가르는 이유: `search.list` 가 100유닛/앱 전체 일일 쿼터
10,000유닛이라 하루 ~100회가 상한이다(`config.youtube_api_key` 주석) — 이 경로를 다른
실패와 뭉개면 "오늘은 안 됨" 과 "그냥 안 됨" 을 사용자에게 구분해 알릴 수 없다.

네트워크는 타지 않는다 — `requests.get` 을 대체한다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from reaction_backend.integrations.youtube import client


class _FakeResponse:
    def __init__(self, *, status: int = 200, body: str = "") -> None:
        self.status_code = status
        self.ok = status < 400
        self._body = body

    def json(self) -> Any:
        return json.loads(self._body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _search_body(*items: dict[str, Any]) -> str:
    return json.dumps({"items": list(items)})


def _playlist_item(
    *, playlist_id: str = "PL1", title: str = "토익 RC 문법", channel: str = "해커스토익"
) -> dict[str, Any]:
    return {
        "id": {"playlistId": playlist_id},
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/x/mqdefault.jpg"}},
        },
    }


async def test_no_key_fails_without_a_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*a: Any, **k: Any) -> Any:
        raise AssertionError("key 가 없으면 네트워크를 타면 안 된다")

    monkeypatch.setattr(client.requests, "get", _explode)
    result = await client.search_playlists("토익", key="", limit=3)
    assert not result.ok
    assert result.reason == client.REASON_NO_KEY


async def test_parses_playlist_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """실측 필드명(2026-09-03 라이브 호출) 그대로."""
    body = _search_body(_playlist_item())
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=body))

    result = await client.search_playlists("토익 RC 문법 강의", key="AIzatest", limit=3)

    assert result.ok
    p = result.playlists[0]
    assert p.playlist_id == "PL1"
    assert p.title == "토익 RC 문법"
    assert p.channel_title == "해커스토익"
    assert p.playlist_url == "https://www.youtube.com/playlist?list=PL1"


async def test_quota_exceeded_is_a_distinct_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """오늘 다 쓴 것과 그냥 실패한 것은 사용자 안내가 달라야 한다."""
    body = json.dumps(
        {
            "error": {
                "code": 403,
                "errors": [{"reason": "quotaExceeded", "message": "quota"}],
            }
        }
    )
    monkeypatch.setattr(
        client.requests, "get", lambda *a, **k: _FakeResponse(status=403, body=body)
    )

    result = await client.search_playlists("토익", key="AIzatest", limit=3)

    assert not result.ok
    assert result.reason == client.REASON_QUOTA


async def test_other_403_is_unavailable_not_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"error": {"code": 403, "errors": [{"reason": "accessNotConfigured"}]}})
    monkeypatch.setattr(
        client.requests, "get", lambda *a, **k: _FakeResponse(status=403, body=body)
    )

    result = await client.search_playlists("토익", key="AIzatest", limit=3)

    assert not result.ok
    assert result.reason == client.REASON_UNAVAILABLE


async def test_empty_results_are_reported_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: _FakeResponse(body=_search_body()))
    result = await client.search_playlists("존재하지않는강의어쩌구", key="AIzatest", limit=3)
    assert not result.ok
    assert result.reason == client.REASON_EMPTY


async def test_timeout_is_reported_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*a: Any, **k: Any) -> Any:
        raise requests.Timeout()

    monkeypatch.setattr(client.requests, "get", _timeout)
    result = await client.search_playlists("토익", key="AIzatest", limit=3)
    assert not result.ok
    assert result.reason == client.REASON_TIMEOUT


# ───────────────── parse_iso8601_duration ─────────────────


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("PT12M34S", 754),
        ("PT1H23M45S", 5025),
        ("PT45S", 45),
        ("P1DT2H", 93600),
        ("garbage", 0),
    ],
)
def test_parse_iso8601_duration(value: str, seconds: int) -> None:
    assert client.parse_iso8601_duration(value) == seconds


# ───────────────── get_playlist_detail (ADR-0010 §1 ③) ─────────────────


def _video_item(video_id: str, title: str) -> dict[str, Any]:
    return {"contentDetails": {"videoId": video_id}, "snippet": {"title": title}}


class _RoutedResponses:
    """URL 별로 다른 응답을 돌려주는 fake — `get_playlist_detail` 이 3개 endpoint
    (playlists/playlistItems/videos) 를 순서대로 부르므로 단일 큐로는 흉내 낼 수 없다."""

    def __init__(self) -> None:
        self.playlists_body: dict[str, Any] = {
            "items": [
                {"snippet": {"title": "구자연의 자연스러운 문법", "channelTitle": "해커스토익"}}
            ]
        }
        # pageToken 이 없으면 0번 페이지, 있으면 그 문자열을 int 로 삼아 인덱싱한다.
        self.item_pages: list[dict[str, Any]] = [
            {"items": [_video_item("v1", "1일차")], "pageInfo": {"totalResults": 1}}
        ]
        self.videos_body: dict[str, Any] = {
            "items": [{"id": "v1", "contentDetails": {"duration": "PT54M"}}]
        }
        self.calls: list[str] = []

    def get(self, url: str, *, params: dict[str, Any], timeout: Any = None) -> _FakeResponse:
        self.calls.append(url)
        if url == client._PLAYLISTS_URL:
            return _FakeResponse(body=json.dumps(self.playlists_body))
        if url == client._PLAYLIST_ITEMS_URL:
            page = int(params.get("pageToken") or 0)
            return _FakeResponse(body=json.dumps(self.item_pages[page]))
        if url == client._VIDEOS_URL:
            return _FakeResponse(body=json.dumps(self.videos_body))
        raise AssertionError(f"unexpected url {url}")


async def test_detail_no_key_fails_without_a_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*a: Any, **k: Any) -> Any:
        raise AssertionError("key 가 없으면 네트워크를 타면 안 된다")

    monkeypatch.setattr(client.requests, "get", _explode)
    result = await client.get_playlist_detail("PL1", key="")
    assert not result.ok
    assert result.reason == client.REASON_NO_KEY


async def test_detail_returns_playlist_title_not_a_video_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회귀 방지 — `playlistItems.snippet.title` 은 영상 제목이지 재생목록 제목이 아니다.
    먼저 이걸 혼동해서 첫 영상 제목이 재생목록 제목으로 새는 버그를 만들었었다."""
    router = _RoutedResponses()
    monkeypatch.setattr(client.requests, "get", router.get)

    result = await client.get_playlist_detail("PL1", key="AIzatest")

    assert result.ok
    assert result.detail is not None
    assert result.detail.title == "구자연의 자연스러운 문법"
    assert result.detail.channel_title == "해커스토익"
    assert result.detail.curriculum[0].title == "1일차"  # 영상 제목은 커리큘럼 쪽에만


async def test_detail_computes_duration_and_video_count(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _RoutedResponses()
    monkeypatch.setattr(client.requests, "get", router.get)

    result = await client.get_playlist_detail("PL1", key="AIzatest")

    assert result.ok
    assert result.detail is not None
    assert result.detail.video_count == 1  # pageInfo.totalResults
    assert result.detail.total_seconds == 54 * 60
    assert result.detail.truncated is False


async def test_detail_paginates_playlist_items(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _RoutedResponses()
    router.item_pages = [
        {
            "items": [_video_item("v1", "1일차")],
            "pageInfo": {"totalResults": 2},
            "nextPageToken": "1",
        },
        {"items": [_video_item("v2", "2일차")], "pageInfo": {"totalResults": 2}},
    ]
    router.videos_body = {
        "items": [
            {"id": "v1", "contentDetails": {"duration": "PT10M"}},
            {"id": "v2", "contentDetails": {"duration": "PT20M"}},
        ]
    }
    monkeypatch.setattr(client.requests, "get", router.get)

    result = await client.get_playlist_detail("PL1", key="AIzatest")

    assert result.ok
    assert result.detail is not None
    assert [c.title for c in result.detail.curriculum] == ["1일차", "2일차"]
    assert result.detail.total_seconds == 30 * 60


async def test_detail_reports_truncation_instead_of_silently_cutting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client, "_MAX_CURRICULUM_ITEMS", 1)
    router = _RoutedResponses()
    router.item_pages = [
        {
            "items": [_video_item("v1", "1일차")],
            "pageInfo": {"totalResults": 2},
            "nextPageToken": "1",
        },
        {"items": [_video_item("v2", "2일차")], "pageInfo": {"totalResults": 2}},
    ]
    router.videos_body = {"items": [{"id": "v1", "contentDetails": {"duration": "PT10M"}}]}
    monkeypatch.setattr(client.requests, "get", router.get)

    result = await client.get_playlist_detail("PL1", key="AIzatest")

    assert result.ok
    assert result.detail is not None
    assert result.detail.truncated is True
    assert len(result.detail.curriculum) == 1
    assert result.detail.video_count == 2  # 상한에 잘려도 실제 총 개수는 정확하다


async def test_detail_empty_playlist_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _RoutedResponses()
    router.item_pages = [{"items": [], "pageInfo": {"totalResults": 0}}]
    monkeypatch.setattr(client.requests, "get", router.get)

    result = await client.get_playlist_detail("PLempty", key="AIzatest")

    assert not result.ok
    assert result.reason == client.REASON_NOT_FOUND


async def test_detail_quota_exceeded_on_playlist_items(monkeypatch: pytest.MonkeyPatch) -> None:
    def _get(url: str, *, params: dict[str, Any], timeout: Any = None) -> _FakeResponse:
        if url == client._PLAYLISTS_URL:
            return _FakeResponse(body=json.dumps({"items": []}))
        body = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}})
        return _FakeResponse(status=403, body=body)

    monkeypatch.setattr(client.requests, "get", _get)

    result = await client.get_playlist_detail("PL1", key="AIzatest")

    assert not result.ok
    assert result.reason == client.REASON_QUOTA


async def test_detail_missing_playlist_meta_does_not_fail_the_whole_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """제목 하나 못 가져온다고 커리큘럼 전체를 잃으면 안 된다 — 그게 이 응답의 핵심이다."""
    router = _RoutedResponses()
    router.playlists_body = {"items": []}  # 비공개 재생목록 등으로 메타만 실패
    monkeypatch.setattr(client.requests, "get", router.get)

    result = await client.get_playlist_detail("PL1", key="AIzatest")

    assert result.ok
    assert result.detail is not None
    assert result.detail.title == ""
    assert len(result.detail.curriculum) == 1
