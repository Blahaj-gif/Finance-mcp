"""
Behavioural tests for the MCP tools.

These cover the defects that rode along with the bar-ordering bug: momentum and
relative-return signs, failures being scored as neutral rather than reported,
and the buying-power guardrail that a pricing failure could switch off.
"""
import datetime
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import finance_mcp as srv
from dashboard import webull_client as wc


def rising_frame(n=40, start=100.0, step=2.0):
    """Oldest-first, strictly rising. Any correct return measure must be positive."""
    end = datetime.datetime.utcnow()
    times = [end - datetime.timedelta(days=(n - 1 - i)) for i in range(n)]
    closes = [start + step * i for i in range(n)]
    return pd.DataFrame({
        "time": [t.strftime("%Y-%m-%d %H:%M:%S") for t in times],
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })


# =====================================================================
# Direction: momentum and returns must not be sign-inverted
# =====================================================================

def test_sector_heatmap_reports_positive_momentum_on_rising_prices(monkeypatch):
    """
    On the raw newest-first frames these returns came out negative for a rising
    market, so the table sorted the worst sectors to the top as "LEADER".
    """
    monkeypatch.setattr(wc, "fetch_data",
                        lambda sym, interval="D", count=200: (rising_frame(count), "Webull OpenAPI"))
    monkeypatch.setattr(srv.webull_client, "fetch_data", wc.fetch_data)

    out = srv.get_sector_heatmap()

    assert "LAGGARD" not in out, "a uniformly rising market must not produce laggards"
    assert "LEADER [BULL]" in out
    assert "Covering 11 of 11 sectors" in out

    # Every printed return percentage must be positive.
    import re
    pcts = re.findall(r"([+-]\d+\.\d+)%", out)
    assert pcts, "expected return percentages in the table"
    assert all(float(p) > 0 for p in pcts), f"negative returns on a rising series: {pcts}"


def test_compare_symbols_returns_are_positive_on_rising_prices(monkeypatch):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (rising_frame(count), "Webull OpenAPI"))

    out = srv.compare_symbols("AAA", "BBB", period_bars=30)

    assert "Return**: `+" in out, f"expected positive returns, got:\n{out}"
    assert "`-" not in out.split("Price Correlation")[0]


def test_ohlcv_last_row_is_the_newest_bar(monkeypatch):
    frame = rising_frame(20)
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (frame, "Webull OpenAPI"))

    out = srv.get_ohlcv("AAA", "D", 20)
    lines = [l for l in out.strip().splitlines() if l.strip()]

    # The newest bar's timestamp belongs on the final data row, and the oldest
    # on the first. Reversed ordering would swap them.
    assert frame["time"].iloc[-1] in lines[-1]
    assert frame["time"].iloc[0] not in lines[-1]


# =====================================================================
# Failures must be reported, not scored as neutral
# =====================================================================

def test_scan_watchlist_accepts_a_comma_string_and_a_list(monkeypatch):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (rising_frame(count), "Webull OpenAPI"))

    from_string = srv.scan_watchlist("AAA, BBB")
    from_list = srv.scan_watchlist(["AAA", "BBB"])   # used to raise AttributeError

    for out in (from_string, from_list):
        assert "AAA" in out and "BBB" in out


def test_scan_watchlist_excludes_failures_instead_of_scoring_them_zero(monkeypatch):
    def flaky(sym, interval="D", count=200):
        if sym == "BAD":
            raise RuntimeError("no data")
        return rising_frame(count), "Webull OpenAPI"

    monkeypatch.setattr(srv.webull_client, "fetch_data", flaky)
    out = srv.scan_watchlist("AAA, BAD")

    assert "could not be evaluated" in out
    assert "BAD" in out
    # A failed symbol must never appear as a scored row.
    assert "| BAD" not in out.split("could not be evaluated")[0]


def test_multi_timeframe_renormalises_when_a_timeframe_fails(monkeypatch):
    def flaky(sym, interval="D", count=200):
        if interval == "M15":
            raise RuntimeError("intraday unavailable")
        return rising_frame(count), "Webull OpenAPI"

    monkeypatch.setattr(srv.webull_client, "fetch_data", flaky)
    out = srv.get_multi_timeframe("AAA")

    assert "FAILED" in out
    assert "Partial coverage" in out
    assert "80%" in out          # D (0.5) + H1 (0.3) resolved, M15 (0.2) did not


def test_multi_timeframe_reports_when_nothing_resolves(monkeypatch):
    def dead(sym, interval="D", count=200):
        raise RuntimeError("feed down")

    monkeypatch.setattr(srv.webull_client, "fetch_data", dead)
    out = srv.get_multi_timeframe("AAA")

    assert "No timeframe could be evaluated" in out
    assert "Confluence Verdict" not in out, "must not print a verdict with zero coverage"


