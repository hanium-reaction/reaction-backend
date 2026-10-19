from uuid import uuid4

import pytest
from pydantic import ValidationError

from reaction_backend.schemas.work_context import QuickAction, ResourceInput


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/a",
        "https://user:secret@example.org",
        "https://",
        "https://example.org/\nsecret",
        "data:text/plain,test",
    ],
)
def test_resource_rejects_unsafe_addresses(url):
    with pytest.raises(ValidationError):
        ResourceInput(url=url, label="자료")


def test_quick_action_requires_confirmed_time_and_nonblank_text():
    with pytest.raises(ValidationError):
        QuickAction(request_id=uuid4(), title="할 일", first_step="한 행동")
    with pytest.raises(ValidationError):
        QuickAction(request_id=uuid4(), title="  ", first_step="한 행동", attempt_minutes=10)
    assert (
        QuickAction(
            request_id=uuid4(), title="할 일", first_step="한 행동", attempt_minutes=10
        ).goal_id
        is None
    )
