"""Prompt Registry (Issue #5 §2).

파일 기반 단일 진실 소스:
    prompts/<domain>/<name>.v1.md

- 디렉토리 스캔으로 자동 발견 → `(domain, name, version) → PromptTemplate`.
- 최신 활성 버전 매핑은 `latest()` 가 SemVer-ish 정렬로 결정.
- `render(prompt_id, variables)` 가 `{{var}}` Mustache 풍 단순 치환을 수행.
- Tool Executor 는 `registry.get(prompt_id)` 만 호출.

지원 도메인 (DevBaseline §4):
    interview / planning / recovery / brief / inbox / review
    habit_penalty / failure_diagnosis

`prompt_id` 표기는 `"<domain>/<name>"` 또는 `"<domain>/<name>@<version>"`.
버전 생략 시 latest active.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_log = logging.getLogger(__name__)

# DevBaseline §4 — 잠금된 8 도메인.
SUPPORTED_DOMAINS: frozenset[str] = frozenset(
    {
        "interview",
        "planning",
        "recovery",
        "brief",
        "inbox",
        "review",
        "habit_penalty",
        "failure_diagnosis",
    }
)

_FILENAME_RE = re.compile(r"^(?P<name>[a-z0-9_]+)\.v(?P<version>\d+(?:\.\d+)?)\.md$")
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class PromptNotFound(KeyError):
    """`prompt_id` 가 레지스트리에 없음. Tool Executor 가 잡아 fallback 분기."""


class PromptRenderError(ValueError):
    """필수 변수 누락 등 렌더 실패."""


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """단일 프롬프트 파일."""

    domain: str
    name: str
    version: str
    """예: "1", "1.2". DB(`llm_runs.prompt_version`) 와 동일 라벨."""
    body: str
    path: Path

    @property
    def prompt_id(self) -> str:
        """`"<domain>/<name>"` — Tool Executor 가 받는 키."""
        return f"{self.domain}/{self.name}"

    @property
    def full_id(self) -> str:
        """`"<domain>/<name>@v<version>"` — A/B 라벨 포함."""
        return f"{self.prompt_id}@v{self.version}"

    def render(self, variables: dict[str, str]) -> str:
        """`{{var}}` 치환. 누락된 변수는 `PromptRenderError`."""
        missing: list[str] = []

        def _sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in variables:
                missing.append(key)
                return ""
            return variables[key]

        rendered = _VAR_RE.sub(_sub, self.body)
        if missing:
            raise PromptRenderError(f"missing variables for {self.full_id}: {sorted(set(missing))}")
        return rendered


def _prompts_root() -> Path:
    """이 모듈 옆 폴더 트리. 단일 진실 소스."""
    return Path(__file__).resolve().parent


def _version_key(version: str) -> tuple[int, ...]:
    """`"1.2"` → `(1, 2)` — 정렬용."""
    return tuple(int(p) for p in version.split("."))


@dataclass(frozen=True, slots=True)
class _RegistryState:
    by_full_id: dict[str, PromptTemplate]
    """`"<domain>/<name>@v<version>"` → template"""
    latest_by_id: dict[str, PromptTemplate]
    """`"<domain>/<name>"` → latest version template"""


def _scan() -> _RegistryState:
    root = _prompts_root()
    by_full_id: dict[str, PromptTemplate] = {}
    latest_by_id: dict[str, PromptTemplate] = {}

    for domain_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        domain = domain_dir.name
        if domain.startswith("_") or domain.startswith("."):
            continue
        if domain not in SUPPORTED_DOMAINS:
            _log.warning("prompt domain %r not in SUPPORTED_DOMAINS; ignored", domain)
            continue

        for md_path in sorted(domain_dir.glob("*.md")):
            m = _FILENAME_RE.match(md_path.name)
            if not m:
                _log.warning("prompt file %s does not match <name>.v<version>.md", md_path)
                continue
            name = m.group("name")
            version = m.group("version")
            tmpl = PromptTemplate(
                domain=domain,
                name=name,
                version=version,
                body=md_path.read_text(encoding="utf-8"),
                path=md_path,
            )
            by_full_id[tmpl.full_id] = tmpl

            existing = latest_by_id.get(tmpl.prompt_id)
            if existing is None or _version_key(version) > _version_key(existing.version):
                latest_by_id[tmpl.prompt_id] = tmpl

    return _RegistryState(by_full_id=by_full_id, latest_by_id=latest_by_id)


@lru_cache(maxsize=1)
def _state() -> _RegistryState:
    return _scan()


def reload() -> None:
    """테스트/핫리로드 — 파일 변경 후 캐시 무효화."""
    _state.cache_clear()


def get(prompt_id: str) -> PromptTemplate:
    """`"domain/name"` 또는 `"domain/name@v1.2"` → 템플릿. 없으면 `PromptNotFound`."""
    s = _state()
    tmpl = s.by_full_id.get(prompt_id) if "@" in prompt_id else s.latest_by_id.get(prompt_id)
    if tmpl is None:
        raise PromptNotFound(prompt_id)
    return tmpl


def render(prompt_id: str, variables: dict[str, str]) -> tuple[str, PromptTemplate]:
    """`get()` + `render()` 묶음. Tool Executor 의 주된 진입점."""
    tmpl = get(prompt_id)
    return tmpl.render(variables), tmpl


def list_all() -> list[PromptTemplate]:
    """모든 등록 템플릿 (도메인/이름/버전 정렬)."""
    return sorted(
        _state().by_full_id.values(),
        key=lambda t: (t.domain, t.name, _version_key(t.version)),
    )
