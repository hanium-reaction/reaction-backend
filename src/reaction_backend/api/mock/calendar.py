"""Calendar mock fixture — #3-C 스텁용 (S04).

데모 freebusy 구간. 실제 Google OAuth·freebusy 조회는 후속 (integrations/google_calendar).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from reaction_backend.schemas.common import KST

DEMO_CALENDAR_PROVIDER = "google"


@dataclass(frozen=True, slots=True)
class DemoBusyInterval:
    """freebusy 의 busy 구간 한 개."""

    start: datetime
    end: datetime


# GET /calendar/freebusy 응답용 busy 구간 (KST).
DEMO_FREEBUSY: tuple[DemoBusyInterval, ...] = (
    DemoBusyInterval(
        datetime(2026, 5, 25, 9, 0, tzinfo=KST), datetime(2026, 5, 25, 10, 30, tzinfo=KST)
    ),
    DemoBusyInterval(
        datetime(2026, 5, 25, 14, 0, tzinfo=KST), datetime(2026, 5, 25, 15, 0, tzinfo=KST)
    ),
)
