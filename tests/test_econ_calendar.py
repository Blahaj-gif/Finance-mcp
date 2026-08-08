"""
Economic calendar and EDGAR client tests.

Offline: every network call is stubbed with a recorded response shape, so the
suite never touches BLS or the SEC. That matters twice over here — the BLS
unregistered tier allows 25 queries a day, and the SEC bans clients that
hammer it.
"""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import econ_calendar as ec


@pytest.fixture(autouse=True)
def clean_cache():
    ec.CACHE.clear()
    yield
    ec.CACHE.clear()


BLS_RESPONSE = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {"series": [{
        "seriesID": "CUUR0000SA0",
        "data": [
            {"year": "2026", "period": "M06", "periodName": "June", "value": "333.952", "footnotes": [{}]},
            {"year": "2026", "period": "M05", "periodName": "May", "value": "335.123", "footnotes": [{}]},
            {"year": "2026", "period": "M13", "periodName": "Annual", "value": "330.000", "footnotes": [{}]},
            {"year": "2025", "period": "M06", "periodName": "June", "value": "322.588", "footnotes": [{}]},
        ],
    }]},
}

RATE_RESPONSE = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {"series": [{
        "seriesID": "LNS14000000",
        "data": [
            {"year": "2026", "period": "M07", "periodName": "July", "value": "4.1", "footnotes": [{}]},
            {"year": "2026", "period": "M06", "periodName": "June", "value": "4.2", "footnotes": [{}]},
            {"year": "2025", "period": "M07", "periodName": "July", "value": "4.3", "footnotes": [{}]},
        ],
    }]},
}


# =====================================================================
# BLS
# =====================================================================

def test_bls_computes_month_and_year_changes(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(BLS_RESPONSE))
    out = ec.fetch_bls_series(["cpi"])
    latest = out["cpi"]["observations"][0]

    assert latest["period"] == "June" and latest["year"] == 2026
    assert latest["value"] == pytest.approx(333.952)
    assert latest["mom_pct"] == pytest.approx((333.952 / 335.123 - 1) * 100)
    assert latest["yoy_pct"] == pytest.approx((333.952 / 322.588 - 1) * 100)
    assert latest["change_unit"] == "%"


def test_bls_drops_the_annual_average_row(monkeypatch):
    """M13 is a yearly average, not a month; including it corrupts MoM."""
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(BLS_RESPONSE))
    obs = ec.fetch_bls_series(["cpi"])["cpi"]["observations"]
    assert all(o["month"] != 13 for o in obs)
    assert len(obs) == 3


def test_rate_series_change_is_in_percentage_points(monkeypatch):
    """
    Unemployment 4.2 -> 4.1 is -0.1pp. Reporting the percent change of a
    percentage (-2.38%) reads as five times the move that occurred.
    """
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(RATE_RESPONSE))
    latest = ec.fetch_bls_series(["unemployment"])["unemployment"]["observations"][0]
    assert latest["change_unit"] == "pp"
    assert latest["mom_pct"] == pytest.approx(-0.1)
    assert latest["yoy_pct"] == pytest.approx(-0.2)


def test_bls_surfaces_an_api_rejection(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(
        {"status": "REQUEST_NOT_PROCESSED", "message": ["daily threshold reached"]}))
    with pytest.raises(RuntimeError, match="daily threshold"):
        ec.fetch_bls_series(["cpi"])


def test_bls_rejects_unknown_series():
    with pytest.raises(ValueError, match="No known series"):
        ec.fetch_bls_series(["not_a_series"])


def test_bls_result_is_cached(monkeypatch):
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return json.dumps(BLS_RESPONSE)

    monkeypatch.setattr(ec, "_http", counting)
    ec.fetch_bls_series(["cpi"])
    ec.fetch_bls_series(["cpi"])
    assert calls["n"] == 1, "the unregistered tier allows only 25 queries a day"


# =====================================================================
# Release calendar
# =====================================================================

