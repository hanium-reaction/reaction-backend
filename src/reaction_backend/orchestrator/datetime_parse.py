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

# 4자리 연도는 `-`·`/`·`.` 어느 구분자든 받는다. "2028/03/01" 을 연도 없는 `M/D` 로 읽으면
# 앞의 2028 이 버려지고 "3/1" 만 남아 가까운 해로 옮겨졌다(2027-03-01).
_ISO = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_YMD = re.compile(r"(?<!\d)(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일?")
# "27년 10월 1일" — 두 자리 연도. `(?<!\d)` 가 "2027년" 의 뒤 두 자리에 걸리는 것을 막는다.
_YMD_SHORT = re.compile(r"(?<!\d)(\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일?")
_MD = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_MD_SLASH = re.compile(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\s*/|\d)")

# 상대 연도 낱말 → 올해 기준 몇 년 뒤. **긴 낱말부터** 본다 — "재작년" 안에 "작년" 이 있다.
_RELATIVE_YEARS: tuple[tuple[str, int], ...] = (
    ("내후년", 2),
    ("재작년", -2),
    ("내년", 1),
    ("명년", 1),
    ("작년", -1),
    ("올해", 0),
    ("금년", 0),
)

# 연도 없는 날짜가 **이만큼 안쪽으로 지났으면** 내년으로 넘기지 않고 지난 날짜 그대로 둔다.
#
# 9/18 에 "9월 15일까지" 는 사람에게 사흘 전의 마감이지 1년 뒤가 아니다. 내년으로 밀면
# 계획이 1년짜리로 늘어나고, 지난 마감을 되묻는 경로(#231)도 조용히 비켜 간다. 지난 날짜로
# 두면 그 경로가 "이미 지났는데 실제로는 언제까지인지" 를 사용자에게 묻는다 — 추측하지
# 않고 묻는 쪽이다. 12월의 "3월 2일" 처럼 한참 지난 날은 여전히 내년이다.
_RECENT_PAST_DAYS = 30


