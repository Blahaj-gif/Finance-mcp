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
# Joining the schedule to the numbers
# =====================================================================

@pytest.mark.parametrize("text,expected", [
    ("July 2026",        (2026, 7, "month")),
    ("Jul 2026",         (2026, 7, "month")),
    ("December 2025",    (2025, 12, "month")),
    ("2nd Quarter 2026", (2026, 6, "quarter")),
    ("4th Quarter 2025", (2025, 12, "quarter")),
    ("Reference Month",  None),
    ("",                 None),
    (None,               None),
])
def test_reference_period_parsing(text, expected):
    assert ec.parse_reference_period(text) == expected


def _bls(series_id, rows):
    return {"seriesID": series_id,
            "data": [{"year": str(y), "period": f"M{m:02d}", "periodName": name,
                      "value": str(v), "footnotes": [{}]} for y, m, name, v in rows]}


# June and July both published; July is newest.
JOIN_RESPONSE = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {"series": [
        _bls("CUUR0000SA0", [(2026, 7, "July", 335.0), (2026, 6, "June", 334.0),
                             (2025, 7, "July", 325.0), (2025, 6, "June", 324.0)]),
        _bls("CUUR0000SA0L1E", [(2026, 7, "July", 330.0), (2026, 6, "June", 329.0),
                                (2025, 7, "July", 320.0), (2025, 6, "June", 319.0)]),
    ]},
}


def _entry(slug, release, ref, date):
    return {"slug": slug, "release": release, "reference_period": ref,
            "date": date, "time_et": "08:30 AM"}


def test_a_release_joins_to_its_own_reference_period_not_the_newest_reading(monkeypatch):
    """
    The CPI released in July *is* the June reading. Pairing each row with
    whatever observation happens to be newest would stamp July's number onto
    June's release and be wrong by exactly one month, invisibly.
    """
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(JOIN_RESPONSE))
    rows = [_entry("cpi", "Consumer Price Index", "June 2026", datetime.date(2026, 7, 14)),
            _entry("cpi", "Consumer Price Index", "July 2026", datetime.date(2026, 8, 12))]

    out, _ = ec.attach_release_values(rows, today=datetime.date(2026, 8, 20))

    june = [v for v in out[0]["values"] if v["series"] == "cpi"][0]
    july = [v for v in out[1]["values"] if v["series"] == "cpi"][0]
    assert june["period"] == "June 2026" and june["value"] == pytest.approx(334.0)
    assert july["period"] == "July 2026" and july["value"] == pytest.approx(335.0)
    assert out[0]["value_status"] == out[1]["value_status"] == "published"


def test_a_release_publishes_every_series_it_carries(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(JOIN_RESPONSE))
    out, _ = ec.attach_release_values(
        [_entry("cpi", "Consumer Price Index", "July 2026", datetime.date(2026, 8, 12))],
        today=datetime.date(2026, 8, 20))
    assert sorted(v["series"] for v in out[0]["values"]) == ["core_cpi", "cpi"]


def test_a_future_release_carries_the_prior_print_stamped_with_its_own_period(monkeypatch):
    """
    We have no consensus feed, so a scheduled row can only show what was last
    published. Labelling that with the *upcoming* reference period would turn a
    historical reading into an implied forecast.
    """
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(JOIN_RESPONSE))
    out, _ = ec.attach_release_values(
        [_entry("cpi", "Consumer Price Index", "August 2026", datetime.date(2026, 9, 11))],
        today=datetime.date(2026, 8, 20))

    row = [v for v in out[0]["values"] if v["series"] == "cpi"][0]
    assert out[0]["value_status"] == "scheduled"
    assert row["status"] == "scheduled"
    assert "value" not in row, "a scheduled release has no reading of its own"
    assert row["prior"]["period"] == "July 2026"