SCHEDULE_HTML = """
<table class="release-list">
  <tr><th>Reference Month</th><th>Release Date</th><th>Release Time</th></tr>
  <tr><td>June 2026</td><td>Jul. 14, 2026</td><td>08:30 AM</td></tr>
  <tr><td>July 2026</td><td>Aug. 12, 2026</td><td>08:30 AM</td></tr>
  <tr><td>August 2026</td><td>Sept. 11, 2026</td><td>08:30 AM</td></tr>
</table>
"""


def test_schedule_parsing(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: SCHEDULE_HTML)
    entries = ec.fetch_release_schedule("cpi")

    assert len(entries) == 3
    assert entries[0]["date"] == datetime.date(2026, 7, 14)
    assert entries[1]["date"] == datetime.date(2026, 8, 12)
    assert entries[2]["date"] == datetime.date(2026, 9, 11)   # "Sept." abbreviation
    assert entries[0]["release"] == "Consumer Price Index"
    assert entries[0]["time_et"] == "08:30 AM"


def test_schedule_entries_are_chronological(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: SCHEDULE_HTML)
    dates = [e["date"] for e in ec.fetch_release_schedule("cpi")]
    assert dates == sorted(dates)


@pytest.mark.parametrize("text,expected", [
    ("Aug. 12, 2026", datetime.date(2026, 8, 12)),
    ("Sept. 11, 2026", datetime.date(2026, 9, 11)),
    ("May 12, 2026", datetime.date(2026, 5, 12)),
    ("Jan. 05, 2027", datetime.date(2027, 1, 5)),
    ("not a date", None),
    ("Feb. 30, 2026", None),      # invalid day
])
def test_release_date_parsing(text, expected):
    assert ec._parse_release_date(text) == expected


def test_upcoming_releases_filters_to_the_window(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: SCHEDULE_HTML)
    found, failed = ec.upcoming_releases(days_ahead=3650, days_back=3650, slugs=["cpi"])
    assert len(found) == 3 and not failed

    found, _ = ec.upcoming_releases(days_ahead=0, days_back=0, slugs=["cpi"])
    assert found == []


