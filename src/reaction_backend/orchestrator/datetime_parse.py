"""사용자 답에서 날짜·시각을 **룰로 먼저** 뽑는다 (#432).

LLM 도 같은 일을 할 수 있지만, 흔하고 확실한 표현에서까지 조용히 틀린다 — 연도 경계
("12월에 '3월까지'" 가 올해인지 내년인지)와 자정 넘김이 대표적이다. 이 모듈이 잡는 것은
**파서가 확실히 이기는 표현**뿐이고, 나머지("이번 학기 말", "시험 끝나고")는 LLM 에 남긴다.

⚠️ **여기서 못 뽑으면 `None` 이다.** 억지로 맞히지 않는다 — 틀린 값을 저장하면 사용자가
그 슬롯을 정정할 기회를 잃는다(하베스팅이 같은 이유로 confidence 게이트를 둔다).

## 자정 계약

`first_plan_adapter._hhmm_to_min` 이 이미 정한 규약을 그대로 따른다:

    구간의 **끝**에 오는 "00:00" 은 **하루 끝(24:00)** 이다.

그래서 "밤 8시부터 자정까지" 는 `{"start": "20:00", "end": "24:00"}` 이다. `"00:00"` 으로
두면 `_activity_awake_min` 이 자정 넘김으로 읽어 **구간을 둘로 쪼갠다**(20:00~24:00 과
00:00~00:00) — 의도와 다르다.
"""

from __future__ import annotations

import re
from datetime import date

__all__ = ["parse_date", "parse_time_range"]


# ── 날짜 ────────────────────────────────────────────────────────────────────

_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_YMD = re.compile(r"(?<!\d)(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일?")
_MD = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_MD_SLASH = re.compile(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\s*/|\d)")


def parse_date(text: str, *, today: date) -> str | None:
    """ "7월 15일까지" → `"2026-07-15"`. 못 뽑으면 `None`.

    ⚠️ **연도 없는 표현이 이 함수의 존재 이유다.** "3월까지" 를 12월에 물으면 사람은
    당연히 내년으로 읽는데, LLM 은 그때그때 다르다. 규칙을 하나로 고정한다:

        연도가 없으면 **오늘 이후로 가장 가까운 해**를 고른다.

    같은 달·같은 날이면 오늘로 본다(마감이 오늘인 경우가 실제로 있다).
    """
    s = text.strip()
    if not s:
        return None

    m = _ISO.search(s) or _YMD.search(s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _iso_or_none(y, mo, d)

    m = _MD.search(s) or _MD_SLASH.search(s)
    if m:
        mo, d = (int(g) for g in m.groups())
        # 연도 없음 — 오늘 이후로 가장 가까운 해.
        for year in (today.year, today.year + 1):
            iso = _iso_or_none(year, mo, d)
            if iso is not None and date.fromisoformat(iso) >= today:
                return iso
        return _iso_or_none(today.year, mo, d)
    return None


def _iso_or_none(year: int, month: int, day: int) -> str | None:
    """실재하는 날짜면 ISO 문자열. 2월 30일 같은 값은 `None`(LLM 에 넘긴다)."""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


# ── 시각 구간 ───────────────────────────────────────────────────────────────

_MERIDIEM = {
    "새벽": 0,
    "아침": 0,
    "오전": 0,
    "낮": 12,
    "점심": 12,
    "오후": 12,
    "저녁": 12,
    "밤": 12,
}
_MIDNIGHT_WORDS = ("자정", "밤 12시", "밤12시", "0시")
_HOUR = r"(?:(새벽|아침|오전|낮|점심|오후|저녁|밤)\s*)?(\d{1,2})\s*(?:시|:)\s*(\d{1,2})?\s*분?"
_RANGE = re.compile(
    _HOUR + r"\s*(?:부터|에서|~|-|–|—|to)\s*" + _HOUR,
    re.IGNORECASE,
)


def parse_time_range(text: str, *, end_is_window: bool = True) -> dict[str, str] | None:
    """ ""밤 8시부터 자정까지" → `{"start": "20:00", "end": "24:00"}`. 못 뽑으면 `None`.

    `end_is_window=True` 면 끝의 자정을 **하루 끝(24:00)** 으로 쓴다 — 활동창·고정일정처럼
    "구간" 을 뜻하는 슬롯의 규약이다(모듈 docstring 참고). 순수한 시각 두 개를 원하면
    `False` 로 둔다.
    """
    s = text.strip()
    if not s:
        return None

    m = _RANGE.search(s)
    if m:
        mer1, h1, min1, mer2, h2, min2 = m.groups()
        start = _to_hhmm(mer1, h1, min1)
        end = _to_hhmm(mer2, h2, min2, prev_hour=start)
        if start is None or end is None:
            return None
        if end_is_window and end == "00:00":
            end = "24:00"
        return {"start": start, "end": end}

    # "밤 8시부터 자정까지" — 끝이 숫자가 아니라 낱말이다.
    if any(w in s for w in _MIDNIGHT_WORDS):
        head = re.search(_HOUR + r"\s*(?:부터|에서|~|-)", s)
        if head:
            mer, hh, mm = head.groups()
            start = _to_hhmm(mer, hh, mm)
            if start is not None:
                return {"start": start, "end": "24:00" if end_is_window else "00:00"}
    return None


def _to_hhmm(
    meridiem: str | None, hour: str, minute: str | None, *, prev_hour: str | None = None
) -> str | None:
    h = int(hour)
    mi = int(minute) if minute else 0
    if not (0 <= h <= 24 and 0 <= mi < 60):
        return None
    if meridiem:
        base = _MERIDIEM[meridiem]
        # "오후 12시" 는 12시, "오전 12시" 는 0시 — 12 를 더하면 24시가 된다.
        h = h % 12 + base if h != 12 else (12 if base == 12 else 0)
    elif prev_hour is not None and 0 < h <= 12:
        # 오전/오후가 없는 뒤쪽 시각("9시~6시")은 **앞 시각보다 뒤**로 읽는다.
        #
        # ⚠️ **0시는 제외한다.** 자정은 모호하지 않은데(12시로 읽을 이유가 없다) 이 규칙에
        # 걸리면 "저녁 8시 ~ 0시" 가 20:00~12:00 이 된다 — 끝이 시작보다 앞선다.
        prev = int(prev_hour.split(":")[0])
        if h < prev and h + 12 <= 24:
            h += 12
    if h > 24 or (h == 24 and mi):
        return None
    return f"{h % 24:02d}:{mi:02d}" if h != 24 else "24:00"
