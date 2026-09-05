"""④층 검토기 프롬프트의 **버전이 코드에 박혀 있어야 한다.**

`prompts/registry.py` 는 `prompt_id` 에 버전이 없으면 **최신 활성 버전**으로 해석한다.
그래서 `planning/plan_quality` 라고만 부르면, 누군가 `plan_quality.v4.md` 를 **파일로
만들기만 해도** 프로덕션 ④층이 배포 없이 그 순간 갈아탄다.

이건 가상의 위험이 아니다. 이 레포에는 이미 오프라인 평가용 v4 후보가 있고
(`plan_quality_eval.v4.md`), 그게 프로덕션을 건드리지 않는 유일한 이유는 **파일 이름을
다르게 지었기 때문**이다 — 규율이지 구조가 아니었다. `l1-7b-v4-results.md` 가 핀을
권고했으나 "범위 밖" 으로 미뤄 뒀던 자리다.

⚠️ **이 테스트가 빨개졌다면 둘 중 하나다.**
1. 의도한 승격 — 그러면 `first_plan.py` 의 핀과 이 파일의 기대값을 **같이** 고친다.
   그건 리뷰를 받는 의도된 변경이다.
2. 실수 — 버전을 지웠거나 새 프롬프트 파일이 이름 규칙을 어겼다. 되돌린다.

승격이 왜 아직인지는 `l1-7b-v4-results.md` §미해결 조건 6개 참조.
"""

from __future__ import annotations

import re
from pathlib import Path

from reaction_backend.prompts import registry

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "reaction_backend"
    / "orchestrator"
    / "first_plan.py"
)

EXPECTED_PROMPT_ID = "planning/plan_quality@v3"


def test_production_review_prompt_is_version_pinned() -> None:
    """호출부가 버전 없는 `planning/plan_quality` 를 쓰면 안 된다."""
    source = _SOURCE.read_text(encoding="utf-8")

    bare = re.findall(r'prompt_id="planning/plan_quality"', source)
    assert not bare, (
        "④층 호출이 버전 없이 `planning/plan_quality` 를 쓴다 — `latest()` 해석에 맡기면 "
        "`plan_quality.v4.md` 파일 생성만으로 프로덕션이 갈아탄다. `@v3` 처럼 명시할 것."
    )
    assert f'prompt_id="{EXPECTED_PROMPT_ID}"' in source, (
        f"기대한 핀 `{EXPECTED_PROMPT_ID}` 을 호출부에서 못 찾았다."
    )


def test_pinned_version_actually_exists() -> None:
    """핀이 실재하는 템플릿을 가리켜야 한다 — 오타면 런타임에야 터진다."""
    template = registry.get(EXPECTED_PROMPT_ID)
    assert template is not None


def test_pin_is_not_silently_the_same_as_latest() -> None:
    """핀과 `latest()` 가 갈라지면 **승격 결정을 하라고 알린다.**

    ⚠️ **이 테스트는 지금 아무것도 보증하지 않는다** — latest 도 v3 라 두 값이 같고,
    핀을 지워도 이 함수는 초록이다. 핀이 있는지를 지키는 건 위의
    `test_production_review_prompt_is_version_pinned` 다.

    이 함수의 쓸모는 **미래에만** 있다: 누군가 `plan_quality.v4.md` 를 두면 latest 가
    v4 로 움직이는데 핀 덕분에 프로덕션은 v3 에 남는다. 그 갈라짐을 조용히 두면 "핀이
    낡았다" 는 사실을 아무도 모르므로, 여기서 빨갛게 만들어 **의도된 승격인지 묻는다.**
    실패가 곧 결함은 아니다.
    """
    latest = registry.get("planning/plan_quality")
    pinned = registry.get(EXPECTED_PROMPT_ID)

    if latest.version != pinned.version:
        # 갈라졌다 = 새 버전이 등록됐다. 핀 덕분에 프로덕션은 아직 안 움직였다.
        # 이건 실패가 아니라 **승격 결정을 하라는 신호**다.
        raise AssertionError(
            f"`planning/plan_quality` 의 latest 가 v{latest.version} 인데 프로덕션 핀은 "
            f"v{pinned.version} 이다. 핀이 자동 승격을 막았다 — 의도한 승격이면 "
            f"`first_plan.py` 의 핀과 이 파일의 EXPECTED_PROMPT_ID 를 같이 올리고, "
            f"아니면 새 프롬프트 파일 이름을 `plan_quality_eval.*` 처럼 분리할 것."
        )
