"""
Central bank and federal series tests.

Offline: every fetch is stubbed. These pin the two things that decide whether a
macro number can be trusted — that each source's format parses correctly, and
that a series which has quietly stopped publishing is caught.
"""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import central_banks as cb
from dashboard import econ_calendar as ec


@pytest.fixture(autouse=True)
def clean_cache():
    ec.CACHE.clear()
    yield
    ec.CACHE.clear()


def days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


# =====================================================================
# Per-source parsing
# =====================================================================

def test_fred_csv_is_parsed_newest_first(monkeypatch):
    csv_text = ("observation_date,DGS10\n"
                "2026-08-04,4.61\n2026-08-05,4.63\n2026-08-06,4.69\n")
    monkeypatch.setattr(cb, "FRED_API_KEY", "")
    monkeypatch.setattr(ec, "_http", lambda *a, **k: csv_text)

    rows = cb._fred_series("DGS10")
    assert rows[0] == {"date": "2026-08-06", "value": 4.69}
    assert rows[-1]["date"] == "2026-08-04"


def test_fred_skips_missing_observations(monkeypatch):
    """FRED writes '.' for a non-publication day; that is not a zero."""
    monkeypatch.setattr(cb, "FRED_API_KEY", "")
    monkeypatch.setattr(ec, "_http", lambda *a, **k:
                        "observation_date,DGS10\n2026-08-05,.\n2026-08-06,4.69\n")
    rows = cb._fred_series("DGS10")
    assert len(rows) == 1 and rows[0]["value"] == 4.69


def test_fred_uses_the_json_api_when_a_key_is_present(monkeypatch):
    seen = {}

    def capture(url, *a, **k):
        seen["url"] = url
        return json.dumps({"observations": [{"date": "2026-08-06", "value": "4.69"}]})

    monkeypatch.setattr(cb, "FRED_API_KEY", "abc123")
    monkeypatch.setattr(ec, "_http", capture)
    rows = cb._fred_series("DGS10")

    assert "api.stlouisfed.org" in seen["url"]
    assert "api_key=abc123" in seen["url"]
    assert rows[0]["value"] == 4.69


def test_fred_surfaces_an_api_error(monkeypatch):
    monkeypatch.setattr(cb, "FRED_API_KEY", "bad")
    monkeypatch.setattr(ec, "_http", lambda *a, **k:
                        json.dumps({"error_message": "Bad Request. Invalid api_key"}))
    with pytest.raises(RuntimeError, match="Invalid api_key"):
        cb._fred_series("DGS10")


def test_ecb_sdmx_is_parsed(monkeypatch):
    payload = {
        "dataSets": [{"series": {"0:0:0": {"observations": {
            "0": [2.15], "1": [2.25], "2": [2.4]}}}}],
        "structure": {"dimensions": {"observation": [
            {"values": [{"id": "2026-06-01"}, {"id": "2026-07-01"}, {"id": "2026-08-01"}]}]}},
    }
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(payload))
    rows = cb._ecb_series("FM/D.U2.EUR.4F.KR.DFR.LEV")

    assert rows[0]["date"] == "2026-08-01" and rows[0]["value"] == 2.4
    assert len(rows) == 3


def test_ecb_reports_an_empty_result(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps({"dataSets": [{"series": {}}]}))
    with pytest.raises(RuntimeError, match="no series"):
        cb._ecb_series("BAD/KEY")


def test_boe_csv_dates_are_normalised(monkeypatch):
    monkeypatch.setattr(ec, "_http", lambda *a, **k:
                        "DATE,IUDBEDR\n02 Jan 2026,3.75\n05 Jan 2026,3.75\n06 Feb 2026,4.00\n")
    rows = cb._boe_series("IUDBEDR")
    assert rows[0]["date"] == "2026-02-06"      # newest first, ISO
    assert rows[0]["value"] == 4.00


# =====================================================================
# Cadence-based staleness
# =====================================================================

def test_cadence_is_measured_from_the_data():
    daily = [{"date": days_ago(i)} for i in range(0, 10)]
    monthly = [{"date": days_ago(i * 30)} for i in range(0, 10)]
    assert cb._observed_cadence_days(daily) == 1
    assert 28 <= cb._observed_cadence_days(monthly) <= 31