def test_a_release_that_is_late_says_so_rather_than_looking_scheduled(monkeypatch):
    """
    The date passed and the number is not in the API. Reporting that as
    'scheduled' hides exactly the case a reader needs to notice.
    """
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(JOIN_RESPONSE))
    out, warnings = ec.attach_release_values(
        [_entry("cpi", "Consumer Price Index", "August 2026", datetime.date(2026, 9, 11))],
        today=datetime.date(2026, 9, 20))

    assert out[0]["value_status"] == "awaiting"
    assert any("has not appeared" in w for w in warnings)


def test_payrolls_headline_is_a_level_change_not_a_percentage(monkeypatch):
    """
    Nonfarm payrolls is quoted as "+147,000 jobs". "+0.09%" is the same fact,
    correctly computed, and unrecognisable as the print it describes.
    """
    resp = {"status": "REQUEST_SUCCEEDED", "Results": {"series": [
        _bls("CES0000000001", [(2026, 7, "July", 160_147.0), (2026, 6, "June", 160_000.0),
                               (2025, 7, "July", 158_000.0)])]}}
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(resp))
    out, _ = ec.attach_release_values(
        [_entry("empsit", "Employment Situation", "July 2026", datetime.date(2026, 8, 7))],
        today=datetime.date(2026, 8, 20))

    pay = [v for v in out[0]["values"] if v["series"] == "payrolls"][0]
    assert pay["headline"]["kind"] == "level_change"
    assert pay["headline"]["number"] == pytest.approx(147.0)
    assert pay["headline"]["text"] == "+147k"


def test_a_level_change_with_no_prior_month_is_omitted_not_zeroed(monkeypatch):
    """A fabricated 0 there reads as 'no jobs added', which is a real claim."""
    resp = {"status": "REQUEST_SUCCEEDED", "Results": {"series": [
        _bls("CES0000000001", [(2026, 7, "July", 160_147.0)])]}}
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(resp))
    out, _ = ec.attach_release_values(
        [_entry("empsit", "Employment Situation", "July 2026", datetime.date(2026, 8, 7))],
        today=datetime.date(2026, 8, 20))
    pay = [v for v in out[0]["values"] if v["series"] == "payrolls"][0]
    assert pay["headline"] is None


def test_cpi_headline_is_the_year_over_year_rate_not_the_index_level(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(JOIN_RESPONSE))
    out, _ = ec.attach_release_values(
        [_entry("cpi", "Consumer Price Index", "July 2026", datetime.date(2026, 8, 12))],
        today=datetime.date(2026, 8, 20))
    cpi = [v for v in out[0]["values"] if v["series"] == "cpi"][0]

    assert cpi["headline"]["kind"] == "yoy"
    assert cpi["headline"]["number"] == pytest.approx((335.0 / 325.0 - 1) * 100)
    assert cpi["headline"]["index_level"] == pytest.approx(335.0)


def test_unemployment_headline_is_a_level_in_percent(monkeypatch):
    resp = {"status": "REQUEST_SUCCEEDED", "Results": {"series": [
        _bls("LNS14000000", [(2026, 7, "July", 4.1), (2026, 6, "June", 4.2)])]}}
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(resp))
    out, _ = ec.attach_release_values(
        [_entry("empsit", "Employment Situation", "July 2026", datetime.date(2026, 8, 7))],
        today=datetime.date(2026, 8, 20))
    une = [v for v in out[0]["values"] if v["series"] == "unemployment"][0]
    assert une["headline"]["text"] == "4.1%"
    assert une["change_unit"] == "pp"


def test_a_release_whose_series_we_do_not_carry_keeps_its_row(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(JOIN_RESPONSE))
    rows = [_entry("jolts", "Job Openings", "June 2026", datetime.date(2026, 8, 5)),
            _entry("cpi", "Consumer Price Index", "July 2026", datetime.date(2026, 8, 12))]
    out, _ = ec.attach_release_values(rows, today=datetime.date(2026, 8, 20))

    assert out[0]["value_status"] == "unmapped" and out[0]["values"] == []
    assert out[1]["value_status"] == "published"