def test_sector_heatmap_names_the_sectors_it_dropped(monkeypatch):
    def flaky(sym, interval="D", count=200):
        if sym == "XLE":
            raise RuntimeError("no data")
        return rising_frame(count), "Webull OpenAPI"

    monkeypatch.setattr(srv.webull_client, "fetch_data", flaky)
    out = srv.get_sector_heatmap()

    assert "Covering 10 of 11 sectors" in out
    assert "XLE" in out.split("excluded from this ranking")[1]


# =====================================================================
# Fallback banner reaches the tools that used to discard the source string
# =====================================================================

@pytest.mark.parametrize("call", [
    lambda: srv.get_ohlcv("AAA", "D", 20),
    lambda: srv.scan_watchlist("AAA"),
    lambda: srv.get_multi_timeframe("AAA"),
    lambda: srv.compare_symbols("AAA", "BBB", period_bars=30),
    lambda: srv.get_sector_heatmap(),
])
def test_yahoo_fallback_is_announced(monkeypatch, call):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (rising_frame(count), "Yahoo Finance (Fallback)"))
    assert "Yahoo Finance (Fallback)" in call()


def test_fallback_banner_is_not_duplicated_by_cache_hits(monkeypatch):
    """A cached hit relabels the source; the banner must not print twice for it."""
    seen = {"n": 0}

    def alternating(sym, interval="D", count=200):
        seen["n"] += 1
        label = "Yahoo Finance (Fallback)" if seen["n"] % 2 else "Yahoo Finance (Fallback) (Cached)"
        return rising_frame(count), label

    monkeypatch.setattr(srv.webull_client, "fetch_data", alternating)
    out = srv.get_multi_timeframe("AAA")
    assert out.lower().count("the primary webull feed did not serve") == 1


# =====================================================================
# News schema tolerance
# =====================================================================

def test_get_news_survives_null_fields(monkeypatch):
    """
    Yahoo sends explicit nulls ("clickThroughUrl": null). `.get(k, default)`
    returns None in that case, not the default, which crashed the tool with
    'NoneType' object has no attribute 'get'.
    """
    import yfinance as yf

    class FakeTicker:
        def __init__(self, sym): pass
        news = [
            {"content": {"title": "Real headline", "provider": None,
                         "clickThroughUrl": None, "pubDate": "2026-08-05T19:56:01Z"}},
            {"content": None},
            {"content": {"title": None, "provider": {"displayName": "Reuters"},
                         "canonicalUrl": {"url": "https://example.com"},
                         "providerPublishTime": 0}},
        ]

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    out = srv.get_news("MU")

    assert "Error fetching news" not in out
    assert "Real headline" in out
    assert "Reuters" in out
    assert "https://example.com" in out
    assert "2026-08-05 19:56:01" in out


def test_get_news_handles_no_articles(monkeypatch):
    import yfinance as yf

    class EmptyTicker:
        def __init__(self, sym): pass
        news = []

    monkeypatch.setattr(yf, "Ticker", EmptyTicker)
    assert "No recent news" in srv.get_news("MU")


# =====================================================================
# Account payload parsing (real API shapes)
# =====================================================================

# Shape confirmed against the live Webull TH API.
BALANCE_PAYLOAD = {
    "total_asset_currency": "THB",
    "total_cash_balance": "10987.68",
    "account_currency_assets": [
        {"currency": "HKD", "cash_balance": "0.00", "buying_power": "0.00"},
        {"currency": "USD", "cash_balance": "333.83", "buying_power": "333.83"},
        {"currency": "THB", "cash_balance": "0.00", "buying_power": "0.00"},
    ],
}

POSITION_PAYLOAD = [
    {"symbol": "MU", "quantity": "0.30000", "currency": "USD", "cost_price": "908.69"},
    {"symbol": "RKLB", "quantity": "2.0000000000", "currency": "USD", "cost_price": "84.80"},
]


def test_buying_power_read_from_the_matching_currency_line():
    """
    Buying power is per-currency. The old code read a top-level `buyingPower`
    key that does not exist in this API, so it always saw zero.
    """
    assert wc.get_buying_power(BALANCE_PAYLOAD, "USD") == pytest.approx(333.83)
    assert wc.get_buying_power(BALANCE_PAYLOAD, "HKD") == pytest.approx(0.0)


def test_buying_power_raises_for_a_currency_that_is_absent():
    with pytest.raises(ValueError, match="No JPY buying power"):
        wc.get_buying_power(BALANCE_PAYLOAD, "JPY")


def test_position_quantity_lookup():
    assert wc.get_position_quantity(POSITION_PAYLOAD, "MU") == pytest.approx(0.3)
    assert wc.get_position_quantity(POSITION_PAYLOAD, "rklb") == pytest.approx(2.0)
    assert wc.get_position_quantity(POSITION_PAYLOAD, "NVDA") == 0.0


def test_primary_account_id_extracted_from_account_list():
    class FakeAccountV2:
        def get_account_list(self):
            return [{"account_id": "1208285034570579968", "account_type": "CASH"}]

    class FakeTradeClient:
        account_v2 = FakeAccountV2()

    assert wc.get_primary_account_id(FakeTradeClient()) == "1208285034570579968"


