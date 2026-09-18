"""계획 API 가 화면에 그대로 뜨는 문구로 거절한다.

- planA-16: 인터뷰를 안 마친 사용자에게 요청 필드 이름('outcome/interviewSessionId')을 말하지 않는다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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
