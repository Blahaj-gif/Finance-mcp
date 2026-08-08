"""
Tests for the risk, sizing and options-analytics tools.

These are the tools whose output a person acts on with money, so the arithmetic
is pinned down rather than smoke-tested.
"""
import datetime
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webull_enriched_mcp as srv
from dashboard import webull_client as wc
from fastmcp.exceptions import ToolError

BALANCE = {
    "total_asset_currency": "THB",
    "account_currency_assets": [
        {"currency": "USD", "cash_balance": "10000.00",
         "buying_power": "10000.00", "market_value": "0.00"},
    ],
}


def flat_frame(n=90, price=100.0, noise=1.0):
    """Oldest-first frame oscillating around `price` so ATR is well defined."""
    end = datetime.datetime.utcnow()
    times = [end - datetime.timedelta(days=(n - 1 - i)) for i in range(n)]
    closes = [price + (noise if i % 2 else -noise) for i in range(n)]
    return pd.DataFrame({
        "time": [t.strftime("%Y-%m-%d %H:%M:%S") for t in times],
        "open": closes,
        "high": [c + noise for c in closes],
        "low": [c - noise for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    })


@pytest.fixture
def fake_account(monkeypatch):
    class FakeAccountV2:
        def get_account_list(self):
            return [{"account_id": "ACC1", "account_label": "Individual Cash"}]

        def get_account_balance(self, account_id):
            return BALANCE

        def get_account_position(self, account_id):
            return [
                {"symbol": "AAA", "quantity": "10", "cost_price": "100.00", "last_price": "110.00"},
                {"symbol": "BBB", "quantity": "5", "cost_price": "50.00", "last_price": "45.00"},
            ]

    import webull.trade.trade_client as tc
    monkeypatch.setattr(tc, "TradeClient", lambda api: type("T", (), {"account_v2": FakeAccountV2()})())
    monkeypatch.setattr(wc, "get_api_client", lambda: object())
    monkeypatch.setattr(srv.webull_client, "get_api_client", lambda: object())
    monkeypatch.setattr(wc, "WEBULL_ACCOUNT_ID", "")


# =====================================================================
# Position sizing
# =====================================================================

def test_position_size_risks_exactly_the_requested_percent(monkeypatch, fake_account):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))

    # $10,000 equity, 1% risk = $100 budget. Entry 100, stop 90 -> $10/share -> 10 shares.
    out = srv.calculate_position_size("AAA", stop_loss_price=90.0, risk_percent=1.0, entry_price=100.0)

    assert "Risk budget @ 1%**: `$100.00`" in out
    assert "10.0000 shares" in out
    assert "LONG" in out


def test_position_size_halves_when_stop_is_twice_as_wide(monkeypatch, fake_account):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))

    wide = srv.calculate_position_size("AAA", stop_loss_price=80.0, risk_percent=1.0, entry_price=100.0)
    assert "5.0000 shares" in wide, "double the risk per share must halve the size"


def test_position_size_caps_at_buying_power(monkeypatch, fake_account):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))

    # A 0.1% stop implies a huge share count; buying power must cap it.
    out = srv.calculate_position_size("AAA", stop_loss_price=99.9, risk_percent=1.0, entry_price=100.0)
    assert "capped" in out.lower()
    assert "buying power" in out.lower()


def test_position_size_detects_a_short(monkeypatch, fake_account):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))
    out = srv.calculate_position_size("AAA", stop_loss_price=110.0, risk_percent=1.0, entry_price=100.0)
    assert "SHORT" in out


def test_position_size_rejects_a_zero_width_stop(monkeypatch, fake_account):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))
    with pytest.raises(ToolError, match="risk per share"):
        srv.calculate_position_size("AAA", stop_loss_price=100.0, entry_price=100.0)


def test_position_size_rejects_a_nonsense_risk_percent(monkeypatch, fake_account):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))
    with pytest.raises(ToolError, match="risk_percent"):
        srv.calculate_position_size("AAA", stop_loss_price=90.0, risk_percent=150.0)


def test_position_size_warns_when_the_stop_sits_inside_daily_noise(monkeypatch, fake_account):
    # noise=5 gives a wide ATR; a $1 stop is well inside it.
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0, noise=5.0), "Webull OpenAPI"))
    out = srv.calculate_position_size("AAA", stop_loss_price=99.0, entry_price=100.0)
    assert "inside one ATR" in out


# =====================================================================
# Portfolio risk
# =====================================================================

def test_portfolio_risk_computes_weights_and_pnl(monkeypatch, fake_account):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))
    out = srv.get_portfolio_risk()

    # AAA 10 x 110 = 1100; BBB 5 x 45 = 225; gross 1325.
    assert "$1,325.00" in out
    assert "83.0%" in out or "83.02%" in out    # AAA weight
    assert "+10.00%" in out                      # AAA P&L
    assert "-10.00%" in out                      # BBB P&L


def test_portfolio_risk_flags_concentration(monkeypatch, fake_account):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))
    out = srv.get_portfolio_risk()
    assert "concentration above 40%" in out
    assert "AAA" in out.split("Risk notes")[1]


