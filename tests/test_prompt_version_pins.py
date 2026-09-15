"""프로덕션이 부르는 프롬프트에 **버전이 여럿이면 호출부가 그중 하나를 못 박아야 한다.**

`prompts/registry.py` 는 `prompt_id` 에 버전이 없으면 **최신 활성 버전**으로 해석한다.
그래서 버전 없이 부르는 호출은 **누군가 새 프롬프트 파일을 추가하는 것만으로** 배포 없이
갈아탄다.

⚠️ **가상의 위험이 아니다 — 2026-09-07 에 실제로 일어났다.**

`#466` 이 `goal_decompose.v3.md` 를 추가하자 `first_plan.decompose_goal` 이 그 순간
v2 → v3 로 옮겨갔다. 승격 자체는 의도된 것이었지만(커밋 메시지가 인정한다) 두 가지가 따라왔다:

1. **승격이 리뷰 대상이 아니었다.** 파일 추가의 부수효과로 일어났다.
2. **오프라인 하네스도 같이 옮겨갔다.** `scripts/l1_6_run.py`·`l1_7_run.py` 와 M33 하네스가
   전부 버전 없이(프로덕션 경로를 그대로) 부르므로, L1-6(자료 적중 0.83)·L1-7A(M26-core
   0.794)·M33 의 기준선을 **오늘 재실행하면 다른 프롬프트를 잰다.** 게다가 L1-6·L1-7 원자료는
   프롬프트 버전을 직접 남기지 않아 provenance 가 불충분했다. M33 처럼 clean git SHA
   (`f705319`, dirty false)를 남긴 실행은 그 커밋의 파일로 v2 였음을 사후 복원할 수 있었지만,
   그것도 원자료만 보고 즉시 식별되지는 않았다.

하루 전 `#465` 가 `plan_quality` 에 대해 정확히 이 시나리오를 경고하고 고쳤는데, 다음 날
다른 층에서 그대로 실행됐다. 그래서 한 프롬프트가 아니라 **규칙**을 고정한다.

## 규칙

> **디스크에 버전이 둘 이상 있는 프롬프트는, 호출부가 `@vN` 으로 못 박아야 한다.**

버전이 하나뿐인 프롬프트는 해석할 모호함이 없으므로 지금은 면제한다 — 그러나 누군가
두 번째 버전을 만드는 **바로 그 순간** 이 테스트가 빨개져 결정을 요구한다. 버전 수에 대한
허용목록을 두지 않는 이유가 이것이다: 허용목록은 손으로 관리해야 하고, 관리를 안 하면
조용히 썩는다.

## 호출부를 찾는 방법 — 못 찾으면 통과가 아니라 실패다

`aiClient.run` / `aiClient.run_grounded` 호출을 AST 로 찾고, `prompt_id` 를 **keyword 든
위치 인자든** 꺼내 값을 정적으로 확정한다. 리터럴, 모듈 상수, 함수 지역 변수, 그 둘을 고르는
조건식(`A if c else B`)까지 따라간다 — 회복(`routes/recovery.py`)이 실제로
`_PROMPT_ID_V3 if use_v3 else _PROMPT_ID_V2` 를 쓴다.

⚠️ 값을 확정하지 못한 호출은 **조용히 건너뛰지 않는다.** 예전 탐색은 리터럴 keyword 만
봤고, 상수나 위치 인자로 넘기면 규칙 밖으로 새어 나갔다. 이제 그런 호출은 실패하고,
호출부를 리터럴·상수로 바꾸라고 요구한다. 진짜 동적 라우팅만 `_DYNAMIC_ROUTING_EXEMPTIONS`
에 **식 원문 그대로** 올리고, 그 값들에 같은 규칙을 거는 런타임 테스트를 함께 둔다.

## 이 테스트가 빨개졌다면

1. **새 버전을 만들었다** → 승격할 것인지 정하고, 호출부의 `@vN` 을 **의도적으로** 올린다.
   그게 리뷰받아야 할 결정이다.
2. **새 호출부를 추가했다** → 처음부터 버전을 박는다.
3. **핀을 지웠다** → 되돌린다.
4. **`prompt_id` 를 확정할 수 없다** → 리터럴이나 모듈 상수로 넘긴다.
"""

from __future__ import annotations

