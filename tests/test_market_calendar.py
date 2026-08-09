"""
US equity market calendar tests.

Dates checked against the published NYSE holiday calendar. These matter because
the staleness threshold is now expressed in trading sessions -- a wrong holiday
means either a false stale alarm or a missed outage.
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import market_calendar as mc


@pytest.mark.parametrize("date,label", [
    (datetime.date(2025, 1, 1),  "New Year's Day 2025"),
    (datetime.date(2025, 1, 20), "MLK Jr. Day 2025"),
    (datetime.date(2025, 2, 17), "Presidents' Day 2025"),
    (datetime.date(2025, 4, 18), "Good Friday 2025"),
    (datetime.date(2025, 5, 26), "Memorial Day 2025"),
    (datetime.date(2025, 6, 19), "Juneteenth 2025"),
    (datetime.date(2025, 7, 4),  "Independence Day 2025"),
    (datetime.date(2025, 9, 1),  "Labor Day 2025"),
    (datetime.date(2025, 11, 27), "Thanksgiving 2025"),
    (datetime.date(2025, 12, 25), "Christmas 2025"),
    (datetime.date(2026, 4, 3),  "Good Friday 2026"),
    (datetime.date(2026, 11, 26), "Thanksgiving 2026"),
])
def test_known_market_holidays(date, label):
    assert not mc.is_trading_day(date), f"{label} should be a market holiday"


@pytest.mark.parametrize("date,label", [
    (datetime.date(2026, 8, 7),  "ordinary Friday"),
    (datetime.date(2025, 7, 7),  "Monday after Independence Day"),
    (datetime.date(2026, 4, 6),  "Monday after Good Friday"),
])
def test_known_trading_days(date, label):
    assert mc.is_trading_day(date), f"{label} should be a trading day"


def test_weekends_are_never_trading_days():
    d = datetime.date(2026, 8, 1)  # a Saturday
    for _ in range(8):
        if d.weekday() >= 5:
            assert not mc.is_trading_day(d)
        d += datetime.timedelta(days=1)


def test_observed_shift_for_weekend_holidays():
    # 4 July 2026 falls on a Saturday -> observed the preceding Friday, 3 July.
    assert not mc.is_trading_day(datetime.date(2026, 7, 3))
    # 25 Dec 2027 falls on a Saturday -> observed Friday 24 December.
    assert not mc.is_trading_day(datetime.date(2027, 12, 24))
    # Sunday holidays roll forward: 4 July 2021 -> Monday 5 July.
    assert not mc.is_trading_day(datetime.date(2021, 7, 5))


def test_new_year_saturday_does_not_close_the_preceding_friday():
    """
    NYSE-specific carve-out: a Saturday 1 January is not rolled back. The
    exchange traded on Friday 31 December 2021.
    """
    assert mc.is_trading_day(datetime.date(2021, 12, 31))
    # Sunday 1 Jan 2023 still rolls forward to Monday 2 Jan.
    assert not mc.is_trading_day(datetime.date(2023, 1, 2))


def test_juneteenth_only_after_it_became_a_holiday():
    assert mc.is_trading_day(datetime.date(2019, 6, 19)), "not a market holiday in 2019"
    assert not mc.is_trading_day(datetime.date(2025, 6, 19))


def test_previous_trading_day_skips_the_weekend():
    monday = datetime.date(2026, 8, 10)
    assert mc.previous_trading_day(monday) == datetime.date(2026, 8, 7)


def test_previous_trading_day_skips_a_holiday_weekend():
    # Monday 2025-05-26 was Memorial Day; the prior session is Friday 2025-05-23.
    tuesday = datetime.date(2025, 5, 27)
    assert mc.previous_trading_day(tuesday) == datetime.date(2025, 5, 23)


def test_trading_days_between_counts_sessions_not_days():
    friday, monday = datetime.date(2026, 8, 7), datetime.date(2026, 8, 10)
    assert (monday - friday).days == 3          # three calendar days...
    assert mc.trading_days_between(friday, monday) == 1   # ...but one session


def test_trading_days_between_is_bounded():
    old = datetime.date(2000, 1, 3)
    assert mc.trading_days_between(old, datetime.date(2026, 8, 7)) == 999


def test_sessions_stale_treats_latest_session_as_fresh():
    latest = mc.previous_trading_day(datetime.date.today() + datetime.timedelta(days=1))
    assert mc.sessions_stale(latest) == 0


def test_sessions_stale_grows_one_per_session():
    latest = mc.previous_trading_day(datetime.date.today() + datetime.timedelta(days=1))
    prior = mc.previous_trading_day(latest)
    assert mc.sessions_stale(prior) == 1


# =====================================================================
# "since when?"
# =====================================================================

import pytest

NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize("text,expected_delta", [
    ("24h",       datetime.timedelta(hours=24)),
    ("1h",        datetime.timedelta(hours=1)),
    ("90m",       datetime.timedelta(minutes=90)),
    ("3d",        datetime.timedelta(days=3)),
    ("2 weeks",   datetime.timedelta(weeks=2)),
    ("  6 hours", datetime.timedelta(hours=6)),
])
def test_relative_windows(text, expected_delta):
    assert mc.parse_since(text, now=NOW) == NOW - expected_delta


def test_a_bare_date_is_read_as_midnight_utc():
    assert mc.parse_since("2026-08-01", now=NOW) == datetime.datetime(
        2026, 8, 1, tzinfo=datetime.timezone.utc)


def test_an_iso_timestamp_keeps_its_time():
    assert mc.parse_since("2026-08-01T13:30:00Z", now=NOW) == datetime.datetime(
        2026, 8, 1, 13, 30, tzinfo=datetime.timezone.utc)


def test_the_result_is_always_timezone_aware():
    """
    It gets compared against SEC acceptance stamps, which are aware. A naive
    value raises TypeError the moment it meets one.
    """
    for text in ("24h", "2026-08-01", "2026-08-01 13:30:00", "2026-08-01T13:30:00Z"):
        assert mc.parse_since(text, now=NOW).tzinfo is not None, text


def test_an_unreadable_window_is_refused_not_defaulted():
    """Quietly falling back to 24h answers a question that was not asked."""
    with pytest.raises(ValueError, match="Could not read"):
        mc.parse_since("since Monday", now=NOW)


def test_a_future_timestamp_is_refused():
    with pytest.raises(ValueError, match="future"):
        mc.parse_since("2027-01-01", now=NOW)


def test_a_zero_length_window_is_refused():
    with pytest.raises(ValueError, match="positive"):
        mc.parse_since("0d", now=NOW)


def test_an_empty_window_is_refused():
    with pytest.raises(ValueError, match="No 'since'"):
        mc.parse_since("", now=NOW)


def test_a_datetime_passes_through_and_gains_utc():
    naive = datetime.datetime(2026, 8, 1, 9, 0)
    assert mc.parse_since(naive, now=NOW) == datetime.datetime(
        2026, 8, 1, 9, 0, tzinfo=datetime.timezone.utc)
