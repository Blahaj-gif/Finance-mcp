"""
Paper vs live, and telling a throttled feed from a dead symbol.

Two things here decide whether someone loses money by accident: which broker
surface a submit button is pointed at, and whether "no data" means the ticker
is wrong or that we are being rate-limited.
"""
import datetime
import os
import re
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import webull_client as wc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# =====================================================================
# Sandbox / paper environment
# =====================================================================

def test_the_default_environment_is_live_not_paper(monkeypatch):
    """
    Deliberately the opposite of Webull's own MCP server, which defaults to the
    sandbox. This project reads a real account for everything else, so a silent
    switch to simulated balances would be the more dangerous surprise.
    """
    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", "prod")
    assert wc.is_paper_environment() is False
    assert wc.environment_label() == "LIVE"


@pytest.mark.parametrize("value", ["uat", "UAT", "sandbox", "paper", " Simulated "])
def test_every_paper_alias_is_recognised(monkeypatch, value):
    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", value)
    assert wc.is_paper_environment() is True
    assert wc.environment_label() == "PAPER"


@pytest.mark.parametrize("value", ["prod", "production", "live", ""])
def test_anything_else_is_treated_as_live(monkeypatch, value):
    """An unrecognised value must not quietly become paper -- or vice versa."""
    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", value)
    assert wc.is_paper_environment() is False


def test_every_sdk_region_has_a_sandbox_endpoint():
    """
    The SDK ships production hosts for twelve regions. If we offer paper mode
    at all, it has to cover the same set, or a user in an uncovered region gets
    a confusing failure instead of a clear one.
    """
    import json
    from webull.core.endpoint.local_config_regional_endpoint_resolver import ENDPOINT_JSON
    prod_regions = set(json.load(open(ENDPOINT_JSON))["region_mapping"])
    assert prod_regions <= set(wc.SANDBOX_ENDPOINTS), (
        f"no sandbox host for {sorted(prod_regions - set(wc.SANDBOX_ENDPOINTS))}")


def test_every_sandbox_entry_defines_all_three_api_types():
    for region, cfg in wc.SANDBOX_ENDPOINTS.items():
        assert set(cfg) == {"api", "quotes-api", "events-api"}, region
        for key, host in cfg.items():
            assert host and "." in host, f"{region}.{key} is not a hostname"


def test_no_sandbox_host_is_a_production_host():
    """
    The whole point is that a paper order never reaches production. A copy-paste
    slip here would route simulated orders to the live broker.
    """
    import json
    from webull.core.endpoint.local_config_regional_endpoint_resolver import ENDPOINT_JSON
    prod = json.load(open(ENDPOINT_JSON))["region_mapping"]
    prod_hosts = {h for cfg in prod.values() for h in cfg.values()}
    for region, cfg in wc.SANDBOX_ENDPOINTS.items():
        for key, host in cfg.items():
            assert host not in prod_hosts, f"{region}.{key} points at production: {host}"
            assert ("sandbox" in host or "uat" in host), \
                f"{region}.{key} is neither a sandbox nor a uat host: {host}"


def test_the_broken_uat_import_is_gone():
    """
    This branch used to `import webull_openapi_mcp`, a package the project does
    not depend on and does not ship, so WEBULL_ENVIRONMENT=uat raised
    ModuleNotFoundError at client construction -- a documented setting that
    broke the app instead of switching it to paper.
    """
    src = open(os.path.join(ROOT, "dashboard", "webull_client.py"), encoding="utf-8").read()
    # The comment credits upstream by path, which is fine; an import is not.
    assert not re.search(r"^\s*(from|import)\s+webull_openapi_mcp", src, re.M)
    assert "SANDBOX_ENDPOINTS" in src, "the endpoints must be defined locally"


def test_an_unknown_region_in_paper_mode_refuses_rather_than_falling_through(monkeypatch):
    """Falling back to production while the user believes they are on paper is
    the one outcome that must never happen."""
    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", "paper")
    monkeypatch.setattr(wc, "WEBULL_REGION_ID", "atlantis")
    monkeypatch.setattr(wc, "_API_CLIENT", None)
    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "k")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "s")
    with pytest.raises(RuntimeError, match="no sandbox endpoint"):
        wc.get_api_client()