def test_primary_account_id_raises_when_no_accounts():
    class FakeAccountV2:
        def get_account_list(self):
            return []

    class FakeTradeClient:
        account_v2 = FakeAccountV2()

    with pytest.raises(RuntimeError, match="no accounts"):
        wc.get_primary_account_id(FakeTradeClient())


# =====================================================================
# Trading guardrail
# =====================================================================

def test_draft_order_blocks_when_price_cannot_be_determined(monkeypatch, tmp_path):
    """
    A bare `except: est_price = 0` combined with an `est_price > 0` guard meant a
    yfinance hiccup disabled the buying-power check entirely.
    """
    import webull.trade.trade_client as tc

    class FakeAccount:
        def get_account_list(self):
            return [{"account_id": "ACC1"}]

        def get_account_balance(self, account_id):
            return BALANCE_PAYLOAD

    class FakeTradeClient:
        def __init__(self, api_client):
            self.account_v2 = FakeAccount()

    class BrokenTicker:
        def __init__(self, sym): pass
        @property
        def fast_info(self):
            raise RuntimeError("yfinance unavailable")

    import yfinance as yf
    monkeypatch.setattr(wc, "get_api_client", lambda: object())
    monkeypatch.setattr(srv.webull_client, "get_api_client", lambda: object())
    monkeypatch.setattr(tc, "TradeClient", FakeTradeClient)
    monkeypatch.setattr(yf, "Ticker", BrokenTicker)
    monkeypatch.setattr(srv, "BASE_DIR", str(tmp_path))
    os.makedirs(tmp_path / "dashboard", exist_ok=True)

    out = srv.draft_order("AAA", "BUY", quantity=10, order_type="MKT", limit_price=None)

    assert "SAFETY BLOCK" in out
    assert "ORDER DRAFTED" not in out
    assert not os.path.exists(tmp_path / "dashboard" / "order_drafts.json"), \
        "an unpriceable order must not be persisted"


# =====================================================================
# Freshness: a reader must be able to tell a live number from a stale one
# =====================================================================

def test_price_responses_stamp_the_bar_they_quote(monkeypatch):
    """
    The staleness gate refuses old bars, but refusing is only half the job — a
    reader still cannot tell a current number from a merely acceptable one and
    will quote both with the same confidence.
    """
    frame = rising_frame(60)
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (frame, "Webull OpenAPI"))

    for out in (srv.get_market_analysis("AAA"),
                srv.get_ohlcv("AAA", "D", 20),
                srv.get_technical_indicators("AAA")):
        assert "Latest D bar:" in out
        assert "source: Webull OpenAPI" in out
        assert frame["time"].iloc[-1][:10] in out


def test_freshness_line_names_the_session_age():
    import datetime
    from dashboard import market_calendar as mc
    latest = mc.previous_trading_day(datetime.date.today() + datetime.timedelta(days=1))
    fresh = rising_frame(10)
    fresh.loc[fresh.index[-1], "time"] = f"{latest} 21:00:00"
    line = wc.freshness_line(fresh, "Webull OpenAPI", "D")
    assert "current session" in line

    stale = fresh.copy()
    older = mc.previous_trading_day(mc.previous_trading_day(latest))
    stale.loc[stale.index[-1], "time"] = f"{older} 21:00:00"
    assert "trading session" in wc.freshness_line(stale, "Webull OpenAPI", "D")


def test_freshness_line_flags_a_cache_hit():
    assert "from 60s cache" in wc.freshness_line(rising_frame(5), "Webull OpenAPI (Cached)", "D")


# =====================================================================
# The composite verdict is opt-in
# =====================================================================

def test_no_buy_sell_score_by_default(monkeypatch):
    """
    An unvalidated score anchors judgment even when labelled unvalidated, so
    the default is to report what the indicators measure and stop there.
    """
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (rising_frame(120), "Webull OpenAPI"))
    out = srv.get_market_analysis("AAA")

    assert "Composite Verdict" not in out
    assert "STRONG BUY" not in out and "STRONG SELL" not in out
    assert "Score:" not in out
    assert "Indicator Readings" in out
    assert "include_verdict=true" in out          # discoverable, not hidden


def test_verdict_appears_only_when_requested(monkeypatch):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (rising_frame(120), "Webull OpenAPI"))
    out = srv.get_market_analysis("AAA", include_verdict=True)

    assert "Composite Verdict" in out
    assert "Score:" in out
    assert "heuristic" in out.lower()


def test_indicator_readings_stay_descriptive_without_the_verdict(monkeypatch):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (rising_frame(120), "Webull OpenAPI"))
    out = srv.get_market_analysis("AAA")

    # The measured reading survives; the instruction attached to it does not.
    assert "RSI (14)" in out
    for directive in ("**BUY**", "**SELL**", "**NEUTRAL**"):
        assert directive not in out