def test_upcoming_releases_reports_a_failed_source_instead_of_dropping_it(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bls unreachable")

    monkeypatch.setattr(ec, "_http", boom)
    found, failed = ec.upcoming_releases(slugs=["cpi", "ppi"])
    assert found == []
    assert len(failed) == 2 and "bls unreachable" in failed[0]


def test_unknown_release_slug_is_rejected():
    with pytest.raises(ValueError, match="Unknown release"):
        ec.fetch_release_schedule("nonsense")


# =====================================================================
# SEC EDGAR
# =====================================================================

TICKER_MAP = {"0": {"cik_str": 723125, "ticker": "MU", "title": "MICRON TECHNOLOGY INC"}}

SUBMISSIONS = {
    "name": "MICRON TECHNOLOGY INC",
    "filings": {"recent": {
        "accessionNumber": ["0000723125-26-000015", "0000723125-26-000013"],
        "filingDate": ["2026-06-24", "2026-06-24"],
        "reportDate": ["2026-05-28", "2026-06-24"],
        "acceptanceDateTime": ["2026-06-24T22:59:46.000Z", "2026-06-24T20:02:01.000Z"],
        "form": ["10-Q", "8-K"],
        "items": ["", "2.02,9.01"],
        "primaryDocument": ["mu-20260528.htm", "mu-20260624.htm"],
        "primaryDocDescription": ["10-Q", "8-K"],
    }},
}


def _stub_sec(monkeypatch):
    monkeypatch.setattr(ec, "SEC_USER_AGENT", "Test Harness (test@example.com)")

    def router(url, *a, **k):
        if "company_tickers" in url:
            return json.dumps(TICKER_MAP)
        if "submissions" in url:
            return json.dumps(SUBMISSIONS)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(ec, "_http", router)


def test_ticker_to_cik_zero_pads(monkeypatch):
    _stub_sec(monkeypatch)
    assert ec.ticker_to_cik("mu")["cik"] == "0000723125"


def test_unknown_ticker_is_rejected(monkeypatch):
    _stub_sec(monkeypatch)
    with pytest.raises(ValueError, match="No SEC registrant"):
        ec.ticker_to_cik("ZZZZ")


def test_company_filings_exposes_acceptance_timestamp(monkeypatch):
    """Filing date is day-resolution; acceptance is what makes this near-real-time."""
    _stub_sec(monkeypatch)
    rows = ec.company_filings("MU")
    assert rows[0]["acceptance"].startswith("2026-06-24T22:59:46")
    assert rows[0]["form"] == "10-Q"
    assert rows[0]["url"].startswith("https://www.sec.gov/Archives/edgar/data/723125/")


def test_company_filings_filters_by_form(monkeypatch):
    _stub_sec(monkeypatch)
    rows = ec.company_filings("MU", forms=["8-K"])
    assert len(rows) == 1
    assert rows[0]["items"] == "2.02,9.01"   # 2.02 = results of operations


def test_company_filings_respects_the_limit(monkeypatch):
    _stub_sec(monkeypatch)
    assert len(ec.company_filings("MU", limit=1)) == 1


def test_sec_calls_refuse_to_run_without_a_user_agent(monkeypatch):
    """
    SEC policy requires a contact address. Sending anonymously risks an IP ban,
    so this fails locally with an actionable message instead.
    """
    monkeypatch.setattr(ec, "SEC_USER_AGENT", "")
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        ec._sec_headers()


ATOM = """<feed>
<entry><title>8-K - OneMain Holdings, Inc. (0001584207) (Filer)</title>
<updated>2026-08-07T17:30:46-04:00</updated>
<link href="https://www.sec.gov/x.htm"/></entry>
<entry><title>8-K - Société Générale SA (0000123456) (Filer)</title>
<updated>2026-08-07T17:25:00-04:00</updated>
<link href="https://www.sec.gov/y.htm"/></entry>
</feed>"""


def test_live_feed_parses_and_normalizes(monkeypatch):
    monkeypatch.setattr(ec, "SEC_USER_AGENT", "Test (t@e.com)")
    monkeypatch.setattr(ec, "_http", lambda *a, **k: ATOM)
    rows = ec.live_filings("8-K", count=10)

    assert len(rows) == 2
    assert rows[0]["company"] == "OneMain Holdings, Inc."
    assert rows[0]["cik"] == "0001584207"
    assert rows[0]["accepted"].startswith("2026-08-07T17:30:46")
    # Accented filer names survive normalization and are reported as Latin.
    assert rows[1]["company"] == "Société Générale SA"
    assert "Latin" in rows[1]["scripts"]


# =====================================================================
# Rate / polling layer
# =====================================================================

def test_daily_cap_is_enforced():
    limiter = ec._HostLimiter(min_interval=0.0, daily_cap=3)
    for _ in range(3):
        limiter.acquire()
    assert limiter.remaining_today() == 0
    with pytest.raises(RuntimeError, match="Daily request cap"):
        limiter.acquire()


def test_daily_cap_resets_on_a_new_day():
    limiter = ec._HostLimiter(min_interval=0.0, daily_cap=2)
    limiter.acquire()
    limiter._day = datetime.date.today() - datetime.timedelta(days=1)
    assert limiter.remaining_today() == 2


def test_uncapped_limiter_reports_no_remaining_count():
    assert ec._HostLimiter(min_interval=0.0).remaining_today() is None


def test_sec_limiter_stays_under_ten_per_second():
    assert ec.SEC_LIMITER.min_interval >= 0.1, "SEC allows at most 10 requests/second"


def test_schedule_scrapes_do_not_consume_the_api_quota(monkeypatch):
    """
    Release-schedule pages are ordinary web fetches. Charging them against the
    25/day API allowance burned 8 queries on a single calendar call.
    """
    monkeypatch.setattr(ec, "_http", lambda *a, **k: SCHEDULE_HTML)
    before = ec.BLS_LIMITER.remaining_today()
    ec.fetch_release_schedule("cpi")
    ec.fetch_release_schedule("ppi")
    assert ec.BLS_LIMITER.remaining_today() == before
    assert ec.BLS_WEB_LIMITER.daily_cap is None


def test_source_status_reports_configuration():
    status = ec.source_status()
    assert status["bls"]["daily_cap"] in (25, 500)
    assert "rate_limit" in status["sec"]
