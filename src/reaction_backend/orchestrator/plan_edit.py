"""Plan 직접 편집(S15) 순수 로직 — 15분 snap · 시간 충돌 · 정책 위반 (Issue #21-B).

DB/세션 비의존 — `ScheduledBlockRepo` 가 충돌 후보를, 라우터가 정책 목록을 넘기면
여기서 판정만 한다. `orchestrator/recovery.py` 처럼 단위 테스트 가능하게 유지.

정책 위반 검사 대상(#21-B): `sleep` · `lunch` · `late_night_block` 윈도우.
`no_touch`(요일별)와 고정 일정은 스케줄러와 같은 전개(`time_policies_to_busy`·
`fixed_schedules_to_busy`)를 라우터가 날짜별로 만들어 `first_busy_overlap` 으로 판정한다 —
요일·자정 넘김 규칙을 여기서 한 벌 더 만들면 스케줄러와 어긋난다.

첫 계획 초안 편집(`apply_draft_edits`)도 여기 둔다 — 승인 요청에 실린 편집본을 초안과
맞추는 규칙이라 DB 가 필요 없다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from reaction_backend.schemas.planning import ActionItemDraft, ScheduledBlockPreview

if TYPE_CHECKING:
    from reaction_backend.db.models.time_policy import TimePolicy
    from reaction_backend.orchestrator.goal_structuring import BusyBlock

_SNAP_MINUTES = 15
_MINUTES_PER_DAY = 24 * 60
# 스케줄러가 긴 카드를 나눌 때 붙이는 회차 꼬리표 — `plan_scheduler` 의 "제목 (1/2)".
_SESSION_SUFFIX = re.compile(r"\s*\(\d+/\d+\)\s*$")


def snap_to_15min(dt: datetime) -> datetime:
    """가장 가까운 15분 경계로 스냅 (초/마이크로 제거). 23:53 → 다음날 00:00 가능."""
    base = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    snapped = round((dt.hour * 60 + dt.minute) / _SNAP_MINUTES) * _SNAP_MINUTES
    return base + timedelta(minutes=snapped)


def _intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """반열린 구간 [start, end) 겹침."""
    return a_start < b_end and b_start < a_end


def _block_day_intervals(start_kst: datetime, end_kst: datetime) -> list[tuple[int, int]]:
    """블록을 0~1440 분 축의 [a,b) 구간(자정 넘으면 2개)으로 변환."""
    start_min = start_kst.hour * 60 + start_kst.minute
    duration = max(int((end_kst - start_kst).total_seconds() // 60), 0)
    end_abs = start_min + duration
    if end_abs <= _MINUTES_PER_DAY:
        return [(start_min, end_abs)]
    return [(start_min, _MINUTES_PER_DAY), (0, end_abs - _MINUTES_PER_DAY)]


def _window_intervals(win_start: time, win_end: time) -> list[tuple[int, int]]:
    """시간대 윈도우 [start, end) → 분 구간. 자정 가로지르면(예: 23:00~07:00) 2개로 분할."""
    start_min = win_start.hour * 60 + win_start.minute
    end_min = win_end.hour * 60 + win_end.minute
    if end_min > start_min:
        return [(start_min, end_min)]
    # wrap: [start, 24:00) ∪ [00:00, end)
    intervals = [(start_min, _MINUTES_PER_DAY)]
    if end_min > 0:
        intervals.append((0, end_min))
    return intervals


def _touches_window(block: list[tuple[int, int]], window: list[tuple[int, int]]) -> bool:
    return any(_intervals_overlap(bs, be, ws, we) for bs, be in block for ws, we in window)


def _parse_time(raw: object) -> time | None:
    """정책 payload 의 `"HH:MM"` → time. `"24:00"`(하루 끝)은 `time(0, 0)` 으로 읽는다.

    스케줄러(`goal_structuring._parse_hhmm`)는 같은 값을 `time.max` 로 읽지만 여기서는
    `time(0, 0)` 이 맞다 — `_window_intervals` 는 끝이 시작보다 앞서면 자정을 넘는 창으로
    접으므로 16:00~24:00 이 정확히 `[16:00, 24:00)` 한 조각이 된다(`time.max` 로 읽으면
    23:59 까지라 하루의 마지막 1분이 창에서 빠진다).

    예전엔 `"24:00"` 이 ValueError 로 떨어져 **그 정책을 통째로 건너뛰었다**. 활동 시간대의
    여집합으로 만든 수면창(08:00~16:00 활동 → 수면 00:00~08:00 · 16:00~24:00)은 저녁 조각이
    늘 `"24:00"` 로 끝나므로, 주간 편집기가 저녁 수면창을 한 번도 보지 못했다.
    """
    if not isinstance(raw, str):
        return None
    if raw.strip() == "24:00":
        return time(0, 0)
    try:
        return time.fromisoformat(raw)
    except ValueError:
        return None


def find_policy_violation(
    start_kst: datetime,
    end_kst: datetime,
    category: str,
    policies: list[TimePolicy],
) -> str | None:
    """이동된 블록이 활성 시간 정책 윈도우에 들어가면 위반 policy_type 반환, 없으면 None."""
    block_intervals = _block_day_intervals(start_kst, end_kst)

    for policy in policies:
        if not policy.is_active:
            continue
        payload = policy.payload or {}
        ptype = policy.policy_type

        if ptype in ("sleep", "lunch"):
            win_start = _parse_time(payload.get("start_time"))
            win_end = _parse_time(payload.get("end_time"))
            if win_start is None or win_end is None:
                continue
            if _touches_window(block_intervals, _window_intervals(win_start, win_end)):
                return ptype

        elif ptype == "late_night_block":
            win_start = _parse_time(payload.get("start_time"))
            if win_start is None:
                continue
            blocked = payload.get("blocked_categories") or []
            # 카테고리 제한이 있으면 해당 카테고리만, 없으면 전부 금지.
            if blocked and category not in blocked:
                continue
            window = _window_intervals(win_start, time(0, 0))  # [start, 24:00)
            if _touches_window(block_intervals, window):
                return ptype

    return None


def spanned_days(start_kst: datetime, end_kst: datetime) -> list[date]:
    """[start, end) 가 걸치는 KST 날짜들 — 자정을 넘는 블록은 이틀. 끝이 딱 자정이면 그날 제외."""
    last = end_kst.date()
    if end_kst.time() == time(0, 0) and end_kst > start_kst:
        last = (end_kst - timedelta(microseconds=1)).date()
    days: list[date] = []
    cursor = start_kst.date()
    while cursor <= last:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def first_busy_overlap(
    start_kst: datetime, end_kst: datetime, busy: Iterable[BusyBlock]
) -> BusyBlock | None:
    """[start, end) 와 겹치는 첫 busy 구간 (반열린 구간 — 맞닿기만 하면 겹침 아님)."""
    for b in busy:
        if start_kst < b.interval.end and b.interval.start < end_kst:
            return b
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 첫 계획 초안 편집 반영 (HITL '수정', planA-2) — 승인 요청에 실린 편집본을 초안에 맞춘다.
# ─────────────────────────────────────────────────────────────────────────────


class DraftEditError(ValueError):
    """편집본이 초안과 맞지 않는다 — 라우터가 422 로 바꾼다(`code` 는 ErrorCode 이름)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DraftBlockEdit:
    """승인 요청의 편집된 블록 한 칸 — 시각은 KST aware, **보낸 그대로**(15분 snap 하지 않음).

    snap 하지 않는 이유는 `apply_draft_edits` 참고 — 초안 시각 자체가 15분 격자가 아니다.
    """

    origin_id: str
    start: datetime
    end: datetime
    title: str | None = None


