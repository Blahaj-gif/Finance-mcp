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
from dashboard import broker


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
    assert broker.get_buying_power(BALANCE_PAYLOAD, "USD") == pytest.approx(333.83)
    assert broker.get_buying_power(BALANCE_PAYLOAD, "HKD") == pytest.approx(0.0)


def test_buying_power_raises_for_a_currency_that_is_absent():
    with pytest.raises(ValueError, match="No JPY buying power"):
        broker.get_buying_power(BALANCE_PAYLOAD, "JPY")


def test_position_quantity_lookup():
    assert broker.get_position_quantity(POSITION_PAYLOAD, "MU") == pytest.approx(0.3)
    assert broker.get_position_quantity(POSITION_PAYLOAD, "rklb") == pytest.approx(2.0)
    assert broker.get_position_quantity(POSITION_PAYLOAD, "NVDA") == 0.0


def test_primary_account_id_extracted_from_account_list():
    class FakeAccountV2:
        def get_account_list(self):
            return [{"account_id": "1208285034570579968", "account_type": "CASH"}]

    class FakeTradeClient:
        account_v2 = FakeAccountV2()

    assert broker.get_primary_account_id(FakeTradeClient()) == "1208285034570579968"


def test_primary_account_id_raises_when_no_accounts():
    class FakeAccountV2:
        def get_account_list(self):
            return []

    class FakeTradeClient:
        account_v2 = FakeAccountV2()

    with pytest.raises(RuntimeError, match="no accounts"):
        broker.get_primary_account_id(FakeTradeClient())


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


# =====================================================================
# Every price-bearing answer states when it is as of
# =====================================================================
# From a reviewer, after a night when everything happened to return live:
# "Thursday's silent staleness is the failure mode I can't detect from the
# output alone." That is the original bug's signature exactly -- the feed was
# reachable, the numbers looked authoritative, and nothing in the response said
# which bar they came from. The staleness gate refuses old bars, but a refusal
# is invisible in an answer that succeeded; only a stamp on the output lets a
# reader tell a live number from a merely acceptable one.

def _tool_bodies():
    """(name, source) for every registered tool, split on the decorator."""
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "finance_mcp.py"), encoding="utf-8").read()
    out = []
    for chunk in re.split(r"\n@mcp\.tool\(\)\n", src)[1:]:
        m = re.match(r"def (\w+)", chunk)
        if m:
            out.append((m.group(1), chunk.split("\n@mcp.tool()")[0]))
    return out


def test_every_tool_that_reads_prices_stamps_when_they_are_as_of():
    """
    The check the reviewer's request reduces to. Any tool calling fetch_data is
    quoting a bar, and must say which one -- via freshness_line for a single
    series, freshness_summary for a sweep, or bar_age for a per-row column.
    """
    stampers = ("freshness_line", "freshness_summary", "bar_age")
    unstamped = [name for name, body in _tool_bodies()
                 if "fetch_data(" in body and not any(s in body for s in stampers)]
    assert not unstamped, (
        f"{len(unstamped)} price tools return no as-of stamp: {unstamped}. "
        "A reader cannot distinguish live data from stale data in the output alone.")


def test_multi_symbol_sweeps_stamp_each_row_not_just_the_header():
    """
    A single header line is not enough where several series are compared: one
    symbol that quietly stopped updating disappears among the fresh ones, and
    the ranking is then across different days.
    """
    sweeps = {"scan_watchlist", "get_sector_heatmap", "get_multi_timeframe",
              "compare_symbols", "get_portfolio_risk"}
    bodies = dict(_tool_bodies())
    for name in sweeps:
        assert name in bodies, f"{name} is no longer a registered tool"
        assert "bar_age(" in bodies[name], f"{name} has no per-row as-of"


def test_the_sweep_summary_quotes_the_stalest_member():
    """An average or a first-row stamp would let one stale name hide in twenty."""
    import pandas as pd
    fresh = pd.DataFrame({"time": ["2026-08-07 00:00:00"], "close": [1.0]})
    stale = pd.DataFrame({"time": ["2026-07-01 00:00:00"], "close": [1.0]})
    ages = [srv.webull_client.bar_age(fresh, "D"), srv.webull_client.bar_age(stale, "D")]

    line = srv.webull_client.freshness_summary(ages, "D", "symbols")
    assert "2026-07-01" in line, "the summary must quote the stalest bar"
    assert "stalest" in line and "freshest" in line


