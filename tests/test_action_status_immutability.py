"""원본 `action_item.status` 불변 — AGENTS.md §2 절대 규칙을 소스 레벨로 고정 (#20 DoD 4).

잠금 배경: 실패한 카드의 `status='failed'` 는 **Resilience 지표의 전제**다. 회복이 원본
status 를 손대면 "실패했지만 회복했다"를 셀 수 없어진다. 그래서 status 전이는 체크인
(execution 레이어) 한 곳만의 책임이고, 회복·replan·만료 cron 은 절대 건드리지 않는다.

왜 이런 형태의 테스트인가:
- 라우터 테스트(`test_recovery.py`)는 `original.status == "failed"` 를 확인하지만 **fake
  repo 를 태운다** — 실 `ActionItemRepo.create_from_recovery` 에 부모 status 쓰기를 넣어도
  잡히지 않는다(감사 실증). replan 쪽은 그 단언조차 0건이었다.
- 그래서 (a) 회복 경로 repo 메서드가 내보내는 **실 객체**를 검사하고, (b) `src/` 전체에서
  `ActionItem.status` 에 쓰는 **소스 위치 자체**를 화이트리스트로 못 박는다. 새 쓰기 지점이
  생기면 그게 의도된 것인지 여기서 한 번 멈춰 생각하게 된다.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import reaction_backend

_SRC = Path(reaction_backend.__file__).parent

# `ActionItem.status` 에 쓰는 것이 허용된 **유일한** 지점.
# (execution_repo.py docstring 이 "체크인 레이어가 합의된 유일한 변경 지점"이라 규정)
_ALLOWED_STATUS_WRITES = {
    # [▶시작] — 카드를 in_progress 로. 체크인 lifecycle 의 시작.
    ("api/routes/today.py", 'action.status = "in_progress"'),
    # Quick Check-in 4칩 — 사용자가 직접 고른 결과를 반영.
    ("api/routes/today.py", "action.status = body.completion_status"),
    # 저녁 일괄 회고 — check-in 과 동일 전이를 재현(사용자 입력 기반).
    ("api/routes/reflection.py", "action.status = item.completion_status"),
    # 신규 카드 생성 시 초기값 — 원본 변경이 아니다.
    ("orchestrator/first_plan_adapter.py", 'row.status = "planned"'),
}

_STATUS_WRITE = re.compile(r"^\s*(?!#)(\w+)\.status\s*=\s*(?!=)(.+?)\s*$", re.MULTILINE)
# 다른 도메인의 status(goal/inbox/habit/응답객체)는 대상이 아니다.
_NON_ACTION_OWNERS = {"g", "goal", "item", "habit", "self", "draft", "response"}


def _action_status_writes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        for owner, value in _STATUS_WRITE.findall(path.read_text(encoding="utf-8")):
            if owner in _NON_ACTION_OWNERS:
                continue
            # 인라인 주석은 계약이 아니다 — 값 부분만 비교(주석을 고쳐도 가드가 안 흔들린다).
            expr = value.split("#", 1)[0].strip()
            found.add((rel, f"{owner}.status = {expr}"))
    return found


def test_action_status_is_written_only_at_the_agreed_checkin_layer() -> None:
    """`ActionItem.status` 쓰기 지점이 화이트리스트를 벗어나지 않는다.

    회복·replan·만료 cron 이 원본 status 를 건드리면 여기서 즉시 실패한다 —
    AGENTS.md §2 "원본 action_item.status 를 회복 결정으로 변경하지 않는다".
    새 쓰기가 정당하다면(예: 새 체크인 경로) 화이트리스트에 **의식적으로** 추가할 것.
    """
    unexpected = _action_status_writes() - _ALLOWED_STATUS_WRITES

    assert not unexpected, (
        f"허가되지 않은 action status 쓰기: {sorted(unexpected)}\n"
        "회복/replan/cron 경로면 AGENTS.md §2 위반이다(Resilience 지표 전제). "
        "정당한 체크인 경로라면 _ALLOWED_STATUS_WRITES 에 추가할 것."
    )


def test_whitelist_has_no_stale_entries() -> None:
    """화이트리스트가 실제 코드와 어긋나지 않는다 — 죽은 항목이 남으면 가드가 헐거워진다."""
    stale = _ALLOWED_STATUS_WRITES - _action_status_writes()

    assert not stale, f"화이트리스트에만 있고 코드에 없는 항목: {sorted(stale)}"


async def test_create_from_recovery_does_not_touch_parent_status() -> None:
    """실 `ActionItemRepo.create_from_recovery` 가 부모 카드를 건드리지 않는다.

    fake 를 우회해 **실 메서드**를 태운다 — 라우터 테스트의 `original.status == "failed"`
    단언은 fake 를 검증하므로 이 메서드에 부모 status 쓰기를 넣어도 안 잡힌다.
    """
    from datetime import date

    from reaction_backend.db.models.action_item import ActionItem
    from reaction_backend.repositories.action_item_repo import ActionItemRepo

    parent = ActionItem()
    parent.id = uuid4()
    parent.status = "failed"

    added: list[ActionItem] = []

    class _Session:
        def add(self, obj: ActionItem) -> None:
            added.append(obj)

        async def flush(self) -> None:
            return None

        async def refresh(self, obj: ActionItem) -> None:
            return None

    repo = ActionItemRepo(_Session())  # type: ignore[arg-type]
    created = await repo.create_from_recovery(
        user_id=uuid4(),
        parent_action_item_id=parent.id,
        title="회복 카드",
        category="study",
        source="recovery_downscope",
        target_date=date(2026, 7, 20),
        estimated_minutes=15,
    )

    assert parent.status == "failed", "회복 생성이 원본 status 를 바꿨다 — Resilience 전제 파괴"
    assert created.parent_action_item_id == parent.id  # 혈통은 기록
    assert len(added) == 1  # 부모를 세션에 다시 add 하지 않는다
