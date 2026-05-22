"""공통 pytest fixture."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from reaction_backend.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """매 테스트마다 새 앱 인스턴스.

    `create_app()` 을 새로 호출하므로 Idempotency in-memory 저장소도 테스트마다 초기화된다.
    """
    with TestClient(create_app()) as test_client:
        yield test_client
