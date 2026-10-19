from datetime import UTC, datetime, timedelta

from reaction_backend.domain.reentry_metrics import summarize


def test_denominator_keeps_nonstarters_and_excludes_initial_and_pending():
    now = datetime(2026, 10, 19, tzinfo=UTC)

    def row(user, kind="restart", start=None, step=None, viewed=None):
        return {
            "user_id": user,
            "attempt_id": user,
            "entry_kind": kind,
            "created_at": now,
            "viewed_at": viewed or now,
            "started_at": start,
            "step_completed_at": step,
        }

    rows = [
        row("no"),
        row("yes", start=now, step=now),
        row("initial", kind="initial", start=now),
        row("pending", viewed=now + timedelta(minutes=15)),
        row("late", start=now + timedelta(minutes=11)),
    ]
    result = summarize(rows + [rows[1]], as_of=now + timedelta(minutes=20))
    assert result == {
        "eligible_users": 3,
        "started_users": 1,
        "first_step_reported_users": 1,
        "pending_window_users": 1,
    }