def test_the_whole_calendar_costs_one_api_call(monkeypatch):
    """The unregistered BLS tier allows 25 queries a day. Per-row would burn it."""
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return json.dumps(JOIN_RESPONSE)

    monkeypatch.setattr(ec, "_http", counting)
    rows = [_entry("cpi", "Consumer Price Index", f"{m} 2026", datetime.date(2026, 8, 12))
            for m in ("April", "May", "June", "July")]
    ec.attach_release_values(rows, today=datetime.date(2026, 8, 20))
    assert calls["n"] == 1


def test_losing_the_numbers_does_not_lose_the_calendar(monkeypatch):
    """A calendar with no values is still a calendar; an exception is not."""
    def boom(*a, **k):
        raise RuntimeError("daily threshold reached")

    monkeypatch.setattr(ec, "_http", boom)
    rows = [_entry("cpi", "Consumer Price Index", "July 2026", datetime.date(2026, 8, 12))]
    out, warnings = ec.attach_release_values(rows, today=datetime.date(2026, 8, 20))

    assert out[0]["date"] == datetime.date(2026, 8, 12)
    assert out[0]["value_status"] == "unavailable"
    assert any("daily threshold" in w for w in warnings)


def test_an_empty_calendar_makes_no_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no request should be made for an empty calendar")

    monkeypatch.setattr(ec, "_http", boom)
    assert ec.attach_release_values([]) == ([], [])


# =====================================================================
# FOMC and BEA — parsed from real captured pages
# =====================================================================
# The fixtures are trimmed copies of the live federalreserve.gov and bea.gov
# pages. Pinning against the real markup is the point: both are scraped, so the
# failure that matters is a layout change, and a hand-written fixture would
# happily keep passing through one.

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def gov_pages(monkeypatch):
    def serve(url, *a, **k):
        if "federalreserve" in url:
            return _fixture("fomc_calendar.html")
        if "bea.gov" in url:
            return _fixture("bea_schedule.html")
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(ec, "_http", serve)


def test_fomc_meetings_parse_from_the_real_page(gov_pages):
    by_date = {m["date"]: m for m in ec.fetch_fomc_meetings()}

    # Announced 2026 schedule, verbatim from the Fed's calendar.
    for day in (datetime.date(2026, 1, 28), datetime.date(2026, 3, 18),
                datetime.date(2026, 4, 29), datetime.date(2026, 6, 17),
                datetime.date(2026, 7, 29), datetime.date(2026, 9, 16),
                datetime.date(2026, 10, 28), datetime.date(2026, 12, 9)):
        assert day in by_date, f"{day} missing from the parsed FOMC calendar"


def test_the_fomc_date_is_the_decision_day_not_the_first_day(gov_pages):
    """
    The statement lands on the final day. Sorting a calendar on the opening day
    puts the decision a day earlier than it happens.
    """
    march = [m for m in ec.fetch_fomc_meetings() if m["date"] == datetime.date(2026, 3, 18)][0]
    assert march["start_date"] == datetime.date(2026, 3, 17)
    assert march["reference_period"] == "Mar 17-18"


def test_projection_meetings_are_flagged(gov_pages):
    """The SEP meetings carry the dot plot and move rates markedly more."""
    meetings = {m["date"]: m for m in ec.fetch_fomc_meetings()}
    assert meetings[datetime.date(2026, 3, 18)]["projections"] is True
    assert meetings[datetime.date(2026, 4, 29)]["projections"] is False


def test_a_notation_vote_is_not_presented_as_a_scheduled_meeting(gov_pages):
    aug = [m for m in ec.fetch_fomc_meetings() if m["date"] == datetime.date(2025, 8, 22)]
    assert aug and aug[0]["unscheduled"] is True
    assert "notation" in aug[0]["note"].lower()


def test_the_statement_time_is_labelled_as_a_convention(gov_pages):
    """The Fed's page does not publish a time; 2:00 PM is our assertion."""
    assert "customary" in ec.fetch_fomc_meetings()[0]["time_et"]


