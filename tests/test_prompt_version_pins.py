"""프로덕션이 부르는 프롬프트에 **버전이 여럿이면 호출부가 그중 하나를 못 박아야 한다.**

`prompts/registry.py` 는 `prompt_id` 에 버전이 없으면 **최신 활성 버전**으로 해석한다.
그래서 버전 없이 부르는 호출은 **누군가 새 프롬프트 파일을 추가하는 것만으로** 배포 없이
갈아탄다.

⚠️ **가상의 위험이 아니다 — 2026-09-07 에 실제로 일어났다.**

`#466` 이 `goal_decompose.v3.md` 를 추가하자 `first_plan.decompose_goal` 이 그 순간
v2 → v3 로 옮겨갔다. 승격 자체는 의도된 것이었지만(커밋 메시지가 인정한다) 두 가지가 따라왔다:

1. **승격이 리뷰 대상이 아니었다.** 파일 추가의 부수효과로 일어났다.
2. **오프라인 하네스도 같이 옮겨갔다.** `scripts/l1_6_run.py`·`l1_7_run.py` 와 M33 하네스가
   전부 버전 없이 부르므로, L1-6(자료 적중 0.83)·L1-7A(M26-core 0.794)·M33 의 기준선을
   **오늘 재실행하면 다른 프롬프트를 잰다.** 저장된 원자료에도 버전이 안 남아 있어
   "이 수치는 v2 로 잰 것"이라고 판별할 방법이 없었다.

하루 전 `#465` 가 `plan_quality` 에 대해 정확히 이 시나리오를 경고하고 고쳤는데, 다음 날
다른 층에서 그대로 실행됐다. 그래서 한 프롬프트가 아니라 **규칙**을 고정한다.

## 규칙

> **디스크에 버전이 둘 이상 있는 프롬프트는, 호출부가 `@vN` 으로 못 박아야 한다.**

버전이 하나뿐인 프롬프트는 해석할 모호함이 없으므로 지금은 면제한다 — 그러나 누군가
두 번째 버전을 만드는 **바로 그 순간** 이 테스트가 빨개져 결정을 요구한다. 별도 허용목록을
두지 않는 이유가 이것이다: 허용목록은 손으로 관리해야 하고, 관리를 안 하면 조용히 썩는다.

## 이 테스트가 빨개졌다면

1. **새 버전을 만들었다** → 승격할 것인지 정하고, 호출부의 `@vN` 을 **의도적으로** 올린다.
   그게 리뷰받아야 할 결정이다.
2. **새 호출부를 추가했다** → 처음부터 버전을 박는다.
3. **핀을 지웠다** → 되돌린다.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

from reaction_backend.prompts import registry

_SRC = Path(__file__).resolve().parents[1] / "src"

# `aiClient.run(...)` / `aiClient.run_grounded(...)` 만 본다. 모듈 docstring 의 사용 예시
# (`llm/__init__.py`)까지 정규식으로 긁으면 **문서를 코드로 착각**한다 — 그래서 AST 로 읽는다.
_RUN_ATTRS = {"run", "run_grounded"}


def _call_sites() -> list[tuple[str, str, int]]:
    """`(prompt_id, 파일, 줄)` — 실제 LLM 호출부만."""
    found: list[tuple[str, str, int]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in _RUN_ATTRS):
                continue
            for kw in node.keywords:
                if kw.arg == "prompt_id" and isinstance(kw.value, ast.Constant):
                    value = kw.value.value
                    if isinstance(value, str):
                        found.append((value, str(path.relative_to(_SRC)), node.lineno))
    return found


def _versions_on_disk() -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for tmpl in registry.list_all():
        out[f"{tmpl.domain}/{tmpl.name}"].append(tmpl.version)
    return out


def test_harness_actually_finds_the_call_sites() -> None:
    """⚠️ **이 테스트가 없으면 아래 두 개가 공허하게 초록일 수 있다.**

    AST 탐색이 조용히 0건을 돌려주면 "위반 없음"이 되어 통과한다. 그래서 알려진 호출부가
    실제로 잡히는지를 먼저 못 박는다.
    """
    sites = _call_sites()
    ids = {pid for pid, _, _ in sites}

    assert len(sites) >= 10, f"호출부를 {len(sites)}건밖에 못 찾았다 — 탐색이 깨졌다"
    for expected in ("planning/goal_decompose@v3", "planning/plan_quality@v3"):
        assert expected in ids, f"{expected} 를 못 찾았다"

    # ⚠️ `recovery/if_then_proposal` 은 여기 못 넣는다 — 호출부가 `_PROMPT_ID_V2`/`_PROMPT_ID_V3`
    # **상수**를 넘겨서 리터럴 탐색에 안 잡힌다(둘 다 버전을 포함하므로 위험은 없다).
    # 즉 이 탐색은 **리터럴 호출만** 덮는다. 상수 경유 호출이 늘면 이 규칙이 조용히 새므로,
    # 그때는 상수까지 따라가도록 넓혀야 한다.


def test_docstring_examples_are_not_mistaken_for_call_sites() -> None:
    """`llm/__init__.py` 의 사용 예시는 호출부가 아니다.

    그 파일은 `prompt_id="recovery/if_then_proposal"` 을 **문서로** 적고 있고, 실제 호출은
    `routes/recovery.py` 가 `_PROMPT_ID_V2`/`_PROMPT_ID_V3` 상수로 한다(둘 다 버전 포함).
    정규식으로 긁던 초안은 이 docstring 을 위반으로 신고했다.
    """
    offenders = [
        (pid, f, ln)
        for pid, f, ln in _call_sites()
        if f.replace("\\", "/").endswith("llm/__init__.py")
    ]
    assert not offenders, f"docstring 예시를 호출부로 셌다: {offenders}"


def test_multi_version_prompts_are_pinned_at_the_call_site() -> None:
    """디스크에 버전이 여럿인 프롬프트는 호출부가 `@vN` 으로 못 박아야 한다."""
    versions = _versions_on_disk()
    violations = []
    for pid, file, line in _call_sites():
        if "@" in pid:
            continue
        available = versions.get(pid, [])
        if len(available) > 1:
            violations.append(f"{file}:{line}  {pid}  (디스크 버전 {sorted(available)})")

    assert not violations, (
        "버전이 여럿인데 호출부가 버전을 안 박았다 — 새 프롬프트 파일 하나로 프로덕션이 "
        "조용히 갈아탄다:\n  " + "\n  ".join(violations)
    )


def test_every_pin_points_at_a_template_that_exists() -> None:
    """핀이 실재하는 템플릿을 가리켜야 한다 — 오타면 런타임에야 터진다."""
    missing = []
    for pid, file, line in _call_sites():
        if "@" not in pid:
            continue
        try:
            registry.get(pid)
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 위치와 함께 보고한다
            missing.append(f"{file}:{line}  {pid}  ({exc})")

    assert not missing, "핀이 없는 템플릿을 가리킨다:\n  " + "\n  ".join(missing)


@pytest.mark.parametrize(
    ("prompt_id", "why"),
    [
        (
            "planning/goal_decompose@v3",
            "②층 분해 — L1-6·L1-7A·M33 기준선이 이 프롬프트에 달려 있다",
        ),
        (
            "planning/plan_quality@v3",
            "④층 검토기 — `plan_quality_eval.v4` 가 옆에 있어 승격 사고가 나기 쉽다 (#465)",
        ),
    ],
)
def test_known_hazard_prompts_stay_pinned(prompt_id: str, why: str) -> None:
    """⚠️ **실측 사고가 있었던 두 자리는 이름으로 못 박는다.**

    위 규칙 테스트는 "버전이 여럿이면 핀"까지만 요구한다. 그런데 누군가 옛 버전 파일을
    지워 버전이 하나만 남으면 규칙이 면제로 바뀌고, 핀을 지워도 초록이 된다. 이 둘은
    실제로 사고가 났던 자리라 그 경로를 따로 막는다.
    """
    ids = {pid for pid, _, _ in _call_sites()}
    assert prompt_id in ids, f"{prompt_id} 핀이 사라졌다 — {why}"