def test_a_sweep_with_no_dated_bars_says_so_rather_than_omitting_the_line():
    line = srv.webull_client.freshness_summary([], "D", "symbols")
    assert "no dated bars" in line


def test_check_connection_reports_freshness_not_merely_reachability():
    """
    The feed was reachable throughout the original staleness bug. "Connected"
    on its own answers a question nobody needed answered.
    """
    body = dict(_tool_bodies())["check_connection"]
    assert "bar_age(" in body
    assert "REACHABLE BUT" in body, "a stale-but-connected result must say so"


# =====================================================================
# Earnings: a date you cannot tell apart from a guess is not a date
# =====================================================================

class _FakeTicker:
    """Stands in for a yfinance Ticker with only what get_earnings reads."""

    def __init__(self, calendar=None, dates=None):
        self.calendar = calendar or {}
        self.earnings_dates = dates


def _dates_frame(rows):
    """rows: [(date_str, estimate, reported_or_None)] newest-first, as yfinance gives."""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({
        "EPS Estimate": [r[1] for r in rows],
        "Reported EPS": [r[2] for r in rows],
        # yfinance's own column is ALREADY a percentage, not a fraction.
        "Surprise(%)": [None if r[2] is None else (r[2] - r[1]) / abs(r[1]) * 100
                        for r in rows],
    }, index=idx)


@pytest.fixture
def earnings_stub(monkeypatch):
    def install(calendar, rows, filings=()):
        monkeypatch.setattr(srv.webull_client, "yahoo_ticker",
                            lambda s: _FakeTicker(calendar, _dates_frame(rows)))
        monkeypatch.setattr(srv.econ_calendar, "earnings_filings",
                            lambda s, limit=6: list(filings))
    return install


def test_an_estimated_window_is_never_rendered_as_a_date(earnings_stub):
    """
    Yahoo publishes an unset date as a window. Printing only its first day is
    a claim the source never made, and it is wrong by up to a week.
    """
    earnings_stub({"Earnings Date": [datetime.date(2026, 10, 28),
                                     datetime.date(2026, 11, 3)]},
                  [("2026-11-03", 1.90, None)])
    out = srv.get_earnings("AAPL")

    assert "ESTIMATED between 2026-10-28 and 2026-11-03" in out
    assert "window, not a date" in out


def test_a_settled_date_says_both_feeds_agree(earnings_stub):
    earnings_stub({"Earnings Date": [datetime.date(2026, 10, 30)]},
                  [("2026-10-30", 1.98, None)])
    out = srv.get_earnings("AAPL")

    assert "Next report: 2026-10-30" in out
    assert "ESTIMATED" not in out and "UNCONFIRMED" not in out
    assert "not a company confirmation" in out, "Yahoo's word is not the company's"


def test_two_yahoo_feeds_disagreeing_downgrades_the_date(earnings_stub):
    """
    Live AAPL: the calendar endpoint says 30 Oct and the earnings table says
    29 Oct. A date the provider cannot agree with itself on is not settled,
    whatever the single-value shape implies.
    """
    earnings_stub({"Earnings Date": [datetime.date(2026, 10, 30)]},
                  [("2026-10-29", 1.98, None)])
    out = srv.get_earnings("AAPL")

    assert "UNCONFIRMED" in out
    assert "2026-10-30" in out and "2026-10-29" in out


def test_surprise_is_not_inflated_a_hundredfold(earnings_stub):
    """
    yfinance's Surprise(%) is already a percentage. The old code multiplied it
    by 100, turning a 6.9% beat into "+674.00%" -- a number an LLM will repeat.
    """
    earnings_stub({"Earnings Date": [datetime.date(2026, 10, 30)]},
                  [("2026-07-30", 1.89, 2.02)])
    out = srv.get_earnings("AAPL")

    assert "+6.88%" in out
    assert "674" not in out