def test_portfolio_risk_survives_a_symbol_with_no_history(monkeypatch, fake_account):
    def flaky(s, i="D", c=200):
        if s == "BBB":
            raise RuntimeError("delisted")
        return flat_frame(price=100.0), "Webull OpenAPI"

    monkeypatch.setattr(srv.webull_client, "fetch_data", flaky)
    out = srv.get_portfolio_risk()
    assert "BBB" in out
    assert "no price history" in out
    assert "n/a" in out          # vol column for the failed symbol


def test_portfolio_risk_handles_an_empty_account(monkeypatch, fake_account):
    import webull.trade.trade_client as tc

    class Empty:
        def get_account_list(self): return [{"account_id": "ACC1"}]
        def get_account_position(self, account_id): return []

    monkeypatch.setattr(tc, "TradeClient", lambda api: type("T", (), {"account_v2": Empty()})())
    assert "No open positions" in srv.get_portfolio_risk()


# =====================================================================
# Multi-account safety
# =====================================================================

def test_multiple_accounts_refuse_to_guess(monkeypatch):
    class Multi:
        def get_account_list(self):
            return [{"account_id": "A1", "account_label": "Cash"},
                    {"account_id": "A2", "account_label": "Margin"}]

    monkeypatch.setattr(wc, "WEBULL_ACCOUNT_ID", "")
    monkeypatch.setattr(wc, "call_webull", lambda fn, *a, **k: fn(*a, **k))
    client = type("T", (), {"account_v2": Multi()})()

    with pytest.raises(RuntimeError, match="WEBULL_ACCOUNT_ID"):
        wc.get_primary_account_id(client)


def test_pinned_account_is_honoured(monkeypatch):
    class Multi:
        def get_account_list(self):
            return [{"account_id": "A1"}, {"account_id": "A2"}]

    monkeypatch.setattr(wc, "WEBULL_ACCOUNT_ID", "A2")
    monkeypatch.setattr(wc, "call_webull", lambda fn, *a, **k: fn(*a, **k))
    client = type("T", (), {"account_v2": Multi()})()
    assert wc.get_primary_account_id(client) == "A2"


def test_pinned_account_must_actually_exist(monkeypatch):
    class Multi:
        def get_account_list(self):
            return [{"account_id": "A1"}, {"account_id": "A2"}]

    monkeypatch.setattr(wc, "WEBULL_ACCOUNT_ID", "NOPE")
    monkeypatch.setattr(wc, "call_webull", lambda fn, *a, **k: fn(*a, **k))
    client = type("T", (), {"account_v2": Multi()})()
    with pytest.raises(RuntimeError, match="not among"):
        wc.get_primary_account_id(client)


# =====================================================================
# Order construction
# =====================================================================

def test_build_order_normalises_aliases():
    o = wc.build_order("mu", "buy", 1, "LMT", 500.0)
    assert o["symbol"] == "MU"
    assert o["side"] == "BUY"
    assert o["order_type"] == "LIMIT"
    assert o["limit_price"] == "500.00"
    # Fields discovered the hard way against the live preview endpoint.
    assert o["entrust_type"] == "QTY"
    assert o["support_trading_session"] == "N"
    assert o["time_in_force"] == "DAY"


def test_build_order_market_has_no_limit_price():
    o = wc.build_order("MU", "SELL", 2, "MKT")
    assert o["order_type"] == "MARKET"
    assert "limit_price" not in o


@pytest.mark.parametrize("kwargs,match", [
    (dict(symbol="MU", action="BUY", quantity=1, order_type="LMT"), "limit_price"),
    (dict(symbol="MU", action="HODL", quantity=1, order_type="MKT"), "BUY or SELL"),
    (dict(symbol="MU", action="BUY", quantity=0, order_type="MKT"), "positive"),
    (dict(symbol="MU", action="BUY", quantity=1, order_type="ICEBERG"), "Unsupported order_type"),
])
def test_build_order_rejects_unsendable_orders(kwargs, match):
    with pytest.raises(ValueError, match=match):
        wc.build_order(**kwargs)


def test_build_order_ids_are_unique():
    a = wc.build_order("MU", "BUY", 1, "MKT")
    b = wc.build_order("MU", "BUY", 1, "MKT")
    assert a["client_order_id"] != b["client_order_id"]


# =====================================================================
# Provenance
# =====================================================================

def test_provenance_records_source_and_bar_time(monkeypatch):
    frame = flat_frame(price=100.0)
    monkeypatch.setattr(wc, "fetch_data", lambda s, i="D", c=200: (frame, "Webull OpenAPI (Cached)"))
    p = wc.get_provenance("AAA")
    assert p["source"] == "Webull OpenAPI"       # cache marker stripped
    assert p["bar_time"] == frame["time"].iloc[-1]
    assert p["bar_close"] == pytest.approx(float(frame["close"].iloc[-1]))


def test_provenance_never_raises(monkeypatch):
    """Metadata capture must not block the write it annotates."""
    monkeypatch.setattr(wc, "fetch_data", lambda s, i="D", c=200: (_ for _ in ()).throw(RuntimeError("feed down")))
    p = wc.get_provenance("AAA")
    assert p["source"] == "UNAVAILABLE"
    assert "feed down" in p["error"]