def test_a_layout_change_raises_rather_than_returning_an_empty_calendar(monkeypatch):
    """An empty FOMC calendar reads as 'no meetings', which is never true."""
    monkeypatch.setattr(ec, "_http", lambda *a, **k: "<html><body>redesigned</body></html>")
    with pytest.raises(RuntimeError, match="layout has changed|year panels"):
        ec.fetch_fomc_meetings()


def test_bea_pce_and_gdp_parse_from_the_real_page(gov_pages):
    entries, skipped = ec.fetch_bea_schedule()
    slugs = {e["slug"] for e in entries}

    assert "bea_pce" in slugs, "PCE is the Fed's target measure and must be present"
    assert "bea_gdp" in slugs
    assert skipped > 0, "the count of untracked BEA releases should be reported"


def test_bea_dates_take_their_year_from_the_table_header(gov_pages):
    """The date cells carry no year at all -- only the header does."""
    entries, _ = ec.fetch_bea_schedule()
    pce = [e for e in entries if e["slug"] == "bea_pce"][0]
    assert pce["date"] == datetime.date(2026, 8, 26)
    assert pce["reference_period"] == "July 2026"


def test_regional_gdp_is_not_mistaken_for_the_national_print(gov_pages):
    """
    "GDP by County and Personal Income by County" is a regional statistic. A
    bare "GDP" prefix match pulls it onto the calendar as if it were the print.
    """
    entries, _ = ec.fetch_bea_schedule()
    assert not any("County" in e.get("full_title", "") for e in entries)


def test_the_three_gdp_vintages_are_distinguishable(gov_pages):
    """
    Advance, second and third estimates of one quarter are three separate
    events; without the vintage two rows read as duplicates of each other.
    """
    entries, _ = ec.fetch_bea_schedule()
    gdp = [e["reference_period"] for e in entries if e["slug"] == "bea_gdp"]
    assert len(set(gdp)) == len(gdp), f"indistinguishable GDP rows: {gdp}"
    assert any("Advance" in g for g in gdp)


def test_bea_layout_change_raises(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: "<html><body>redesigned</body></html>")
    with pytest.raises(RuntimeError, match="layout has changed"):
        ec.fetch_bea_schedule()


# =====================================================================
# One calendar
# =====================================================================

def test_the_calendar_merges_every_source_chronologically(monkeypatch):
    def serve(url, *a, **k):
        if "federalreserve" in url:
            return _fixture("fomc_calendar.html")
        if "bea.gov" in url:
            return _fixture("bea_schedule.html")
        if "bls.gov/schedule" in url:
            return SCHEDULE_HTML
        return json.dumps(JOIN_RESPONSE)

    monkeypatch.setattr(ec, "_http", serve)
    entries, _ = ec.economic_calendar(days_ahead=120, days_back=30,
                                      today=datetime.date(2026, 8, 20))

    assert {e["source"] for e in entries} >= {"BLS", "Federal Reserve", "BEA"}
    assert [e["date"] for e in entries] == sorted(e["date"] for e in entries)


def test_one_dead_source_does_not_empty_the_calendar(monkeypatch):
    """
    A Fed page redesign must not take CPI off the calendar with it, and the
    warning has to name the source so an empty week is not read as a quiet one.
    """
    def serve(url, *a, **k):
        if "federalreserve" in url or "bea.gov" in url:
            raise RuntimeError("connection refused")
        if "bls.gov/schedule" in url:
            return SCHEDULE_HTML
        return json.dumps(JOIN_RESPONSE)

    monkeypatch.setattr(ec, "_http", serve)
    entries, warnings = ec.economic_calendar(days_ahead=400, days_back=400,
                                             today=datetime.date(2026, 8, 20))

    assert any(e["source"] == "BLS" for e in entries)
    assert any("FOMC" in w for w in warnings)
    assert any("BEA" in w for w in warnings)