def test_an_unreported_quarter_is_pending_not_a_zero_surprise(earnings_stub):
    earnings_stub({"Earnings Date": [datetime.date(2026, 10, 30)]},
                  [("2026-10-30", 1.98, None), ("2026-07-30", 1.89, 2.02)])
    out = srv.get_earnings("AAPL")
    assert "pending" in out


def test_reported_quarters_are_confirmed_against_item_2_02(earnings_stub):
    earnings_stub({"Earnings Date": [datetime.date(2026, 10, 30)]},
                  [("2026-07-30", 1.89, 2.02)],
                  filings=[{"filing_date": "2026-07-30",
                            "acceptance": "2026-07-30T20:30:28.000Z",
                            "report_date": "2026-07-30",
                            "url": "https://www.sec.gov/Archives/x.htm"}])
    out = srv.get_earnings("AAPL")

    assert "Item 2.02" in out
    assert "2026-07-30 20:30:28" in out, "the SEC acceptance time is the confirmation"


def test_a_foreign_issuer_with_no_8k_is_not_reported_as_never_having_reported(earnings_stub):
    """TSM files 6-K, so Item 2.02 finds nothing. Absence is not evidence here."""
    earnings_stub({"Earnings Date": [datetime.date(2026, 10, 15)]},
                  [("2026-07-16", 3.89, 4.31)], filings=[])
    out = srv.get_earnings("TSM")

    assert "6-K" in out
    assert "not evidence" in out


def test_a_dead_sec_does_not_take_the_earnings_history_with_it(monkeypatch):
    monkeypatch.setattr(srv.webull_client, "yahoo_ticker",
                        lambda s: _FakeTicker({"Earnings Date": [datetime.date(2026, 10, 30)]},
                                              _dates_frame([("2026-07-30", 1.89, 2.02)])))

    def boom(*a, **k):
        raise RuntimeError("EDGAR unreachable")

    monkeypatch.setattr(srv.econ_calendar, "earnings_filings", boom)
    out = srv.get_earnings("AAPL")

    assert "+6.88%" in out, "the Yahoo history should survive an SEC outage"
    assert "SEC confirmation unavailable" in out


# =====================================================================
# get_updates: what changed, not what is true
# =====================================================================

def _filing(sym_stamp, form="8-K", items="", desc="", url="https://sec.gov/x"):
    return {"form": form, "acceptance": sym_stamp, "items": items,
            "description": desc, "url": url, "filing_date": sym_stamp[:10]}


@pytest.fixture
def quiet_updates(monkeypatch):
    """No filings, no macro, no bars -- each test switches on what it needs."""
    monkeypatch.setattr(srv.econ_calendar, "company_filings", lambda *a, **k: [])
    monkeypatch.setattr(srv.econ_calendar, "economic_calendar",
                        lambda *a, **k: ([], []))

    def no_bars(*a, **k):
        raise RuntimeError("no feed in this test")

    monkeypatch.setattr(srv.webull_client, "fetch_data", no_bars)


def test_nothing_new_is_stated_not_left_blank(quiet_updates):
    """An empty response reads as a failure; "nothing new" is a real answer."""
    out = srv.get_updates(symbols="AAPL", since="24h")
    assert "Nothing new in this window" in out
    assert "not an error" in out


def test_only_filings_newer_than_the_cutoff_are_reported(monkeypatch, quiet_updates):
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    recent = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    monkeypatch.setattr(srv.econ_calendar, "company_filings",
                        lambda *a, **k: [_filing(recent, "8-K", "2.02,9.01"),
                                         _filing(old, "10-Q")])
    out = srv.get_updates(symbols="AAPL", since="24h")

    assert "New SEC filings (1)" in out
    assert "Results of operations" in out, "8-K items carry the actual news"
    assert "10-Q" not in out, "a filing older than the cutoff must not appear"