# =====================================================================
# Throttled vs delisted
# =====================================================================

@pytest.fixture(autouse=True)
def clear_canary():
    wc._canary_state.update(checked_at=0.0, alive=None)
    yield
    wc._canary_state.update(checked_at=0.0, alive=None)


def test_an_empty_result_with_a_healthy_canary_is_a_bad_symbol(monkeypatch):
    monkeypatch.setattr(wc, "_canary_is_alive", lambda: True)
    err = wc._explain_empty_yahoo_result("NOPE")
    assert isinstance(err, wc.SymbolNotFoundError)
    assert "NOPE" in str(err) and "delisted" in str(err)


def test_an_empty_result_with_a_dead_canary_is_throttling(monkeypatch):
    """
    Yahoo answers a burst with 200 and an empty body as often as with 429. Read
    literally that says "this symbol does not exist", which sends the caller off
    to check a ticker that was never the problem.
    """
    monkeypatch.setattr(wc, "_canary_is_alive", lambda: False)
    err = wc._explain_empty_yahoo_result("AAPL")
    assert isinstance(err, wc.YahooThrottledError)
    assert "rate-limiting" in str(err)


def test_the_two_failures_are_different_exception_types():
    """Callers have to be able to branch on this, not grep a message."""
    assert not issubclass(wc.YahooThrottledError, wc.SymbolNotFoundError)
    assert not issubclass(wc.SymbolNotFoundError, wc.YahooThrottledError)
    assert issubclass(wc.SymbolNotFoundError, ValueError)


def test_the_canary_result_is_cached(monkeypatch):
    """A watchlist sweep of dead tickers must not fire one probe per name."""
    calls = {"n": 0}

    class Probe:
        def history(self, **kw):
            calls["n"] += 1
            return pd.DataFrame({"close": [1.0]})

    monkeypatch.setattr(wc, "yahoo_ticker", lambda s: Probe())
    for _ in range(5):
        assert wc._canary_is_alive() is True
    assert calls["n"] == 1


def test_a_canary_that_raises_counts_as_dead(monkeypatch):
    def boom(_):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(wc, "yahoo_ticker", boom)
    assert wc._canary_is_alive() is False


# =====================================================================
# Observed feed delay
# =====================================================================

class _MetaTicker:
    def __init__(self, meta):
        self.history_metadata = meta


def test_feed_delay_is_measured_from_yahoos_own_last_print(monkeypatch):
    """
    Yahoo does not publish exchangeDataDelayedBy on the chart endpoint, so the
    lag is measured rather than asserted: its last regular-session print
    against now.
    """
    five_min_ago = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
    monkeypatch.setattr(wc, "yahoo_ticker", lambda s: _MetaTicker({
        "regularMarketTime": int(five_min_ago.replace(
            tzinfo=datetime.timezone.utc).timestamp()),
        "fullExchangeName": "NasdaqGS",
        "exchangeTimezoneName": "America/New_York",
        "currency": "USD",
    }))
    d = wc.yahoo_feed_delay("AAPL")
    assert 4.0 <= d["observed_lag_minutes"] <= 6.0
    assert d["exchange"] == "NasdaqGS"
    assert d["market_open"] is True


def test_a_long_lag_is_reported_as_a_closed_market_not_a_delay(monkeypatch):
    """Overnight the gap is the session, not the feed. Saying otherwise would
    read as a 20-hour data delay."""
    yesterday = datetime.datetime.utcnow() - datetime.timedelta(hours=20)
    monkeypatch.setattr(wc, "yahoo_ticker", lambda s: _MetaTicker({
        "regularMarketTime": int(yesterday.replace(
            tzinfo=datetime.timezone.utc).timestamp())}))
    d = wc.yahoo_feed_delay("AAPL")
    assert d["market_open"] is False
    assert d["observed_lag_minutes"] > 1000


def test_missing_metadata_returns_nothing_rather_than_a_guess(monkeypatch):
    monkeypatch.setattr(wc, "yahoo_ticker", lambda s: _MetaTicker({}))
    assert wc.yahoo_feed_delay("AAPL") == {}

    def boom(_):
        raise RuntimeError("no metadata")
    monkeypatch.setattr(wc, "yahoo_ticker", boom)
    assert wc.yahoo_feed_delay("AAPL") == {}