def test_the_calendar_can_be_narrowed_to_one_source(monkeypatch):
    def serve(url, *a, **k):
        if "bls.gov/schedule" in url:
            return SCHEDULE_HTML
        if "bls.gov" in url:
            return json.dumps(JOIN_RESPONSE)
        raise AssertionError(f"should not have fetched {url}")

    monkeypatch.setattr(ec, "_http", serve)
    entries, _ = ec.economic_calendar(days_ahead=400, days_back=400, sources=["bls"],
                                      today=datetime.date(2026, 8, 20))
    assert entries and all(e["source"] == "BLS" for e in entries)


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


# =====================================================================
# BLS key port
# =====================================================================

def test_key_validation_reports_the_unregistered_tier_when_no_key(monkeypatch):
    monkeypatch.setattr(ec, "BLS_API_KEY", "")
    r = ec.validate_bls_key(None)
    assert r["valid"] is False
    assert r["daily_cap"] == 25
    assert ec.BLS_REGISTRATION_URL in r["detail"]


def test_key_validation_accepts_a_working_key(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(
        {"status": "REQUEST_SUCCEEDED", "message": [], "Results": {"series": []}}))
    r = ec.validate_bls_key("a-real-looking-key")
    assert r["valid"] is True
    assert r["daily_cap"] == 500
    assert "v2" in r["tier"]


def test_key_validation_reports_a_rejected_key(monkeypatch):
    """A bad key does not error at the API -- it silently degrades. Catch it here."""
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(
        {"status": "REQUEST_NOT_PROCESSED", "message": ["invalid registration key"]}))
    r = ec.validate_bls_key("typo")
    assert r["valid"] is False
    assert "invalid registration key" in r["detail"]


def test_key_validation_survives_an_unreachable_endpoint(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(ec, "_http", boom)
    r = ec.validate_bls_key("k")
    assert r["valid"] is False and "network down" in r["detail"]


def test_registered_tier_requests_bls_side_calculations(monkeypatch):
    """v2 can compute its own percent changes; we ask for them as a cross-check."""
    captured = {}

    def capture(url, data=None, **k):
        captured["url"] = url
        captured["payload"] = json.loads(data.decode())
        return json.dumps(BLS_RESPONSE)

    monkeypatch.setattr(ec, "BLS_API_KEY", "key123")
    monkeypatch.setattr(ec, "_http", capture)
    ec.fetch_bls_series(["cpi"])

    assert captured["url"] == ec.BLS_V2
    assert captured["payload"]["registrationkey"] == "key123"
    assert captured["payload"]["calculations"] is True


def test_our_arithmetic_is_checked_against_the_bls_figure(monkeypatch):
    """When BLS supplies its own change and it disagrees with ours, say so."""
    payload = json.loads(json.dumps(BLS_RESPONSE))
    payload["Results"]["series"][0]["data"][0]["calculations"] = {
        "pct_changes": {"1": "99.0", "12": "99.0"}}
    monkeypatch.setattr(ec, "BLS_API_KEY", "key123")
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(payload))

    latest = ec.fetch_bls_series(["cpi"])["cpi"]["observations"][0]
    assert latest.get("warnings"), "a disagreement with the BLS figure must be reported"
    assert "disagrees with the BLS-computed" in latest["warnings"][0]


# =====================================================================
# XBRL fundamentals cross-check
# =====================================================================

CONCEPT = {
    "units": {"shares": [
        {"val": 1000000, "end": "2025-06-17", "form": "10-Q", "filed": "2025-06-25"},
        {"val": 1129393151, "end": "2026-06-17", "form": "10-Q", "filed": "2026-06-25"},
    ]}
}


def _stub_xbrl(monkeypatch, concept=CONCEPT):
    monkeypatch.setattr(ec, "SEC_USER_AGENT", "Test (t@e.com)")

    def router(url, *a, **k):
        if "company_tickers" in url:
            return json.dumps(TICKER_MAP)
        if "companyconcept" in url:
            if "EntityCommonStockSharesOutstanding" in url:
                return json.dumps(concept)
            raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                url, 404, "Not Found", None, None)
        raise AssertionError(f"unexpected {url}")

    monkeypatch.setattr(ec, "_http", router)