@dataclass(frozen=True, slots=True)
class EditedDraft:
    """편집을 반영한 초안 — 영속화에 그대로 넘긴다.

    `moved` 는 초안에 **없던 시각**으로 놓인 블록만이다. 초안 그대로인 블록은 생성 단계에서
    이미 busy(다른 계획·고정 일정·정책)를 피해 놓였으므로 다시 볼 필요가 없고, 라우터는
    옮긴 것만 기존 일정·고정 일정과 대조한다.
    """

    blocks: list[ScheduledBlockPreview]
    action_items: list[ActionItemDraft]
    moved: list[ScheduledBlockPreview]
    dropped_origin_ids: list[str]


def card_title_from(title: str) -> str:
    """블록 제목 → 카드 제목. 회차 꼬리표("(1/2)")는 블록 표시용이라 카드 이름에서 뗀다."""
    return _SESSION_SUFFIX.sub("", title).strip()


def _to_minute(dt: datetime) -> datetime:
    """초·마이크로초를 떼 분 단위로 — FE 가 `…:00.000` 처럼 다르게 적어 보내도 같은 시각으로 본다."""
    return dt.replace(second=0, microsecond=0)


def apply_draft_edits(
    *,
    blocks: Sequence[ScheduledBlockPreview],
    action_items: Sequence[ActionItemDraft],
    edits: Sequence[DraftBlockEdit],
) -> EditedDraft:
    """사용자가 초안 화면에서 옮기고·지우고·이름 바꾼 결과를 초안에 반영한다.

    `edits` 는 **최종 블록 목록 전체**다(부분 패치가 아니다) — 그래야 "지운 블록"을 따로
    표시할 필요 없이 목록에서 빠진 것으로 알 수 있다.

    - 각 항목의 `origin_id` 는 초안 블록의 `originId` 중 하나여야 한다. 승인은 초안을 고치는
      자리라, 초안에 없던 카드를 여기서 새로 만들지는 않는다.
    - 한 카드의 블록이 목록에서 **모두** 빠지면 그 카드를 만들지 않는다(삭제). 초안에서 원래
      블록이 없던 카드(자리가 없어 못 놓인 것)는 화면에 없었으니 그대로 둔다.
    - 제목이 초안의 그 카드 블록 제목 어느 것과도 다르면 카드 이름을 바꾼 것으로 본다.
      회차 꼬리표만 붙은 원래 제목을 되돌려 보낸 것을 개명으로 오인하지 않기 위해서다.

    **시각은 15분 격자로 맞추지 않는다**(리뷰 반영). 스케줄러는 15분 격자에 놓지 않는다 —
    회차 사이 쉬는 시간(기본 10분) 때문에 두 번째 회차가 :10 에 시작하고, 수업이 10:50 에
    끝나면 블록도 10:50 에 시작하고, 10분짜리 회차도 있다. 예전엔 받은 시각을 전부 snap 한 뒤
    초안과 비교해서, **손대지 않은** 블록까지 "옮긴 블록"이 됐다 — 10:50 블록이 10:45 로 당겨져
    수업과 겹친다며 422 가 나거나, 13:10~14:10 이 13:15~14:15 로 저장돼 쉬는 시간이 사라지거나,
    12:40~12:50 이 12:45~12:45 가 돼 422 가 났다. 그래서
    - 받은 시각(분 단위)이 그 카드의 초안 칸과 **같으면** 초안 시각을 그대로 쓰고 `moved` 에
      넣지 않는다. 한 초안 칸을 두 번 보내면(같은 블록 중복) 422 로 막는다 — `moved` 가 아니라
      겹침 검사를 안 거치므로 여기서 거르지 않으면 똑같은 블록 두 개가 저장된다.
    - 다르면 사용자가 옮긴 것이고, **보낸 시각 그대로**(분 단위) 저장한다. 초안 화면은 끌기를
      "원래 시각 + 15분 단위" 로 움직이므로 10:50 블록을 옮기면 11:50 처럼 격자 밖 시각이
      화면에 보인다. 여기서 snap 하면 사용자가 승인한 화면과 다른 시각(11:45)이 **승인 뒤에야**
      저장된다 — PATCH 편집은 snap 된 결과를 바로 응답해 화면이 따라가지만 승인은 그런 확인
      단계가 없다. 시작·끝을 따로 snap 하면 10분 회차가 0분이 되거나 15분으로 늘어나는 문제도 있다.
    """
    draft_by_origin: dict[str, list[ScheduledBlockPreview]] = {}
    for b in blocks:
        if b.origin_id is not None:
            draft_by_origin.setdefault(b.origin_id, []).append(b)
    # (카드, 시작, 끝) → 초안 블록. 분 단위로 맞춰 비교하되 저장은 초안 블록의 시각 그대로.
    draft_slot_of: dict[tuple[str, datetime, datetime], ScheduledBlockPreview] = {
        (b.origin_id, _to_minute(b.start), _to_minute(b.end)): b
        for b in blocks
        if b.origin_id is not None
    }

    out_blocks: list[ScheduledBlockPreview] = []
    moved: list[ScheduledBlockPreview] = []
    claimed: set[tuple[str, datetime, datetime]] = set()
    renamed: dict[str, str] = {}
    for e in edits:
        template = draft_by_origin.get(e.origin_id)
        if not template:
            raise DraftEditError(
                "COMMON_VALIDATION_ERROR",
                "초안에 없는 일정이 섞여 있어요. 화면을 새로고침한 뒤 다시 시작해 주세요.",
            )
        start, end = _to_minute(e.start), _to_minute(e.end)
        if end <= start:
            raise DraftEditError("PLAN_INVALID_TIME", "종료 시각이 시작 시각보다 늦어야 해요.")
        new_title = (e.title or "").strip()
        draft_titles = {t.title for t in template}
        if new_title and new_title not in draft_titles:
            renamed.setdefault(e.origin_id, card_title_from(new_title) or new_title)
        slot = (e.origin_id, start, end)
        unmoved = draft_slot_of.get(slot)
        if unmoved is not None:
            if slot in claimed:
                raise DraftEditError(
                    "COMMON_VALIDATION_ERROR",
                    "같은 일정이 두 번 들어 있어요. 화면을 새로고침한 뒤 다시 시작해 주세요.",
                )
            claimed.add(slot)
            # 손대지 않은 블록 — 초안 블록 그대로(시각·회차 제목). 제목만 바꿨으면 제목만.
            out_blocks.append(unmoved.model_copy(update={"title": new_title or unmoved.title}))
            continue
        block = template[0].model_copy(
            update={"start": start, "end": end, "title": new_title or template[0].title}
        )
        out_blocks.append(block)
        moved.append(block)

    kept_origins = {b.origin_id for b in out_blocks}
    dropped = sorted(o for o in draft_by_origin if o not in kept_origins)
    out_actions: list[ActionItemDraft] = []
    for item in action_items:
        if item.node_id in dropped:
            continue
        title = renamed.get(item.node_id)
        out_actions.append(item.model_copy(update={"title": title}) if title else item)
    return EditedDraft(
        blocks=out_blocks,
        action_items=out_actions,
        moved=moved,
        dropped_origin_ids=dropped,
    )