import ast
import dataclasses
import textwrap
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Final

import pytest

from reaction_backend.orchestrator import interview_catalog
from reaction_backend.prompts import registry

_SRC = Path(__file__).resolve().parents[1] / "src"

#: `LLMToolExecutor` 메서드별 `prompt_id` 의 위치 인자 번호 — `llm/tool_executor.py` 시그니처
#: (`run(module, schema, prompt_id, fallback, ...)` / `run_grounded(module, prompt_id, ...)`).
_PROMPT_ID_POSITION: Final = {"run": 2, "run_grounded": 1}

#: 정적으로 값을 확정할 수 없어도 되는 호출 — **(파일, prompt_id 식 원문)** 이 정확히 같을 때만.
#: 식이 바뀌면 면제가 풀려 다시 빨개진다. 새로 넣을 때는 이유와, 그 값에 규칙을 거는 테스트를
#: 함께 적는다.
_INTERVIEW_CATALOG_REASON: Final = (
    "인터뷰 종류(plan/ultimate)에 따라 `CATALOGS` 가 프롬프트를 고른다 — 값이 "
    "`interview_catalog.py` 의 dataclass 필드라 AST 로 못 따라간다. 대신 "
    "`test_interview_catalog_prompts_follow_the_pin_rule` 이 런타임 값에 같은 규칙을 건다."
)
_DYNAMIC_ROUTING_EXEMPTIONS: Final[Mapping[tuple[str, str], str]] = {
    ("reaction_backend/orchestrator/interview.py", "catalog.prompt_next_question"): (
        _INTERVIEW_CATALOG_REASON
    ),
    (
        "reaction_backend/orchestrator/interview.py",
        "intake_prompt if merged and intake_prompt else catalog.prompt_ambiguity",
    ): _INTERVIEW_CATALOG_REASON,
    ("reaction_backend/orchestrator/interview.py", "CATALOGS[state['kind']].prompt_summary"): (
        _INTERVIEW_CATALOG_REASON
    ),
}


# ── 스캐너 ───────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class CallSite:
    file: str
    line: int
    #: `prompt_id` 로 넘긴 식의 원문(`ast.unparse`). 인자를 못 찾았으면 `<...>` 설명.
    expr: str
    #: 이 호출이 넘길 수 있는 prompt id 전부. **None = 정적으로 확정하지 못했다.**
    prompt_ids: frozenset[str] | None


