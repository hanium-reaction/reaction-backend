"""프롬프트 파일과 호출부가 어긋나지 않게 한다 (#430).

레포에 **한 번도 배선된 적 없는** 프롬프트가 둘 있었다 — `failure_diagnosis/classify` ·
`habit_penalty/evaluate`. 최초 LLM 인프라 커밋(#5)에서 만들어지고 그대로 남았다.

고아 프롬프트가 나쁜 이유는 셋이다:

1. 레지스트리는 **디렉터리를 스캔**한다. 안 쓰는 파일이 남아 있으면 나중에 같은 이름의
   새 버전을 올릴 때 `latest()` 해석이 헷갈린다.
2. 읽는 사람이 **살아 있는 계약으로 오해**한다. `habit_penalty` 는 특히 헷갈렸다 —
   같은 이름의 모듈이 실제로 동작하는데 **룰 기반**이다(DevBaseline §1.4 가 정한
   결정론적 판정이라 LLM 이 필요 없다). 프롬프트만 보면 LLM 이 판정한다고 읽힌다.
3. 반대 방향도 있다 — 코드가 **없는 프롬프트**를 부르면 런타임까지 안 잡힌다.
"""

from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_SRC = _ROOT / "src" / "reaction_backend"
_PROMPTS = _SRC / "prompts"

# 배선하지 않는 것이 **설계인** 프롬프트. 새로 넣을 때는 이유를 여기 적는다.
_INTENTIONALLY_UNWIRED = {
    # 평가 하네스 전용(`scripts/l1_7b_v4_run.py`). 프로덕션 ④층은 `plan_quality` 를 부른다.
    # ⚠️ 이름이 `plan_quality` 가 아닌 것이 **프로덕션 자동 승격을 막는 유일한 장치**다 —
    # 레지스트리가 버전 없이 부르면 같은 이름 중 가장 높은 번호를 고르기 때문이다.
    "planning/plan_quality_eval",
}


def _prompt_ids() -> set[str]:
    out = set()
    for f in _PROMPTS.rglob("*.md"):
        if f.name == "README.md":
            continue
        stem = f.stem.rsplit(".v", 1)[0]  # goal_decompose.v2 → goal_decompose
        out.add(f"{f.parent.name}/{stem}")
    return out


def _referenced_ids() -> set[str]:
    """`src/` 안의 문자열 리터럴 중 프롬프트 id 모양인 것.

    호출부가 `prompt_id=` 로 직접 쓰기도 하고 카탈로그 상수(`prompt_ambiguity=...`)로
    두기도 해서, **키워드 이름이 아니라 리터럴 모양**으로 훑는다.

    ⚠️ **버전을 못 박은 참조도 센다** — 회복은 `"recovery/if_then_proposal@v2"` 처럼
    `@vN` 을 붙여 A/B 를 가른다. 접미사를 떼고 대조하지 않으면 고아로 잘못 잡힌다.
    """
    ids = _prompt_ids()
    seen: set[str] = set()
    for f in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            bare = node.value.split("@", 1)[0]
            if bare in ids:
                seen.add(bare)
    return seen


def test_every_prompt_file_is_wired() -> None:
    """파일은 있는데 부르는 곳이 없는 프롬프트가 없다."""
    orphans = _prompt_ids() - _referenced_ids() - _INTENTIONALLY_UNWIRED
    assert not orphans, (
        f"호출처 없는 프롬프트: {sorted(orphans)} — 배선하거나 지워라. "
        "의도적으로 안 배선하는 것이면 `_INTENTIONALLY_UNWIRED` 에 **이유와 함께** 넣어라."
    )


def test_intentionally_unwired_prompts_still_exist() -> None:
    """예외 목록이 낡지 않게 — 지워진 프롬프트가 목록에 남아 있으면 예외가 무의미해진다."""
    missing = _INTENTIONALLY_UNWIRED - _prompt_ids()
    assert not missing, f"예외 목록에만 있고 파일은 없다: {sorted(missing)}"


def test_the_two_orphans_are_gone() -> None:
    """#430 이 지운 둘이 되살아나지 않게.

    둘 다 최초 LLM 인프라 커밋(#5)에서 만들어지고 **한 번도 배선된 적 없다.**
    `habit_penalty` 는 특히 되살리면 안 된다 — 그 판정은 DevBaseline §1.4 가 정한
    **결정론적 규칙**(3주 연속 `done < target * 0.5`)이라 LLM 이 할 일이 아니다.
    """
    for gone in ("failure_diagnosis/classify", "habit_penalty/evaluate"):
        assert gone not in _prompt_ids(), (
            f"`{gone}` 이 돌아왔다 — 배선할 계획이 있으면 호출부와 함께 넣어라."
        )
