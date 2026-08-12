"""
The macro watcher: pre-fetching a release so the print is already in hand.

The proposal that prompted this was 40 calls across a 5-second burst. Two
things are wrong with that and only one is the rate limit -- the other is that
concurrency does not shorten a wait for a scheduled event. Forty questions
asked at 08:30:00.0 all get yesterday's answer.
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import econ_calendar as ec
from dashboard import macro_watch as mw

DAY = datetime.date(2026, 8, 12)


def _entry(slug="cpi", day=DAY, period="July 2026"):
    return {"release": "Consumer Price Index", "slug": slug,
            "reference_period": period, "date": day, "time_et": "08:30 AM"}


def _at(offset_seconds):
    return ec.release_moment(_entry()) + datetime.timedelta(seconds=offset_seconds)


@pytest.fixture(autouse=True)
def _registered_quota(monkeypatch):
    monkeypatch.setattr(ec.BLS_LIMITER, "daily_cap", 500)
    monkeypatch.setattr(ec.BLS_LIMITER, "remaining_today", lambda: 500)
    mw.WATCH_STATE.update({"calls": 0, "windows": 0, "last_error": None,
                           "watching": None, "last_landed": None})


# =====================================================================
# The cadence
# =====================================================================

def test_the_peak_rate_stays_well_inside_the_documented_ceiling():
    """
    BLS documents 50 requests per 10 seconds. 40 calls in 5 seconds is 80 per
    10 -- over the limit against an API whose penalty is being cut off, on the
    one morning of the month it matters.
    """
    assert mw.peak_requests_per_10s() <= 50
    assert mw.peak_requests_per_10s() <= 15, (
        "and it should keep a wide margin, because the gain above this is "
        "fractions of a second on a monthly number")


def test_a_single_window_cannot_eat_the_day():
    """
    Worst case if the release never arrives. A 90-second lead at a one-second
    cadence made this 179 -- ninety of them guaranteed stale, because a
    publication cannot be detected before it happens.
    """
    assert mw.worst_case_calls() <= 110, mw.worst_case_calls()
    assert 500 - mw.worst_case_calls() > ec.quota_reserve(), (
        "a single late release would strand every other tool")


def test_polling_is_densest_where_publication_is_most_likely():
    """The defensible half of "burst at the release": sample hardest at the
    instant, not uniformly across twenty minutes."""
    assert mw.poll_interval(0) < mw.poll_interval(60) < mw.poll_interval(600)
    assert mw.poll_interval(-mw.LEAD.total_seconds()) == mw.poll_interval(0), (
        "the lead-in is part of the dense phase; BLS is sometimes a moment early")


def test_the_window_eventually_closes():
    assert mw.poll_interval(1199) is not None
    assert mw.poll_interval(1201) is None, "a poll loop that never gives up is a leak"


def test_a_punctual_release_costs_almost_nothing():
    """
    The whole argument against the burst: when the print is there, one call
    finds it. Forty would have found the same thing forty times.
    """
    calls = {"n": 0}

    def fetch(keys, **kwargs):
        calls["n"] += 1
        return {"cpi": {"observations": [{"year": "2026", "period": "M07"}]}}

    original = ec.fetch_bls_series
    ec.fetch_bls_series = fetch
    try:
        landed, made = mw.watch_release(_entry(), sleeper=lambda s: None,
                                        clock=lambda: _at(1))
    finally:
        ec.fetch_bls_series = original

    assert landed is True
    assert made == 1, f"a punctual release should cost one call, cost {made}"


def test_a_late_release_polls_and_gives_up_rather_than_looping_forever():
    ticks = iter([-90, -10, 0, 5, 30, 200, 900, 1300])

    def clock():
        return _at(next(ticks))

    original = ec.fetch_bls_series
    ec.fetch_bls_series = lambda keys, **kw: {
        "cpi": {"observations": [{"year": "2026", "period": "M06"}]}}   # still June
    try:
        landed, made = mw.watch_release(_entry(), sleeper=lambda s: None,
                                        clock=clock)
    finally:
        ec.fetch_bls_series = original

    assert landed is False
    assert made > 0 and made < 20, f"{made} calls to conclude it was late"


def test_the_quota_reserve_stops_the_watcher_too(monkeypatch):
    """The reserve is not only for the on-demand path."""
    monkeypatch.setattr(ec, "can_afford_window", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr(ec, "fetch_bls_series",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    landed, made = mw.watch_release(_entry(), sleeper=lambda s: None,
                                    clock=lambda: _at(0))
    assert landed is False and made == 0 and called["n"] == 0


def test_a_transport_error_does_not_end_the_watch():
    """A watcher that dies on one bad response stops watching every future
    release, and nothing would say so."""
    ticks = iter([0, 5, 30, 1300])
    calls = {"n": 0}

    def flaky(keys, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return {"cpi": {"observations": [{"year": "2026", "period": "M06"}]}}

    original = ec.fetch_bls_series
    ec.fetch_bls_series = flaky
    try:
        landed, made = mw.watch_release(_entry(), sleeper=lambda s: None,
                                        clock=lambda: _at(next(ticks)))
    finally:
        ec.fetch_bls_series = original

    assert calls["n"] > 1, "gave up after the first failure"
    assert mw.WATCH_STATE["last_error"], "the failure was swallowed silently"


# =====================================================================
# Choosing what to watch
# =====================================================================

def test_a_release_whose_series_we_do_not_publish_is_not_watched():
    assert not ec.RELEASE_SERIES.get("realer"), "fixture assumption changed"
    moment, entry = mw.next_release([_entry(slug="realer")], _at(-3600))
    assert entry is None


def test_the_soonest_release_is_chosen():
    later = dict(_entry(), date=DAY + datetime.timedelta(days=1))
    moment, entry = mw.next_release([later, _entry()], _at(-3600))
    assert ec.release_moment(entry).date() == DAY


def test_a_release_whose_window_has_closed_is_not_chosen():
    moment, entry = mw.next_release([_entry()], _at(3600))
    assert entry is None


def test_nothing_is_watched_when_the_calendar_is_empty():
    assert mw.next_release([], _at(0)) == (None, None)
    assert mw.next_release(None, _at(0)) == (None, None)


# =====================================================================
# Reporting
# =====================================================================

def test_the_status_line_says_when_it_is_not_running():
    mw.WATCH_STATE["running"] = False
    text = mw.status()
    assert "not running" in text
    assert "fresh on demand" in text, (
        "not running is not the same as not working, and the difference "
        "belongs in the sentence")


def test_the_status_line_reports_what_it_has_spent():
    mw.WATCH_STATE.update({"running": True, "windows": 2, "calls": 7})
    try:
        text = mw.status()
        assert "2 windows" in text and "7 API calls" in text
    finally:
        mw.WATCH_STATE["running"] = False
