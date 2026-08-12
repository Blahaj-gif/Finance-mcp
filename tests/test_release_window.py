"""
Polling across a macro release, and the arithmetic that decides the cadence.

BLS publishes at an announced instant and a six-hour cache meant the number
people came for could be half a day stale on the morning it mattered. The
tempting fix -- burst the API across the release -- is eight times over the
documented rate limit and buys milliseconds, because polling faster does not
make BLS publish sooner.
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import econ_calendar as ec

ET = ec.ET


def _entry(day, clock="08:30 AM", slug="cpi", period="July 2026"):
    return {"release": "Consumer Price Index", "slug": slug,
            "reference_period": period, "date": day, "time_et": clock}


def _at(day, hour, minute=0, second=0):
    return datetime.datetime.combine(
        day, datetime.time(hour, minute, second), tzinfo=ET)


DAY = datetime.date(2026, 8, 12)


# =====================================================================
# The cadence, and why it is what it is
# =====================================================================

def test_the_cadence_stays_inside_the_documented_rate_limit():
    """
    BLS documents 50 requests per 10 seconds for a registered v2 key. The
    proposal that prompted this was 40 requests per second -- eight times over,
    and 80% of the 500/day quota spent in ten seconds.
    """
    requests_per_10s = 10 / ec.TTL_MACRO_LIVE
    assert requests_per_10s <= 50, "the fast cadence breaches BLS's rate limit"
    assert requests_per_10s < 5, (
        "and it should not sail close to it either -- the gain above this is "
        "milliseconds of detection latency")


def test_a_single_window_cannot_eat_the_day():
    """
    A flat 3s cadence across the full 21.5-minute window is 430 calls, 86% of
    the daily quota, spent watching a release that may not have arrived. The
    step down to 30s after two minutes is what makes the worst case affordable.
    """
    assert ec.poll_budget() < 150, f"worst case is {ec.poll_budget()} calls"
    reserve_survives = 500 - ec.poll_budget() > ec.QUOTA_RESERVE
    assert reserve_survives, "a single late release would strand every other tool"


def test_the_window_opens_before_the_announced_instant_and_closes_after():
    entries = [_entry(DAY)]
    assert ec.release_window(entries, _at(DAY, 8, 25)) is None, "too early"
    assert ec.release_window(entries, _at(DAY, 8, 29, 30)) is not None
    assert ec.release_window(entries, _at(DAY, 8, 30)) is not None
    assert ec.release_window(entries, _at(DAY, 8, 45)) is not None
    assert ec.release_window(entries, _at(DAY, 9, 0)) is None, "gave up too late"


def test_the_cadence_steps_down_once_a_release_is_demonstrably_late():
    entries = [_entry(DAY)]
    assert ec.macro_ttl(entries, _at(DAY, 8, 30)) == ec.TTL_MACRO_LIVE
    assert ec.macro_ttl(entries, _at(DAY, 8, 31)) == ec.TTL_MACRO_LIVE
    assert ec.macro_ttl(entries, _at(DAY, 8, 40)) == ec.TTL_MACRO_LATE
    assert ec.macro_ttl(entries, _at(DAY, 12, 0)) == ec.TTL_MACRO


def test_a_release_with_no_series_we_publish_is_not_watched():
    """There is no point polling the API across a release whose numbers we do
    not show. `realer` is on the same 08:30 slot as CPI and we carry none of
    it."""
    assert not ec.RELEASE_SERIES.get("realer"), "fixture assumption changed"
    entries = [_entry(DAY, slug="realer")]
    assert ec.release_window(entries, _at(DAY, 8, 30)) is None


def test_the_window_closes_when_the_print_lands_not_when_the_timer_expires():
    """
    Polling for something already in hand is the most expensive kind of
    nothing.
    """
    entries = [_entry(DAY)]
    during = _at(DAY, 8, 30, 5)
    assert ec.macro_ttl(entries, during, landed=False) == ec.TTL_MACRO_LIVE
    assert ec.macro_ttl(entries, during, landed=True) == ec.TTL_MACRO


def test_the_quota_reserve_stops_a_window_from_stranding_other_tools(monkeypatch):
    entries = [_entry(DAY)]
    monkeypatch.setattr(ec.BLS_LIMITER, "remaining_today",
                        lambda: ec.QUOTA_RESERVE - 1)
    assert ec.macro_ttl(entries, _at(DAY, 8, 30)) == ec.TTL_MACRO


# =====================================================================
# has_landed: about the period, not the clock
# =====================================================================

def _data(year, month, key="cpi"):
    return {key: {"observations": [{"year": str(year), "period": f"M{month:02d}",
                                    "value": "1.0"}]}}


def test_landing_is_judged_on_the_reference_period():
    """
    A payload refetched after publication still shows the prior month until BLS
    swaps it, and that is exactly the state worth polling through. A timestamp
    would call it landed; the period does not.
    """
    entry = _entry(DAY, period="July 2026")
    assert ec.has_landed(entry, _data(2026, 6)) is False, "June is the prior print"
    assert ec.has_landed(entry, _data(2026, 7)) is True


def test_landing_is_false_rather_than_raising_on_anything_unreadable():
    entry = _entry(DAY, period="July 2026")
    for payload in (None, {}, {"cpi": {}}, {"cpi": {"observations": []}},
                    {"cpi": {"observations": [{"year": "x", "period": "??"}]}}):
        assert ec.has_landed(entry, payload) is False
    assert ec.has_landed(_entry(DAY, period="not a period"), _data(2026, 7)) is False
    assert ec.has_landed(None, _data(2026, 7)) is False


# =====================================================================
# The announced instant
# =====================================================================

def test_the_release_moment_is_eastern_not_a_fixed_offset():
    """
    The schedule spans a daylight-saving boundary twice a year, and an hour is
    a long time to be wrong about on release morning.
    """
    summer = ec.release_moment(_entry(datetime.date(2026, 8, 12)))
    winter = ec.release_moment(_entry(datetime.date(2026, 1, 13)))
    assert summer.utcoffset() != winter.utcoffset()
    assert summer.hour == winter.hour == 8


def test_a_date_survives_a_json_round_trip():
    """Rows carry a real date object; serialising the calendar turns it into a
    string, and this is called on both sides of that."""
    as_object = ec.release_moment(_entry(DAY))
    as_text = ec.release_moment(_entry("2026-08-12"))
    assert as_object == as_text


def test_an_unreadable_row_yields_no_moment_rather_than_a_guess():
    assert ec.release_moment({}) is None
    assert ec.release_moment({"date": "the twelfth"}) is None
    # A missing time falls back to the 08:30 slot BLS uses for almost
    # everything, which is a documented convention rather than a guess.
    assert ec.release_moment({"date": DAY, "time_et": ""}).hour == 8