_Scope = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef
_NESTED_SCOPES: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_scope(scope: _Scope) -> Iterator[ast.AST]:
    """스코프 본문 노드 — 중첩 함수·클래스·람다 안으로는 내려가지 않는다."""
    stack: list[ast.AST] = list(scope.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _NESTED_SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _binding_values(scope: _Scope, name: str) -> list[ast.expr] | None:
    """`scope` 에서 `name` 에 대입되는 값 식들.

    `[]` = 이 스코프에서 바인딩되지 않는다(바깥을 본다). `None` = 바인딩되지만 단순 대입이
    아니라(인자·import·for·augmented 대입·언패킹 등) 값을 확정할 수 없다.
    """
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        a = scope.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
        if any(p is not None and p.arg == name for p in params):
            return None

    values: list[ast.expr] = []
    covered: set[int] = set()
    stores = 0
    for node in _walk_scope(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    values.append(node.value)
                    covered.add(id(target))
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                values.append(node.value)
                covered.add(id(target))
        elif (
            (
                isinstance(node, ast.Import | ast.ImportFrom)
                and any((a.asname or a.name.split(".")[0]) == name for a in node.names)
            )
            or (isinstance(node, ast.Global | ast.Nonlocal) and name in node.names)
            or (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and node.name == name
            )
        ):
            return None
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store):
            stores += 1
    if stores > len(covered):
        return None  # for 대상·with as·언패킹·augmented 대입 등
    return values


def _resolve(
    expr: ast.expr, func: _Scope | None, module: ast.Module, seen: frozenset[tuple[int, str]]
) -> frozenset[str] | None:
    if isinstance(expr, ast.Constant):
        return frozenset({expr.value}) if isinstance(expr.value, str) else None
    if isinstance(expr, ast.IfExp):
        body = _resolve(expr.body, func, module, seen)
        orelse = _resolve(expr.orelse, func, module, seen)
        return body | orelse if body is not None and orelse is not None else None
    if isinstance(expr, ast.Name):
        scopes: list[_Scope] = [func, module] if func is not None else [module]
        for scope in scopes:
            key = (id(scope), expr.id)
            if key in seen:
                return None  # 순환
            values = _binding_values(scope, expr.id)
            if values is None:
                return None
            if not values:
                continue
            inner = func if scope is func else None
            resolved = [_resolve(v, inner, module, seen | {key}) for v in values]
            if any(r is None for r in resolved):
                return None
            return frozenset().union(*(r for r in resolved if r is not None))
        return None  # 어디서도 바인딩되지 않는다(builtin·star import 등)
    return None  # f-string·속성·첨자·호출 등 — 정적으로 확정하지 않는다


def _executor_names(tree: ast.Module) -> set[str]:
    """이 모듈에서 `aiClient` 를 가리키는 이름 — import 별칭 포함."""
    names = {"aiClient"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "reaction_backend.llm"
        ):
            names.update(a.asname or a.name for a in node.names if a.name == "aiClient")
    return names


def _calls_with_scope(tree: ast.Module) -> Iterator[tuple[ast.Call, _Scope | None]]:
    def visit(node: ast.AST, func: _Scope | None) -> Iterator[tuple[ast.Call, _Scope | None]]:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else func
            if isinstance(child, ast.Call):
                yield child, inner
            yield from visit(child, inner)

    yield from visit(tree, None)


def _prompt_id_arg(call: ast.Call, method: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "prompt_id":
            return kw.value
    position = _PROMPT_ID_POSITION[method]
    leading = call.args[: position + 1]
    if any(isinstance(arg, ast.Starred) for arg in leading) or len(call.args) <= position:
        return None  # `*args`/`**kwargs` 로 넘겼거나 인자가 없다
    return call.args[position]


def _scan_source(source: str, file: str) -> list[CallSite]:
    """모듈 소스 하나에서 `aiClient.run*` 호출부를 뽑는다.

    ⚠️ 문자열로 긁지 않고 AST 로 읽는다 — docstring 의 사용 예시(`llm/__init__.py`)를
    호출부로 착각하지 않게.
    """
    tree = ast.parse(source, filename=file)
    executors = _executor_names(tree)
    sites: list[CallSite] = []
    for call, func in _calls_with_scope(tree):
        target = call.func
        if not (isinstance(target, ast.Attribute) and target.attr in _PROMPT_ID_POSITION):
            continue
        receiver = target.value
        is_executor = (isinstance(receiver, ast.Name) and receiver.id in executors) or (
            isinstance(receiver, ast.Attribute) and receiver.attr == "aiClient"
        )
        if not is_executor:
            # 아무 객체의 `.run()` 은 LLM 호출이 아니다(`study_method_agent.run(goal=...)`).
            # 그러나 `prompt_id=` 를 넘기면서 `aiClient` 로 못 알아본 수신자라면 별칭을
            # 놓친 것일 수 있다 — 건너뛰지 않고 확정 불가로 올린다.
            if not any(kw.arg == "prompt_id" for kw in call.keywords):
                continue
            expr = next(kw.value for kw in call.keywords if kw.arg == "prompt_id")
            sites.append(
                CallSite(
                    file,
                    call.lineno,
                    f"<aiClient 로 못 알아본 수신자 {ast.unparse(receiver)}> {ast.unparse(expr)}",
                    None,
                )
            )
            continue
        arg = _prompt_id_arg(call, target.attr)
        if arg is None:
            sites.append(CallSite(file, call.lineno, "<prompt_id 인자를 찾지 못했다>", None))
            continue
        sites.append(
            CallSite(file, call.lineno, ast.unparse(arg), _resolve(arg, func, tree, frozenset()))
        )
    return sites


def _call_sites() -> list[CallSite]:
    """`src/` 전체의 실제 LLM 호출부."""
    sites: list[CallSite] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        sites.extend(_scan_source(path.read_text(encoding="utf-8"), rel))
    return sites


def _versions_on_disk() -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for tmpl in registry.list_all():
        out[f"{tmpl.domain}/{tmpl.name}"].append(str(tmpl.version))
    return out


def _unresolved(
    sites: Iterable[CallSite], exemptions: Mapping[tuple[str, str], str]
) -> list[CallSite]:
    return [s for s in sites if s.prompt_ids is None and (s.file, s.expr) not in exemptions]


def _unpinned(prompt_ids: Iterable[str], versions: Mapping[str, list[str]]) -> list[str]:
    return sorted(pid for pid in prompt_ids if "@" not in pid and len(versions.get(pid, [])) > 1)


def _resolved_ids(sites: Iterable[CallSite]) -> set[str]:
    return {pid for s in sites if s.prompt_ids is not None for pid in s.prompt_ids}


# ── 스캐너 자체 ──────────────────────────────────────────────────────────────

_HEADER: Final = "from reaction_backend.llm import aiClient\n"


def _scan(body: str) -> list[CallSite]:
    return _scan_source(_HEADER + textwrap.dedent(body), "pkg/mod.py")


def _only(sites: list[CallSite]) -> CallSite:
    assert len(sites) == 1, sites
    return sites[0]


def test_scanner_reads_a_keyword_literal() -> None:
    site = _only(
        _scan(
            """
            async def f():
                await aiClient.run(module="planning", schema=S, prompt_id="planning/foo", fallback=fb)
            """
        )
    )
    assert site.prompt_ids == {"planning/foo"}


def test_scanner_follows_a_module_constant() -> None:
    site = _only(
        _scan(
            """
            _PROMPT_ID = "planning/foo@v1"

            async def f():
                await aiClient.run(module="planning", schema=S, prompt_id=_PROMPT_ID, fallback=fb)
            """
        )
    )
    assert site.prompt_ids == {"planning/foo@v1"}


@pytest.mark.parametrize(
    "call",
    [
        'aiClient.run("planning", S, "planning/foo@v1", fb)',
        'aiClient.run_grounded("planning", "planning/foo@v1", variables={})',
    ],
)
def test_scanner_reads_a_positional_literal(call: str) -> None:
    site = _only(_scan(f"async def f():\n    await {call}\n"))
    assert site.prompt_ids == {"planning/foo@v1"}


@pytest.mark.parametrize(
    "call",
    [
        'aiClient.run("planning", S, _PROMPT_ID, fb, timeout=8.0)',
        'aiClient.run_grounded("planning", _PROMPT_ID)',
    ],
)
def test_scanner_follows_a_positional_constant(call: str) -> None:
    site = _only(_scan(f'_PROMPT_ID = "planning/foo@v1"\n\nasync def f():\n    await {call}\n'))
    assert site.prompt_ids == {"planning/foo@v1"}


def test_scanner_follows_a_local_choice_between_constants() -> None:
    """`routes/recovery.py` 의 모양 — 지역 변수가 두 상수 중 하나를 고른다."""
    site = _only(
        _scan(
            """
            _V2 = "recovery/p@v2"
            _V3 = "recovery/p@v3"

            async def f(use_v3):
                prompt_id = _V3 if use_v3 else _V2
                await aiClient.run(module="recovery", schema=S, prompt_id=prompt_id, fallback=fb)
            """
        )
    )
    assert site.prompt_ids == {"recovery/p@v2", "recovery/p@v3"}


def test_scanner_follows_an_import_alias_of_the_executor() -> None:
    sites = _scan_source(
        'from reaction_backend.llm import aiClient as ai\n\nai.run("planning", S, "planning/foo", fb)\n',
        "pkg/mod.py",
    )
    assert _only(sites).prompt_ids == {"planning/foo"}


def test_scanner_ignores_other_run_methods() -> None:
    """에이전트의 `.run(goal=...)` 은 LLM 호출이 아니다 — 호출부로 세지 않는다."""
    assert _scan("async def f():\n    await study_method_agent.run(goal=g, session=s)\n") == []


def test_unpinned_multi_version_constant_is_a_violation_and_a_pinned_one_is_not() -> None:
    versions = {"planning/foo": ["1", "2"]}
    unpinned = _only(_scan('_P = "planning/foo"\naiClient.run("planning", S, _P, fb)\n'))
    pinned = _only(_scan('_P = "planning/foo@v2"\naiClient.run("planning", S, _P, fb)\n'))

    assert unpinned.prompt_ids is not None and pinned.prompt_ids is not None
    assert _unpinned(unpinned.prompt_ids, versions) == ["planning/foo"]
    assert _unpinned(pinned.prompt_ids, versions) == []


def test_docstring_examples_are_not_counted_as_calls() -> None:
    source = '''
        """모듈 설명.

            result = await aiClient.run(module="recovery", prompt_id="recovery/if_then_proposal")
        """

        def f():
            """await aiClient.run_grounded("planning", "planning/materials_search")"""
    '''
    assert _scan(source) == []


@pytest.mark.parametrize(
    ("body", "expr"),
    [
        (
            'async def f(x):\n    await aiClient.run("m", S, f"planning/{x}", fb)\n',
            "f'planning/{x}'",
        ),
        ("async def f(pid):\n    await aiClient.run(prompt_id=pid)\n", "pid"),
        ("async def f(cfg):\n    await aiClient.run(prompt_id=cfg.prompt)\n", "cfg.prompt"),
        ("async def f(k):\n    await aiClient.run(prompt_id=IDS[k])\n", "IDS[k]"),
        ("async def f():\n    await aiClient.run(prompt_id=pick())\n", "pick()"),
        ("from x import PID\n\nasync def f():\n    await aiClient.run(prompt_id=PID)\n", "PID"),
        ('_P = "planning/foo"\n_P += "@v1"\naiClient.run(prompt_id=_P)\n', "_P"),
        ("async def f():\n    await aiClient.run(prompt_id=UNDEFINED)\n", "UNDEFINED"),
        (
            "async def f(kwargs):\n    await aiClient.run(**kwargs)\n",
            "<prompt_id 인자를 찾지 못했다>",
        ),
        (
            'async def f(c):\n    await c.run(prompt_id="planning/foo")\n',
            "<aiClient 로 못 알아본 수신자 c> 'planning/foo'",
        ),
    ],
)
def test_unresolvable_prompt_id_is_reported_not_dropped(body: str, expr: str) -> None:
    """⚠️ 확정하지 못한 호출이 목록에서 사라지면 규칙이 조용히 샌다 — 반드시 위반으로 올라온다."""
    site = _only(_scan(body))
    assert site.prompt_ids is None
    assert site.expr == expr
    assert _unresolved([site], {}) == [site]


def test_exemption_matches_the_exact_expression_only() -> None:
    site = _only(_scan("async def f(cfg):\n    await aiClient.run(prompt_id=cfg.prompt)\n"))
    assert _unresolved([site], {("pkg/mod.py", "cfg.prompt"): "이유"}) == []
    assert _unresolved([site], {("pkg/mod.py", "cfg.other"): "이유"}) == [site]


# ── 프로덕션 ─────────────────────────────────────────────────────────────────


def test_harness_actually_finds_the_call_sites() -> None:
    """⚠️ **이 테스트가 없으면 아래 테스트들이 공허하게 초록일 수 있다.**

    AST 탐색이 조용히 0건을 돌려주면 "위반 없음"이 되어 통과한다. 그래서 알려진 호출부가
    실제로 잡히는지를 먼저 못 박는다 — 리터럴 keyword 뿐 아니라 상수 경유(회복)와 위치
    인자(자료 검색)도.
    """
    sites = _call_sites()
    ids = _resolved_ids(sites)

    assert len(sites) >= 15, f"호출부를 {len(sites)}건밖에 못 찾았다 — 탐색이 깨졌다"
    for expected in (
        "planning/goal_decompose@v3",
        "planning/plan_quality@v3",
        "planning/study_method@v2",
        # `routes/recovery.py` — `_PROMPT_ID_V3 if use_v3 else _PROMPT_ID_V2` 상수 경유.
        "recovery/if_then_proposal@v2",
        "recovery/if_then_proposal@v3",
        # `routes/materials.py` — `run_grounded("planning", "planning/materials_search")` 위치 인자.
        "planning/materials_search",
    ):
        assert expected in ids, f"{expected} 를 못 찾았다"


def test_docstring_examples_are_not_mistaken_for_call_sites() -> None:
    """`llm/__init__.py` 의 사용 예시는 호출부가 아니다.

    그 파일은 `prompt_id="recovery/if_then_proposal"` 을 **문서로** 적고 있고, 실제 호출은
    `routes/recovery.py` 가 `_PROMPT_ID_V2`/`_PROMPT_ID_V3` 상수로 한다(둘 다 버전 포함).
    정규식으로 긁던 초안은 이 docstring 을 위반으로 신고했다.
    """
    offenders = [s for s in _call_sites() if s.file.endswith("llm/__init__.py")]
    assert not offenders, f"docstring 예시를 호출부로 셌다: {offenders}"


def test_every_prompt_id_is_statically_known() -> None:
    """값을 확정할 수 없는 `prompt_id` 는 핀 규칙을 우회한다 — 면제된 동적 라우팅 외엔 실패."""
    unresolved = _unresolved(_call_sites(), _DYNAMIC_ROUTING_EXEMPTIONS)
    assert not unresolved, (
        "prompt_id 를 정적으로 확정할 수 없다 — 리터럴이나 모듈 상수로 넘겨라. 진짜 동적 "
        "라우팅이면 `_DYNAMIC_ROUTING_EXEMPTIONS` 에 이유와 규칙 테스트를 함께 올려라:\n  "
        + "\n  ".join(f"{s.file}:{s.line}  prompt_id={s.expr}" for s in unresolved)
    )


def test_dynamic_routing_exemptions_still_match_a_call_site() -> None:
    """면제가 낡지 않게 — 호출부가 바뀌었는데 면제만 남으면 다음 동적 호출을 덮을 수 있다."""
    live = {(s.file, s.expr) for s in _call_sites() if s.prompt_ids is None}
    stale = sorted(set(_DYNAMIC_ROUTING_EXEMPTIONS) - live)
    assert not stale, f"맞는 호출부가 없는 면제: {stale}"


def test_interview_catalog_prompts_follow_the_pin_rule() -> None:
    """면제된 인터뷰 호출부가 실제로 넘기는 값 — 카탈로그의 `prompt_*` 필드 — 에 같은 규칙을 건다."""
    catalogs = {id(c): c for c in interview_catalog.CATALOGS.values()}
    catalogs.update(
        (id(v), v)
        for v in vars(interview_catalog).values()
        if isinstance(v, interview_catalog.InterviewCatalog)
    )
    values = [
        (catalog.kind, field.name, getattr(catalog, field.name))
        for catalog in catalogs.values()
        for field in dataclasses.fields(catalog)
        if field.name.startswith("prompt_")
    ]
    prompt_ids = [pid for _, _, pid in values if pid is not None]
    assert len(catalogs) >= 2 and len(prompt_ids) >= 7, f"카탈로그 탐색이 깨졌다: {values}"

    versions = _versions_on_disk()
    violations = [
        f"{kind}.{name}={pid}" for kind, name, pid in values if pid and _unpinned([pid], versions)
    ]
    assert not violations, f"카탈로그 프롬프트에 버전이 여럿인데 핀이 없다: {violations}"
    for pid in prompt_ids:
        registry.get(pid)  # 없는 프롬프트면 PromptNotFound


def test_multi_version_prompts_are_pinned_at_the_call_site() -> None:
    """디스크에 버전이 여럿인 프롬프트는 호출부가 `@vN` 으로 못 박아야 한다."""
    versions = _versions_on_disk()
    violations = [
        f"{s.file}:{s.line}  {pid}  (디스크 버전 {sorted(versions[pid])})"
        for s in _call_sites()
        if s.prompt_ids is not None
        for pid in _unpinned(s.prompt_ids, versions)
    ]
    assert not violations, (
        "버전이 여럿인데 호출부가 버전을 안 박았다 — 새 프롬프트 파일 하나로 프로덕션이 "
        "조용히 갈아탄다:\n  " + "\n  ".join(violations)
    )


def test_every_pin_points_at_a_template_that_exists() -> None:
    """핀이 실재하는 템플릿을 가리켜야 한다 — 오타면 런타임에야 터진다."""
    missing = []
    for s in _call_sites():
        for pid in sorted(s.prompt_ids or ()):
            if "@" not in pid:
                continue
            try:
                registry.get(pid)
            except Exception as exc:  # noqa: BLE001 — 어떤 실패든 위치와 함께 보고한다
                missing.append(f"{s.file}:{s.line}  {pid}  ({exc})")

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
    assert prompt_id in _resolved_ids(_call_sites()), f"{prompt_id} 핀이 사라졌다 — {why}"