def test_financials_prefer_the_most_recently_filed_figure(monkeypatch):
    _stub_xbrl(monkeypatch)
    fact = ec.company_financials("MU")["facts"]["shares_outstanding"]
    assert fact["value"] == 1129393151
    assert fact["form"] == "10-Q" and fact["filed"] == "2026-06-25"


def test_cross_check_confirms_an_agreeing_source(monkeypatch):
    _stub_xbrl(monkeypatch)
    found = ec.cross_check_fundamentals("MU", {"shares_outstanding": 1129393151})
    assert len(found) == 1
    assert found[0]["agrees"] is True
    assert found[0]["divergence_pct"] == pytest.approx(0.0)


def test_cross_check_flags_a_disagreeing_source(monkeypatch):
    _stub_xbrl(monkeypatch)
    found = ec.cross_check_fundamentals("MU", {"shares_outstanding": 900_000_000})
    assert found[0]["agrees"] is False
    assert found[0]["divergence_pct"] > 15
    assert found[0]["form"] == "10-Q"


def test_cross_check_ignores_fields_with_nothing_to_compare(monkeypatch):
    _stub_xbrl(monkeypatch)
    assert ec.cross_check_fundamentals("MU", {"revenue": 123}) == []
    assert ec.cross_check_fundamentals("MU", {"shares_outstanding": None}) == []


# =====================================================================
# Transient upstream failures
# =====================================================================

def test_transient_5xx_is_retried(monkeypatch):
    """
    BLS, FRED and EDGAR all return an occasional 503. One attempt makes a sound
    tool look unreliable -- a live probe lost an entire economic calendar to one.
    """
    import urllib.error
    calls = {"n": 0}

    class FakeResp:
        headers = {}
        def read(self): return b'{"ok":true}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", None, None)
        return FakeResp()

    monkeypatch.setattr(ec.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)
    assert ec._http("https://example.gov/x") == '{"ok":true}'
    assert calls["n"] == 3