def parse_date(text: str, *, today: date) -> str | None:
    """ "7월 15일까지" → `"2026-07-15"`. 못 뽑으면 `None`.

    ⚠️ **연도 없는 표현이 이 함수의 존재 이유다.** "3월까지" 를 12월에 물으면 사람은
    당연히 내년으로 읽는데, LLM 은 그때그때 다르다. 규칙을 하나로 고정한다:

        연도가 없으면 **오늘 이후로 가장 가까운 해**를 고른다.
        단, 최근 `_RECENT_PAST_DAYS` 일 안에 지난 날이면 그 지난 날짜다.

    같은 달·같은 날이면 오늘로 본다(마감이 오늘인 경우가 실제로 있다).

    ⚠️ **연도를 말했으면 그 연도를 따른다.** "내년 10월 1일"·"27년 10월 1일" 의 연도 표시를
    버리고 월·일만 읽으면 1년 뒤 마감이 2주 뒤가 된다 — 그리고 룰이 LLM 값을 덮으므로
    (#432) LLM 이 맞게 읽어도 소용이 없었다.
    """
    s = text.strip()
    if not s:
        return None

    m = _ISO.search(s) or _YMD.search(s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _iso_or_none(y, mo, d)

    m = _YMD_SHORT.search(s)
    if m:
        yy, mo, d = (int(g) for g in m.groups())
        return _iso_or_none(2000 + yy, mo, d)

    m = _MD.search(s) or _MD_SLASH.search(s)
    if m:
        mo, d = (int(g) for g in m.groups())
        offset = next((n for word, n in _RELATIVE_YEARS if word in s), None)
        if offset is not None:
            return _iso_or_none(today.year + offset, mo, d)
        # 연도 없음 — 최근에 지난 날이면 그 날, 아니면 오늘 이후로 가장 가까운 해.
        this_year = _iso_or_none(today.year, mo, d)
        if this_year is not None:
            passed_days = (today - date.fromisoformat(this_year)).days
            if 0 < passed_days <= _RECENT_PAST_DAYS:
                return this_year
        for year in (today.year, today.year + 1):
            iso = _iso_or_none(year, mo, d)
            if iso is not None and date.fromisoformat(iso) >= today:
                return iso
        return this_year
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
# "밤 12시" 는 자정이다(낮 12시가 아니다). "저녁 12시" 도 같은 뜻으로 쓰인다.
_MIDNIGHT_MERIDIEM = ("밤", "저녁")
# 시와 분 사이 구분자(`시` | `:`)를 **잡아 둔다** — `:` 로 쓴 시각은 이미 24시간제다.
_HOUR = r"(?:(새벽|아침|오전|낮|점심|오후|저녁|밤)\s*)?(\d{1,2})\s*(시|:)\s*(\d{1,2})?\s*분?"
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
        mer1, h1, _sep1, min1, mer2, h2, sep2, min2 = m.groups()
        start = _to_hhmm(mer1, h1, min1)
        end = _to_hhmm(mer2, h2, min2, prev_hour=start, clock=sep2 == ":")
        if start is None or end is None:
            return None
        if end_is_window and end == "00:00":
            end = "24:00"
        # 끝이 시작보다 앞이면 **자정을 넘는 구간**이다("22:00-02:00"). 그대로 둔다 —
        # `first_plan_adapter._activity_awake_min` 이 이미 넘김을 두 구간으로 읽는다.
        return {"start": start, "end": end}

    # "밤 8시부터 자정까지" — 끝이 숫자가 아니라 낱말이다.
    if any(w in s for w in _MIDNIGHT_WORDS):
        head = re.search(_HOUR + r"\s*(?:부터|에서|~|-)", s)
        if head:
            mer, hh, _sep, mm = head.groups()
            start = _to_hhmm(mer, hh, mm)
            if start is not None:
                return {"start": start, "end": "24:00" if end_is_window else "00:00"}
    return None


def _to_hhmm(
    meridiem: str | None,
    hour: str,
    minute: str | None,
    *,
    prev_hour: str | None = None,
    clock: bool = False,
) -> str | None:
    """시각 토큰 하나 → "HH:MM". `clock=True` 는 "02:00" 처럼 `:` 로 쓴 24시간제 표기다."""
    h = int(hour)
    mi = int(minute) if minute else 0
    if not (0 <= h <= 24 and 0 <= mi < 60):
        return None
    if meridiem:
        base = _MERIDIEM[meridiem]
        if h == 12:
            # "오후 12시" 는 12시, "오전 12시" 는 0시 — 12 를 더하면 24시가 된다.
            # "밤 12시" 는 자정(0시)이다 — 구간 끝이면 호출부가 24:00 으로 바꾼다.
            h = 12 if base == 12 and meridiem not in _MIDNIGHT_MERIDIEM else 0
        else:
            h = h % 12 + base
    elif prev_hour is not None and not clock and 0 < h <= 12:
        # 오전/오후가 없는 뒤쪽 시각("9시~6시")은 **앞 시각보다 뒤**로 읽는다 — 단, 12 를
        # 더해야 실제로 앞 시각 뒤가 될 때만. "22시-2시" 의 2시에 12 를 더하면 14시가 되어
        # 밤 10시~낮 2시라는 엉뚱한 구간이 나온다. 그건 자정을 넘는 구간이다(02:00).
        #
        # ⚠️ **0시는 제외한다.** 자정은 모호하지 않은데(12시로 읽을 이유가 없다) 이 규칙에
        # 걸리면 "저녁 8시 ~ 0시" 가 20:00~12:00 이 된다 — 끝이 시작보다 앞선다.
        #
        # ⚠️ **`:` 로 쓴 시각("02:00")도 제외한다.** 이미 24시간제다 — FE 시간 다이얼이
        # "09:00-23:00" 형식을 쓰고, 자정을 넘는 구간은 '직접 입력' 으로 같은 형식을 적으라고
        # 안내한다. 여기에 12 를 더하면 "22:00-02:00" 이 22:00~14:00 이 됐다(실측).
        prev = int(prev_hour.split(":")[0])
        if h < prev < h + 12:
            h += 12
    if h > 24 or (h == 24 and mi):
        return None
    return f"{h % 24:02d}:{mi:02d}" if h != 24 else "24:00"
