"""First eligible attempt per user; missing starts remain in the denominator.

The rows contain timestamps/IDs only, never first-action text. This is descriptive
instrumentation, not evidence of a treatment effect or verified real-world action.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TypedDict


class Observation(TypedDict):
    user_id: str
    attempt_id: str
    entry_kind: str
    created_at: datetime
    viewed_at: datetime | None
    started_at: datetime | None
    step_completed_at: datetime | None


def summarize(
    rows: Iterable[Observation], *, as_of: datetime, window_minutes: int = 10
) -> dict[str, int]:
    first: dict[str, Observation] = {}
    for row in sorted(rows, key=lambda r: (r["created_at"], r["attempt_id"])):
        if row["entry_kind"] in ("restart", "resume") and row["viewed_at"]:
            first.setdefault(row["user_id"], row)
    eligible = starts = steps = pending = 0
    for row in first.values():
        viewed = row["viewed_at"]
        assert viewed is not None
        cutoff = viewed + timedelta(minutes=window_minutes)
        if cutoff > as_of:
            pending += 1
            continue
        eligible += 1
        started = row["started_at"]
        completed = row["step_completed_at"]
        starts += int(started is not None and viewed <= started <= cutoff)
        steps += int(
            completed is not None
            and started is not None
            and viewed <= started <= completed <= cutoff
        )
    return {
        "eligible_users": eligible,
        "started_users": starts,
        "first_step_reported_users": steps,
        "pending_window_users": pending,
    }