def test_client_errors_are_not_retried(monkeypatch):
    """A 404 is our mistake and will fail identically three times."""
    import urllib.error
    calls = {"n": 0}

    def not_found(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr(ec.urllib.request, "urlopen", not_found)
    with pytest.raises(urllib.error.HTTPError):
        ec._http("https://example.gov/missing")
    assert calls["n"] == 1


def test_retries_give_up_and_raise(monkeypatch):
    import urllib.error
    def always_down(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "down", None, None)
    monkeypatch.setattr(ec.urllib.request, "urlopen", always_down)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        ec._http("https://example.gov/x")


# =====================================================================
# Item 2.02 — which 8-K actually announced a quarter
# =====================================================================

def test_only_item_2_02_filings_count_as_an_earnings_release(monkeypatch):
    """
    A substring match on "2.02" also catches 12.02 and 2.021. The items field
    is a comma-separated list of exact codes and has to be read as one.
    """
    filings = [
        {"accession": "a", "items": "2.02,9.01"},
        {"accession": "b", "items": "5.02"},
        {"accession": "c", "items": "9.01, 2.02"},     # spaced
        {"accession": "d", "items": "12.02"},          # must not match
        {"accession": "e", "items": ""},
        {"accession": "f", "items": "2.021"},          # must not match
    ]
    monkeypatch.setattr(ec, "company_filings", lambda *a, **k: filings)
    got = [f["accession"] for f in ec.earnings_filings("AAPL")]
    assert got == ["a", "c"]


def test_earnings_filings_respects_the_limit(monkeypatch):
    monkeypatch.setattr(ec, "company_filings",
                        lambda *a, **k: [{"accession": str(i), "items": "2.02"}
                                         for i in range(20)])
    assert len(ec.earnings_filings("AAPL", limit=3)) == 3


# =====================================================================
# Earnings: the three states a report date can be in
# =====================================================================

class _FakeTicker:
    def __init__(self, calendar=None, dates=None):
        self.calendar = calendar or {}
        self.earnings_dates = dates


def _pending_frame(date_str):
    import pandas as pd
    return pd.DataFrame({"EPS Estimate": [1.0], "Reported EPS": [float("nan")],
                         "Surprise(%)": [float("nan")]},
                        index=pd.to_datetime([date_str]))


@pytest.fixture
def earnings_mod(monkeypatch):
    from dashboard import earnings as em

    def install(calendar, dates=None, filings=()):
        monkeypatch.setattr(em.wc, "yahoo_ticker", lambda s: _FakeTicker(calendar, dates))
        monkeypatch.setattr(em.ec, "earnings_filings", lambda s, limit=8: list(filings))
    return em, install


def test_agreeing_feeds_read_as_confirmed(earnings_mod):
    em, install = earnings_mod
    install({"Earnings Date": [datetime.date(2026, 10, 30)]}, _pending_frame("2026-10-30"))
    assert em.next_report("AAPL")["status"] == em.STATUS_CONFIRMED


def test_disagreeing_yahoo_feeds_read_as_disputed(earnings_mod):
    """
    Live AAPL: the calendar endpoint says 30 Oct, the earnings table says 29
    Oct. A date the provider cannot agree with itself on is not settled.
    """
    em, install = earnings_mod
    install({"Earnings Date": [datetime.date(2026, 10, 30)]}, _pending_frame("2026-10-29"))
    got = em.next_report("AAPL")
    assert got["status"] == em.STATUS_DISPUTED
    assert got["table_date"] == datetime.date(2026, 10, 29)


def test_a_window_reads_as_estimated(earnings_mod):
    em, install = earnings_mod
    install({"Earnings Date": [datetime.date(2026, 10, 28), datetime.date(2026, 11, 3)]})
    got = em.next_report("AAPL")
    assert got["status"] == em.STATUS_ESTIMATED
    assert got["window"] == [datetime.date(2026, 10, 28), datetime.date(2026, 11, 3)]


def test_no_date_is_a_status_not_an_exception(earnings_mod):
    em, install = earnings_mod
    install({})
    assert em.next_report("NOPE")["status"] == em.STATUS_UNKNOWN


def test_every_status_carries_an_explanation(earnings_mod):
    em, _ = earnings_mod
    for status in (em.STATUS_CONFIRMED, em.STATUS_DISPUTED,
                   em.STATUS_ESTIMATED, em.STATUS_UNKNOWN):
        assert em.STATUS_NOTE[status].strip()


def test_one_bad_symbol_does_not_empty_the_watchlist(monkeypatch):
    from dashboard import earnings as em

    def flaky(sym):
        if sym == "BAD":
            raise RuntimeError("no such ticker")
        return _FakeTicker({"Earnings Date": [datetime.date(2026, 10, 30)]},
                           _pending_frame("2026-10-30"))

    monkeypatch.setattr(em.wc, "yahoo_ticker", flaky)
    rows, problems = em.upcoming(["AAPL", "BAD"], today=datetime.date(2026, 10, 1))
    assert [r["symbol"] for r in rows] == ["AAPL"]
    assert any("BAD" in p for p in problems)


def test_dates_outside_the_horizon_are_excluded(monkeypatch):
    from dashboard import earnings as em
    monkeypatch.setattr(em.wc, "yahoo_ticker",
                        lambda s: _FakeTicker({"Earnings Date": [datetime.date(2027, 6, 1)]}))
    rows, _ = em.upcoming(["AAPL"], days_ahead=30, today=datetime.date(2026, 10, 1))
    assert rows == []


# =====================================================================
# Quarterly fundamentals from XBRL
# =====================================================================

def _xbrl(points):
    return {"units": {"USD": points}}


def _pt(start, end, val, filed="2026-01-01", form="10-Q"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form, "fp": "Q1"}


def test_year_to_date_facts_are_differenced_into_quarters(monkeypatch):
    """
    Cash-flow tags are filed cumulatively: Q1 is a 3-month fact, Q2 a 6-month,
    Q3 a 9-month. Filtering to quarter-length durations returned exactly one row
    per year -- Q1 -- with the "previous quarter" a year back, so a QoQ change
    was really a YoY one wearing the wrong label.
    """
    from dashboard import earnings as em
    ytd = _xbrl([
        _pt("2026-01-01", "2026-03-31", 100.0),      # Q1        -> 100
        _pt("2026-01-01", "2026-06-30", 250.0),      # H1        -> Q2 = 150
        _pt("2026-01-01", "2026-09-30", 420.0),      # 9M        -> Q3 = 170
    ])
    monkeypatch.setattr(em.ec, "ticker_to_cik", lambda s: {"cik": "0000000001"})
    monkeypatch.setattr(em.ec, "xbrl_concept", lambda cik, tax, tag: ytd)

    got = em.quarterly_metric("X", "Capex", limit=4)
    by_end = {r["period_end"]: r["value"] for r in got["rows"]}
    assert by_end["2026-03-31"] == pytest.approx(100.0)
    assert by_end["2026-06-30"] == pytest.approx(150.0)
    assert by_end["2026-09-30"] == pytest.approx(170.0)


def test_a_directly_filed_quarter_beats_a_derived_one(monkeypatch):
    from dashboard import earnings as em
    data = _xbrl([
        _pt("2026-01-01", "2026-03-31", 100.0),
        _pt("2026-01-01", "2026-06-30", 250.0),
        _pt("2026-04-01", "2026-06-30", 999.0),      # the company's own Q2 fact
    ])
    monkeypatch.setattr(em.ec, "ticker_to_cik", lambda s: {"cik": "1"})
    monkeypatch.setattr(em.ec, "xbrl_concept", lambda cik, tax, tag: data)
    rows = {r["period_end"]: r["value"] for r in em.quarterly_metric("X", "Capex")["rows"]}
    assert rows["2026-06-30"] == pytest.approx(999.0), "a filed quarter must win over 250-100"


def test_ytd_facts_are_never_differenced_across_a_fiscal_year(monkeypatch):
    """
    Subtracting last year's full-year total from this year's Q1 is arithmetic
    on unrelated periods. Grouping by fiscal-year start is what prevents it.
    """
    from dashboard import earnings as em
    data = _xbrl([
        _pt("2025-01-01", "2025-12-31", 1000.0),     # prior FY
        _pt("2026-01-01", "2026-03-31", 100.0),      # this FY Q1
    ])
    monkeypatch.setattr(em.ec, "ticker_to_cik", lambda s: {"cik": "1"})
    monkeypatch.setattr(em.ec, "xbrl_concept", lambda cik, tax, tag: data)
    rows = {r["period_end"]: r["value"] for r in em.quarterly_metric("X", "Capex")["rows"]}
    assert rows.get("2026-03-31") == pytest.approx(100.0)
    assert -900.0 not in rows.values()


def test_a_restated_figure_uses_the_most_recent_filing(monkeypatch):
    from dashboard import earnings as em
    data = _xbrl([
        _pt("2026-01-01", "2026-03-31", 100.0, filed="2026-04-30"),
        _pt("2026-01-01", "2026-03-31", 111.0, filed="2026-08-01"),   # restatement
    ])
    monkeypatch.setattr(em.ec, "ticker_to_cik", lambda s: {"cik": "1"})
    monkeypatch.setattr(em.ec, "xbrl_concept", lambda cik, tax, tag: data)
    rows = em.quarterly_metric("X", "Capex")["rows"]
    assert rows[0]["value"] == pytest.approx(111.0)


def test_free_cash_flow_is_labelled_as_derived():
    """It is not a tagged figure in the filing; saying so is the point."""
    from dashboard import earnings as em
    assert "derived" in em.METRICS["FCF"]["note"].lower()


def test_an_unknown_metric_is_refused():
    from dashboard import earnings as em
    with pytest.raises(ValueError, match="Unknown metric"):
        em.quarterly_metric("AAPL", "Ebitda")