def test_a_monthly_series_is_not_stale_at_two_months(monkeypatch):
    """
    A fixed tolerance either cries wolf on monthly data or misses a dead daily
    series. The BoJ call rate is monthly and routinely two months behind.
    """
    rows = [{"date": days_ago(60 + i * 30), "value": 0.8} for i in range(6)]
    monkeypatch.setattr(cb, "_fred_series", lambda sid, n=260: rows)
    d = cb.fetch_series(["boj_call_rate"])["boj_call_rate"]
    assert d["stale"] is False
    assert d["cadence_days"] in range(28, 32)


def test_a_discontinued_series_is_caught(monkeypatch):
    """FRED still serves BOERUKM, retired in 2017, with nothing to say so."""
    rows = [{"date": f"2017-0{i}-01", "value": 0.25} for i in (5, 4, 3, 2, 1)]
    monkeypatch.setattr(cb, "_fred_series", lambda sid, n=260: rows)
    d = cb.fetch_series(["us_10y"])["us_10y"]
    assert d["stale"] is True
    assert d["age_days"] > 3000


def test_a_daily_series_two_days_old_is_fresh(monkeypatch):
    rows = [{"date": days_ago(i + 2), "value": 4.6} for i in range(10)]
    monkeypatch.setattr(cb, "_fred_series", lambda sid, n=260: rows)
    assert cb.fetch_series(["us_10y"])["us_10y"]["stale"] is False


def test_a_dead_daily_series_is_caught(monkeypatch):
    rows = [{"date": days_ago(90 + i), "value": 4.6} for i in range(10)]
    monkeypatch.setattr(cb, "_fred_series", lambda sid, n=260: rows)
    assert cb.fetch_series(["us_10y"])["us_10y"]["stale"] is True


# =====================================================================
# Aggregation and failure handling
# =====================================================================

def test_one_failing_source_does_not_sink_the_others(monkeypatch):
    good = [{"date": days_ago(i), "value": 4.6 + i * 0.01} for i in range(5)]

    def boom(*a, **k):
        raise RuntimeError("ECB unreachable")

    monkeypatch.setattr(cb, "_fred_series", lambda sid, n=260: good)
    monkeypatch.setattr(cb, "_ecb_series", boom)

    out = cb.fetch_series(["us_10y", "ecb_deposit"])
    assert out["us_10y"]["latest"]["value"] == pytest.approx(4.6)
    assert "ECB unreachable" in out["ecb_deposit"]["error"]


def test_change_is_measured_against_the_previous_observation(monkeypatch):
    rows = [{"date": days_ago(0), "value": 4.69}, {"date": days_ago(1), "value": 4.63}]
    monkeypatch.setattr(cb, "_fred_series", lambda sid, n=260: rows)
    assert cb.fetch_series(["us_10y"])["us_10y"]["change"] == pytest.approx(0.06)


def test_unknown_series_is_rejected():
    with pytest.raises(ValueError, match="No known series"):
        cb.fetch_series(["not_a_real_series"])


def test_every_catalogued_series_names_a_real_fetcher():
    for key, (source, sid, label, unit) in cb.SERIES.items():
        assert source in ("fred", "ecb", "boe"), f"{key} has no fetcher for '{source}'"
        assert sid and label and unit


# =====================================================================
# BEA key handling
# =====================================================================

def test_bea_requires_a_key(monkeypatch):
    monkeypatch.setattr(cb, "BEA_API_KEY", "")
    with pytest.raises(RuntimeError, match="BEA_API_KEY"):
        cb.bea_dataset()


def test_bea_empty_body_is_explained(monkeypatch):
    """BEA answers a bad key with an empty body rather than an error."""
    monkeypatch.setattr(cb, "BEA_API_KEY", "k")
    monkeypatch.setattr(ec, "_http", lambda *a, **k: "")
    with pytest.raises(RuntimeError, match="empty response"):
        cb.bea_dataset()


def test_bea_parses_a_nipa_table(monkeypatch):
    payload = {"BEAAPI": {"Results": {"Data": [
        {"LineDescription": "Gross domestic product", "TimePeriod": "2026Q1",
         "DataValue": "23,456.7", "CL_UNIT": "Level"}]}}}
    monkeypatch.setattr(cb, "BEA_API_KEY", "k")
    monkeypatch.setattr(ec, "_http", lambda *a, **k: json.dumps(payload))
    out = cb.bea_dataset()
    assert out["rows"][0]["value"] == pytest.approx(23456.7)


def test_source_status_reports_key_requirements():
    status = cb.source_status()
    assert set(status) == {"fred", "ecb", "boe", "boj", "bea"}
    assert status["ecb"]["tier"] == "keyless"
    assert "No public API" in status["boj"]["note"]
