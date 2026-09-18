"""날짜·시각을 **룰이 먼저** 뽑는다 (#432).

LLM 도 같은 일을 하지만 흔하고 확실한 표현에서까지 조용히 틀린다 — 연도 경계와 자정이
대표적이다. 이 파일이 지키는 것은 둘이다:

1. 파서가 **확실히 이기는 표현**에서 정확한가
2. 파서가 **못 하는 표현**에서 `None` 을 내는가 (억지로 맞히면 LLM 이 할 기회를 뺏는다)
"""

from __future__ import annotations

from datetime import date

import pytest

from reaction_backend.orchestrator.datetime_parse import parse_date, parse_time_range

# 12월 — 연도 경계가 실제로 물리는 시점.
_DEC = date(2026, 12, 10)
_JUN = date(2026, 6, 1)


# ── 날짜 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "today", "want"),
    [
        ("2026-07-15", _JUN, "2026-07-15"),
        ("2026년 7월 15일", _JUN, "2026-07-15"),
        ("2026년 7월 15일까지요", _JUN, "2026-07-15"),
        ("7월 15일까지", _JUN, "2026-07-15"),
        ("7/15", _JUN, "2026-07-15"),
        ("7 월 15 일", _JUN, "2026-07-15"),
    ],
)
def test_explicit_dates_are_parsed(text: str, today: date, want: str) -> None:
    assert parse_date(text, today=today) == want


def test_year_boundary_picks_the_upcoming_year() -> None:
    """⚠️ **이 함수의 존재 이유다.** 12월에 "3월 2일" 은 사람에게 내년이다.

    LLM 은 그때그때 다르게 답한다 — 규칙을 하나로 고정해 코드가 쥔다.
    """
    assert parse_date("3월 2일까지", today=_DEC) == "2027-03-02"
    # 아직 안 지난 날이면 올해다.
    assert parse_date("12월 25일", today=_DEC) == "2026-12-25"


def test_today_itself_counts_as_upcoming() -> None:
    """마감이 오늘인 경우가 실제로 있다 — 내년으로 밀면 안 된다."""
    assert parse_date("12월 10일", today=_DEC) == "2026-12-10"


# 9월 중순 — "내년"·"지난 마감" 이 실제로 물린 시점(배포 미러 실측, 2026-09-18).
_SEP = date(2026, 9, 18)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("내년 10월 1일", "2027-10-01"),
        ("내년 10월 1일까지요", "2027-10-01"),
        ("올해 12월 1일", "2026-12-01"),
        ("내후년 3월 1일", "2028-03-01"),
        ("27년 10월 1일", "2027-10-01"),
        ("2028/03/01", "2028-03-01"),
        ("2028.03.01", "2028-03-01"),
        ("2025/12/25", "2025-12-25"),  # 적힌 연도 그대로 — 지났으면 #231 이 되묻는다
    ],
)
def test_an_explicit_year_is_not_dropped(text: str, want: str) -> None:
    """⚠️ 연도를 말했으면 그 연도다 — 룰이 LLM 값을 덮으므로(#432) 여기서 버리면 끝이다.

    고치기 전: "내년 10월 1일" → 2026-10-01(2주 뒤), "27년 10월 1일" → 2026-10-01,
    "2028/03/01" → 2027-03-01. 1년짜리 마감이 2주로 압축돼 계획이 통째로 찌그러졌다.
    """
    assert parse_date(text, today=_SEP) == want


def test_a_recently_passed_date_stays_in_the_past() -> None:
    """사흘 전 날짜는 1년 뒤가 아니다 — 지난 날짜로 둬야 지난 마감 되묻기(#231)가 돈다.

    고치기 전: 9/18 에 "9월 15일까지" → 2027-09-15. 계획이 1년짜리로 늘어나고,
    사용자에게 "이미 지났는데 언제까지로 할까요" 를 물을 기회가 조용히 사라졌다.
    """
    assert parse_date("9월 15일까지", today=_SEP) == "2026-09-15"
    # 한참 지난 날은 여전히 다가오는 해다(12월의 "3월 2일" 과 같은 규칙).
    assert parse_date("8월 1일", today=_SEP) == "2027-08-01"