def test_the_window_boundary_is_the_acceptance_stamp_not_the_filing_date(monkeypatch, quiet_updates):
    """
    filing_date is day-resolution. A filing accepted at 20:30 yesterday is
    inside a 24h window opened at 12:00 today only if the time is honoured.
    """
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    inside = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    outside = (now - dt.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    monkeypatch.setattr(srv.econ_calendar, "company_filings",
                        lambda *a, **k: [_filing(inside), _filing(outside)])
    out = srv.get_updates(symbols="AAPL", since="2h")
    assert "New SEC filings (1)" in out


def test_a_move_is_measured_from_before_the_window(monkeypatch, quiet_updates):
    """
    Measuring from the first bar *inside* the window discards the move that
    happened at its open -- usually the largest part of it.
    """
    frame = rising_frame(n=10, start=100.0, step=10.0)   # 100..190, +10/bar
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (frame, "Test feed"))

    out = srv.get_updates(symbols="AAPL", since="3d", move_threshold_pct=1.0)
    # Three days back covers the last 3 bars, so the reference is bar -4 (160)
    # and the newest is 190 -> +18.75%. Measuring from 170 would give +11.76%.
    assert "+18.75%" in out


def test_a_quiet_market_is_reported_with_its_numbers(monkeypatch, quiet_updates):
    flat = rising_frame(n=10, start=100.0, step=0.01)
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda sym, interval="D", count=200: (flat, "Test feed"))

    out = srv.get_updates(symbols="AAPL", since="3d", move_threshold_pct=5.0)
    assert "nothing moved 5.0% or more" in out
    assert "AAPL +0.0" in out, "the actual move should still be quoted"


def test_an_unreadable_since_is_a_tool_error_not_a_silent_default(quiet_updates):
    with pytest.raises(Exception, match="Could not read"):
        srv.get_updates(symbols="AAPL", since="last Tuesday")


def test_one_broken_symbol_does_not_hide_the_rest(monkeypatch, quiet_updates):
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    recent = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def filings(symbol, **k):
        if symbol == "BAD":
            raise RuntimeError("CIK lookup failed")
        return [_filing(recent, "8-K", "5.02")]

    monkeypatch.setattr(srv.econ_calendar, "company_filings", filings)
    out = srv.get_updates(symbols="AAPL,BAD", since="24h")

    assert "Director/officer departure" in out
    assert "Incomplete" in out and "BAD" in out


def test_a_published_macro_release_carries_its_reading(monkeypatch, quiet_updates):
    import datetime as dt
    today = dt.date.today()
    entry = {
        "date": today, "source": "BLS", "release": "Consumer Price Index",
        "reference_period": "July 2026", "slug": "cpi", "value_status": "published",
        "values": [{"series": "cpi", "short": "CPI", "status": "published",
                    "headline": {"kind": "yoy", "number": 2.7, "text": "+2.7% YoY"}}],
    }
    monkeypatch.setattr(srv.econ_calendar, "economic_calendar",
                        lambda *a, **k: ([entry], []))
    out = srv.get_updates(symbols="", since="24h")

    assert "Consumer Price Index" in out
    assert "CPI +2.7% YoY" in out


# =====================================================================
# Alerts you can trust to fire
# =====================================================================

