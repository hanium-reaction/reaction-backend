"""Issue #3 mock/stub 데이터.

도메인 스텁(#3-B~#3-H)이 공통으로 참조하는 데모 fixture 를 모은다.
도메인별 fixture 는 이 패키지에 `<domain>.py` 로 추가한다 (예: `mock/goals.py`).
도메인 실구현이 끝나면 이 패키지는 제거된다.
"""

from reaction_backend.api.mock.demo import DEMO_USER, DemoUser

__all__ = ["DEMO_USER", "DemoUser"]