@pytest.mark.parametrize(
    "text",
    [
        "이번 학기 말",  # 뜻을 알아야 날짜가 된다 → LLM 몫
        "시험 끝나고",
        "3월까지",  # 일(日)이 없다 — 어느 날인지 모른다
        "2월 30일",  # 실재하지 않는 날짜
        "",
        "그냥 빨리요",
    ],
)
def test_returns_none_when_it_should_not_guess(text: str) -> None:
    """⚠️ `None` 은 "값이 없다" 가 아니라 **"룰이 판단하지 않았다"** 는 뜻이다.

    호출부가 그때 LLM 값으로 떨어진다. 억지로 맞히면 사용자가 정정할 기회를 잃는다.
    """
    assert parse_date(text, today=_DEC) is None


# ── 시각 구간 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("오전 9시부터 오후 11시까지", {"start": "09:00", "end": "23:00"}),
        ("9시부터 6시까지", {"start": "09:00", "end": "18:00"}),  # 뒤 시각은 앞보다 뒤로
        ("저녁 7시30분부터 밤 11시까지", {"start": "19:30", "end": "23:00"}),
        ("오후 12시부터 오후 1시까지", {"start": "12:00", "end": "13:00"}),
        ("9:00 ~ 23:00", {"start": "09:00", "end": "23:00"}),
    ],
)
def test_time_ranges_are_parsed(text: str, want: dict[str, str]) -> None:
    assert parse_time_range(text) == want


def test_midnight_end_becomes_the_end_of_day() -> None:
    """⚠️ **자정 계약** — 구간의 끝 자정은 `24:00` 이다.

    `"00:00"` 으로 두면 `first_plan_adapter._activity_awake_min` 이 자정 넘김으로 읽어
    **구간을 둘로 쪼갠다**(20:00~24:00 과 00:00~00:00). 사용자 의도와 다르다.
    """
    assert parse_time_range("밤 8시부터 자정까지") == {"start": "20:00", "end": "24:00"}
    assert parse_time_range("저녁 8시 ~ 0시") == {"start": "20:00", "end": "24:00"}
    # 구간이 아니라 시각 두 개를 원하면 계약을 끈다.
    assert parse_time_range("밤 8시부터 자정까지", end_is_window=False) == {
        "start": "20:00",
        "end": "00:00",
    }


def test_noon_and_midnight_meridiem_are_not_off_by_twelve() -> None:
    """ "오전 12시" 는 0시, "오후 12시" 는 12시다 — 기계적으로 12를 더하면 24시가 된다."""
    assert parse_time_range("오전 12시부터 오전 6시까지") == {"start": "00:00", "end": "06:00"}
    assert parse_time_range("오후 12시부터 오후 6시까지") == {"start": "12:00", "end": "18:00"}


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("22:00-02:00", {"start": "22:00", "end": "02:00"}),
        ("23:00~01:00", {"start": "23:00", "end": "01:00"}),
        ("13:00~02:00", {"start": "13:00", "end": "02:00"}),
        ("22시-2시", {"start": "22:00", "end": "02:00"}),
        ("저녁 7시부터 1시까지", {"start": "19:00", "end": "01:00"}),
    ],
)
def test_overnight_windows_are_kept_overnight(text: str, want: dict[str, str]) -> None:
    """⚠️ 자정을 넘는 구간은 **넘는 구간 그대로**다 — 끝이 시작보다 앞서도 된다.

    FE 시간 다이얼은 자정을 넘는 범위를 못 만들고 '직접 입력' 으로 "22:00-02:00" 처럼
    적으라고 안내한다. 고치기 전엔 그 02:00 에 12 를 더해 22:00~14:00 이 됐고(실측),
    계획이 새벽 3시~낮 2시(수업 시간)에 잡혔다. `_activity_awake_min` 은 넘김을 이미 안다.
    """
    assert parse_time_range(text) == want


def test_night_twelve_oclock_is_midnight_not_noon() -> None:
    """ "밤 12시" 는 자정이다 — 낮 12시로 읽으면 "아침 9시부터 밤 12시까지" 가 3시간이 된다."""
    assert parse_time_range("아침 9시부터 밤 12시까지") == {"start": "09:00", "end": "24:00"}
    assert parse_time_range("저녁 6시부터 밤 12시까지") == {"start": "18:00", "end": "24:00"}
    assert parse_time_range("밤 12시부터 오전 6시까지") == {"start": "00:00", "end": "06:00"}
    # "오후 12시" · "낮 12시" 는 여전히 정오다.
    assert parse_time_range("낮 12시부터 오후 3시까지") == {"start": "12:00", "end": "15:00"}


@pytest.mark.parametrize("text", ["아침에", "저녁쯤", "", "시간 되는 대로"])
def test_time_range_returns_none_when_it_should_not_guess(text: str) -> None:
    assert parse_time_range(text) is None