def test_a_typod_condition_is_refused_rather_than_saved(monkeypatch):
    """
    "PRICE_ABOEV" used to save happily and report success, producing an alert
    that could never fire and that the manager then failed on every 60 seconds,
    into a log nobody reads.
    """
    called = {"n": 0}
    monkeypatch.setattr(srv.alert_manager, "add_alert",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    with pytest.raises(Exception, match="Unknown condition"):
        srv.set_alert("AAPL", "PRICE_ABOEV", 999.0)
    assert called["n"] == 0, "nothing should have been written"


def test_a_valid_condition_is_written_through_the_locked_writer(monkeypatch):
    written = []
    monkeypatch.setattr(srv.alert_manager, "add_alert", lambda a, **k: written.append(a))
    monkeypatch.setattr(srv.webull_client, "get_provenance",
                        lambda s, i: {"bar_close": 300.0, "bar_time": "2026-08-07",
                                      "source": "Test feed"})
    out = srv.set_alert("AAPL", "rsi_below", 30.0, "oversold")

    assert written and written[0]["condition"] == "RSI_BELOW"
    assert written[0]["status"] == "ACTIVE"
    assert "Price when set" in out, "the level it was judged against must be recorded"


def test_every_supported_condition_is_accepted(monkeypatch):
    """The tool's list and the manager's list must not drift apart."""
    monkeypatch.setattr(srv.alert_manager, "add_alert", lambda a, **k: None)
    monkeypatch.setattr(srv.webull_client, "get_provenance", lambda s, i: {})
    for cond in srv.alert_manager.CONDITIONS:
        srv.set_alert("AAPL", cond, 50.0)


# =====================================================================
# Pre-flight order rules
# =====================================================================

def test_a_draft_that_the_broker_would_refuse_never_reaches_the_queue(monkeypatch, tmp_path):
    """
    BUY 1 ZETA @ $0.01 previewed cleanly at $0.01 cost and was then refused at
    placement: a sub-$0.10 limit needs more than 1000 shares. Without a local
    check, that arrives as an opaque 417 *after* a human has approved it.

    Deliberately no credentials are stubbed. This check needs no network, so it
    must fire on a machine that has none -- CI caught this test passing locally
    only because a developer .env happened to be present, which meant it was
    really asserting on the account-risk path rather than on the rule.
    """
    monkeypatch.setattr(srv, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv.webull_client, "get_provenance", lambda s, i="D": {})
    monkeypatch.setattr(srv.webull_client, "get_api_client",
                        lambda: (_ for _ in ()).throw(
                            ValueError("Webull App Key and App Secret must be set in .env")))
    out = srv.draft_order("ZETA", "BUY", 1, "LMT", 0.01)

    assert "SAFETY BLOCK" in out
    assert "1,000 shares" in out
    assert not os.path.exists(os.path.join(str(tmp_path), "dashboard", "order_drafts.json"))


@pytest.mark.parametrize("qty,price,clean", [
    (1, 0.01, False),
    (1000, 0.01, False),      # the rule is *more than* 1000
    (1001, 0.01, True),
    (1, 26.0, True),
    (1, 0.10, True),          # at the boundary the step no longer applies
])
def test_the_penny_quantity_step_matches_what_the_broker_enforces(qty, price, clean):
    order = srv.broker.build_order("ZETA", "BUY", qty, "LMT", price)
    assert (srv.broker.order_rule_violations(order) == []) is clean


def test_the_rules_check_is_advisory_not_authoritative():
    """
    It exists to move common refusals earlier, not to become a second opinion
    the broker has to agree with. Anything it does not know about must still
    reach the broker.
    """
    order = srv.broker.build_order("ZETA", "BUY", 5, "LMT", 26.0)
    order["some_future_field"] = "unknown to us"
    assert srv.broker.order_rule_violations(order) == []


def test_the_offline_rules_run_before_anything_that_needs_the_network():
    """
    Order matters. The account-risk checks need credentials and a round trip;
    constructing the order and testing the broker's published rules needs
    neither. With the networked checks first, a malformed order on a machine
    with no credentials was refused for "App Key ... must be set" rather than
    for the thing actually wrong with it -- which is how a developer .env made
    the test above pass locally while CI failed.
    """
    body = dict(_tool_bodies())["draft_order"]
    rules_at = body.index("order_rule_violations")
    network_at = body.index("PRE-TRADE RISK CHECKS")
    assert rules_at < network_at, (
        "the offline construction and rule checks must precede the account checks")


def test_the_batch_launcher_does_not_claim_success_on_failure():
    """
    install.bat printed "Installation Process Complete!" unconditionally, so an
    installer that threw still ended on a success banner -- the one line a
    person actually reads.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bat = open(os.path.join(root, "install.bat"), encoding="utf-8").read()
    assert "errorlevel 1" in bat.lower(), "must check the installer's exit code"
    assert "INSTALLATION FAILED" in bat
    assert "exit /b 1" in bat, "must propagate a failure to whatever ran it"


def test_the_batch_launcher_checks_the_installer_is_present():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bat = open(os.path.join(root, "install.bat"), encoding="utf-8").read()
    assert "if not exist" in bat.lower()
