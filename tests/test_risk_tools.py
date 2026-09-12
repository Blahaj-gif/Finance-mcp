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

import finance_mcp as srv
from dashboard import webull_client as wc
from dashboard import broker
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
    monkeypatch.setattr(broker, "WEBULL_ACCOUNT_ID", "")


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

    # AAA 10 x 110 = 1100; BBB 5 x 45 = 225; gross 1325. The fixture reports no
    # currency per position, so the total is shown bare rather than as dollars.
    assert "1,325.00" in out
    assert "$1,325.00" not in out
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

    monkeypatch.setattr(broker, "WEBULL_ACCOUNT_ID", "")
    monkeypatch.setattr(wc, "call_webull", lambda fn, *a, **k: fn(*a, **k))
    client = type("T", (), {"account_v2": Multi()})()

    with pytest.raises(RuntimeError, match="WEBULL_ACCOUNT_ID"):
        broker.get_primary_account_id(client)


def test_pinned_account_is_honoured(monkeypatch):
    class Multi:
        def get_account_list(self):
            return [{"account_id": "A1"}, {"account_id": "A2"}]

    monkeypatch.setattr(broker, "WEBULL_ACCOUNT_ID", "A2")
    monkeypatch.setattr(wc, "call_webull", lambda fn, *a, **k: fn(*a, **k))
    client = type("T", (), {"account_v2": Multi()})()
    assert broker.get_primary_account_id(client) == "A2"


def test_pinned_account_must_actually_exist(monkeypatch):
    class Multi:
        def get_account_list(self):
            return [{"account_id": "A1"}, {"account_id": "A2"}]

    monkeypatch.setattr(broker, "WEBULL_ACCOUNT_ID", "NOPE")
    monkeypatch.setattr(wc, "call_webull", lambda fn, *a, **k: fn(*a, **k))
    client = type("T", (), {"account_v2": Multi()})()
    with pytest.raises(RuntimeError, match="not among"):
        broker.get_primary_account_id(client)


# =====================================================================
# Order construction
# =====================================================================

def test_build_order_normalises_aliases():
    o = broker.build_order("mu", "buy", 1, "LMT", 500.0)
    assert o["symbol"] == "MU"
    assert o["side"] == "BUY"
    assert o["order_type"] == "LIMIT"
    assert o["limit_price"] == "500.00"
    # Fields discovered the hard way against the live preview endpoint.
    assert o["entrust_type"] == "QTY"
    assert o["support_trading_session"] == "N"
    assert o["time_in_force"] == "DAY"


def test_build_order_market_has_no_limit_price():
    o = broker.build_order("MU", "SELL", 2, "MKT")
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
        broker.build_order(**kwargs)


def test_build_order_ids_are_unique():
    a = broker.build_order("MU", "BUY", 1, "MKT")
    b = broker.build_order("MU", "BUY", 1, "MKT")
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


# =====================================================================
# The broker API version actually reaches this region
# =====================================================================
# The SDK ships three generations of the order API and they do not cover the
# same regions. order_v2.cancel_order documents "Webull HK and Webull US" only,
# and returned 404 SDK.UnknownServerError from a TH account -- so the emergency
# stop had never worked here. Only order_v3 lists TH, JP, SG, AU, MY, UK, BR,
# MX, ZA and EU alongside HK and US.

def _repo(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)


def test_every_order_call_uses_the_generation_that_covers_this_region():
    import re
    offenders = []
    for path in (_repo("finance_mcp.py"), _repo("dashboard", "webull_client.py"),
                 _repo("dashboard", "app.py")):
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            # Comments explaining *why* v2 is wrong are the point, not a breach.
            if line.lstrip().startswith(("#", "*")) or '"""' in line:
                continue
            if re.search(r"\border_v[12]\s*\.", line):
                offenders.append(f"{os.path.basename(path)}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "order_v1/order_v2 do not cover every region the client supports; "
        f"use order_v3: {offenders}")


def test_cancel_takes_the_client_order_id_not_the_broker_order_id():
    """
    Webull's cancel endpoint keys on the id we generated (DRFT_9a32c8d5), not
    the broker's own order_id (037VACVVDO80O0KCJR84000000). get_open_orders
    shows both, and passing the broker one 404s -- so the tool has to say which.
    """
    src = open(_repo("finance_mcp.py"), encoding="utf-8").read()
    body = src[src.index("def cancel_order("):]
    body = body[:body.index("\n@mcp.tool()")] if "\n@mcp.tool()" in body else body
    assert "client_order_id" in body, "the tool must name which id it needs"
    assert "NOT the" in body or "not the" in body, "and must say which id it is not"


def test_preview_is_documented_as_weaker_than_placement():
    """
    Verified live: BUY 1 ZETA @ $0.01 previewed cleanly at $0.01 cost, then was
    refused at submission for a quantity-step rule. Anything that presents
    preview as a guarantee of acceptance is overstating it.
    """
    src = open(_repo("dashboard", "broker.py"), encoding="utf-8").read()
    body = src[src.index("def preview_order("):]
    body = body[:body.index("def place_order(")]
    assert "does not run every rule" in body or "weaker" in body


def test_the_data_client_no_longer_offers_a_way_to_trade():
    """
    webull_client is the market-data client plus shared plumbing; broker.py is
    the trading surface. They fail differently -- a price feed degrades to a
    fallback and says so, an order path must refuse rather than substitute --
    and keeping both behind one import is how cancel_order ended up on an API
    generation that does not serve this region.
    """
    from dashboard import webull_client as data_client
    for name in ("place_order", "cancel_order", "preview_order", "build_order",
                 "get_primary_account_id", "get_buying_power"):
        assert not hasattr(data_client, name), (
            f"webull_client still exposes {name}; it belongs in broker.py")


def test_the_broker_module_does_not_reimplement_the_signed_client():
    """One place builds the signed request, and it is not this module."""
    src = open(_repo("dashboard", "broker.py"), encoding="utf-8").read()
    assert "ApiClient(" not in src
    assert "from dashboard.webull_client import" in src or "from webull_client import" in src


# =====================================================================
# The one-time live acknowledgement
# =====================================================================
# Deliberately narrow: reads are never gated, because real quotes and a real
# portfolio are the reason to run this tool. The gate sits on the only action
# that spends money.

def test_consent_is_not_granted_until_it_is(tmp_path):
    from dashboard import live_consent as lc
    path = str(tmp_path / "c.json")
    assert lc.has_consented("ACC1", path) is False
    lc.grant("ACC1", "detail", path)
    assert lc.has_consented("ACC1", path) is True


def test_consent_is_per_account(tmp_path):
    """A second account is a different pile of money."""
    from dashboard import live_consent as lc
    path = str(tmp_path / "c.json")
    lc.grant("ACC1", "", path)
    assert lc.has_consented("ACC1", path)
    assert not lc.has_consented("ACC2", path)


def test_an_unreadable_record_fails_towards_asking_again(tmp_path):
    """
    The file records that consent was given. Unreadable must mean "ask", never
    "assume yes" -- the whole value is in the asking.
    """
    from dashboard import live_consent as lc
    path = tmp_path / "c.json"
    path.write_text("{corrupt", encoding="utf-8")
    assert lc.has_consented("ACC1", str(path)) is False


def test_consent_survives_a_crash_mid_write(tmp_path):
    from dashboard import live_consent as lc
    path = str(tmp_path / "c.json")
    lc.grant("ACC1", "detail", path)
    assert not os.path.exists(path + ".tmp")
    import json
    json.loads(open(path, encoding="utf-8").read())


def test_consent_can_be_revoked(tmp_path):
    from dashboard import live_consent as lc
    path = str(tmp_path / "c.json")
    lc.grant("ACC1", "", path)
    assert lc.revoke("ACC1", path) is True
    assert lc.has_consented("ACC1", path) is False
    assert lc.revoke("ACC1", path) is False, "revoking twice is not an error, just a no-op"


def test_consent_requires_an_account_id(tmp_path):
    from dashboard import live_consent as lc
    with pytest.raises(ValueError, match="per account"):
        lc.grant("", "", str(tmp_path / "c.json"))


def test_reading_data_is_never_gated_by_consent():
    """
    The gate is on submission alone. If it ever reaches the data path, a fresh
    install shows nothing and the tool looks broken -- which is exactly what
    defaulting the environment to paper did, and why that was reverted.
    """
    src = open(_repo("dashboard", "webull_client.py"), encoding="utf-8").read()
    assert "live_consent" not in src, "the data client must not know about consent"
    broker_src = open(_repo("dashboard", "broker.py"), encoding="utf-8").read()
    assert "live_consent" not in broker_src, (
        "place_order stays mechanical; the acknowledgement belongs in the UI "
        "that a human actually clicks")


def test_paper_mode_needs_no_acknowledgement():
    """Nothing to lose in the sandbox, so nothing to acknowledge."""
    src = open(_repo("dashboard", "app.py"), encoding="utf-8").read()
    block = src[src.index("needs_consent = ("):src.index("if needs_consent:")]
    assert "is_paper_environment()" in block


def test_the_submit_button_is_disabled_until_acknowledged():
    src = open(_repo("dashboard", "app.py"), encoding="utf-8").read()
    block = src[src.index("with col_exec:"):]
    block = block[:block.index("elif not preview:") + 40]
    assert "needs_consent" in block and "disabled=True" in block


def test_the_briefing_runs_before_the_tabs_not_on_the_submit_button():
    """
    By the time someone has drafted an order, opened Execution and clicked
    Preview, they have said they mean it three times. A fourth confirmation
    there is ceremony, and ceremony teaches people to click through warnings.
    First run is when someone is still reading.
    """
    src = open(_repo("dashboard", "app.py"), encoding="utf-8").read()
    assert src.index("_needs_briefing") < src.index("st.tabs("), (
        "the briefing must render before the tabs")


def test_the_briefing_states_what_the_assistant_can_and_cannot_do():
    src = open(_repo("dashboard", "app.py"), encoding="utf-8").read()
    block = src[src.index("### Before you start"):src.index("Shown once per account")]
    assert "What Claude can do" in block and "What Claude cannot do" in block
    assert "draft" in block.lower() and "approve" in block.lower()
    assert "heuristic" in block, "the consensus score's nature belongs here"
    assert "real money" in block


def test_everything_except_submission_works_before_the_briefing():
    """
    The gate must never reach the read path. A fresh install that shows nothing
    is the failure mode that defaulting the environment to paper produced.
    """
    src = open(_repo("dashboard", "app.py"), encoding="utf-8").read()
    briefing = src[src.index("if _needs_briefing:"):src.index("# Dashboard Tab Selection")]
    assert "st.stop()" not in briefing, "the briefing must not halt the page"


def test_the_execution_tab_points_at_the_briefing_rather_than_repeating_it():
    src = open(_repo("dashboard", "app.py"), encoding="utf-8").read()
    block = src[src.index("needs_consent = ("):src.index("# --- Step 2: submit")]
    assert "briefing" in block.lower()


def test_the_briefings_account_lookup_does_not_swallow_a_programming_error():
    """
    The module-level TradeClient import was missing, so the account lookup
    raised NameError, a bare `except Exception` caught it, and the briefing
    silently never rendered -- a feature that appeared to work because the thing
    reporting its absence was the thing that was broken. A broker that cannot be
    reached is a real reason to have no account; a NameError is not.
    """
    src = open(_repo("dashboard", "app.py"), encoding="utf-8").read()
    block = src[src.index("_briefing_account = None"):src.index("_needs_briefing = ")]
    assert "NameError" in block, "programming errors must not be silently absorbed"
    assert "_briefing_lookup_error" in block


def test_the_dashboard_imports_tradeclient_at_module_level():
    """Three call sites imported it locally; the briefing needed it at the top."""
    src = open(_repo("dashboard", "app.py"), encoding="utf-8").read()
    header = src[:src.index("# ---")]
    assert "from webull.trade.trade_client import TradeClient" in header


# =====================================================================
# A share count is a ratio of two money amounts, so they must be one currency
# =====================================================================

def test_sizing_refuses_to_divide_one_currency_by_another(monkeypatch, fake_account):
    """
    The account is THB-denominated with a USD buying-power line. Dividing a THB
    risk budget by a USD risk-per-share gives a share count wrong by the
    exchange rate -- about 35x too many shares -- and the output rendered it as
    "**THB equity**: `$320,000.00`", a THB amount with a dollar sign on it.
    """
    import sys as _sys
    # A real THB line, so the refusal under test is the currency mismatch and
    # not "no THB buying power" -- which mentions both codes and would let this
    # test pass without the fix existing.
    monkeypatch.setattr(_sys.modules[__name__], "BALANCE", {
        "total_asset_currency": "THB",
        "account_currency_assets": [
            {"currency": "THB", "cash_balance": "320000.00",
             "buying_power": "320000.00", "market_value": "0.00"},
        ],
    })
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))
    monkeypatch.setattr(srv.webull_client, "yahoo_feed_delay",
                        lambda s: {"currency": "USD", "exchange": "NasdaqGS"})

    with pytest.raises(ToolError) as caught:
        srv.calculate_position_size("AAA", stop_loss_price=90.0, risk_percent=1.0,
                                    entry_price=100.0, account_currency="THB")

    message = str(caught.value)
    assert "USD" in message and "THB" in message, \
        "the refusal must name both currencies so the caller can fix it"


def test_sizing_proceeds_when_the_currencies_agree(monkeypatch, fake_account):
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))
    monkeypatch.setattr(srv.webull_client, "yahoo_feed_delay",
                        lambda s: {"currency": "USD", "exchange": "NasdaqGS"})

    out = srv.calculate_position_size("AAA", stop_loss_price=90.0, risk_percent=1.0,
                                      entry_price=100.0, account_currency="USD")
    assert "10.0000 shares" in out


def test_an_unreadable_quote_currency_is_said_out_loud_not_assumed(monkeypatch, fake_account):
    """
    Silence is what made this invisible. An unknown must be visible rather than
    assumed away -- but it must not refuse either, or an upstream hiccup would
    stop sizing working at all.
    """
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))
    monkeypatch.setattr(srv.webull_client, "yahoo_feed_delay",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no metadata")))

    out = srv.calculate_position_size("AAA", stop_loss_price=90.0, risk_percent=1.0,
                                      entry_price=100.0, account_currency="USD")
    assert "10.0000 shares" in out
    low = out.lower()
    assert "currency" in low and "exchange rate" in low, (
        "an unreadable quote currency must be stated, not assumed away")


def test_portfolio_risk_never_adds_two_currencies_together(monkeypatch, fake_account):
    """
    Gross exposure summed every position's value regardless of denomination, and
    then divided each position by that total to get a weight. A THB holding
    beside a USD one produced a gross that is not money in any currency, and
    weights that are wrong for both.
    """
    class Mixed:
        def get_account_list(self):
            return [{"account_id": "ACC1"}]

        def get_account_balance(self, account_id):
            return BALANCE

        def get_account_position(self, account_id):
            return [
                {"symbol": "AAA", "quantity": "10", "cost_price": "100.00",
                 "last_price": "110.00", "currency": "USD"},
                {"symbol": "KKK", "quantity": "100", "cost_price": "150.00",
                 "last_price": "160.00", "currency": "THB"},
            ]

    import webull.trade.trade_client as tc
    monkeypatch.setattr(tc, "TradeClient",
                        lambda api: type("T", (), {"account_v2": Mixed()})())
    monkeypatch.setattr(srv.webull_client, "fetch_data",
                        lambda s, i="D", c=200: (flat_frame(price=100.0), "Webull OpenAPI"))

    out = srv.get_portfolio_risk()

    assert "$1,100.00" in out, "the USD book is 10 x 110"
    assert "16,000.00 THB" in out, "the THB book is 100 x 160, and is not dollars"
    assert "$17,100.00" not in out, "the two must never be added together"
    assert "100.0%" in out, "each position is the whole of its own currency book"


def test_returns_are_paired_by_date_and_never_across_a_gap():
    """
    Return series were paired by POSITION after reset_index(drop=True), so a
    holding that missed a session had every later return lined up against a
    different day's return for its neighbours -- and the beta and correlations
    were computed on that. Measured on a synthetic book with a true beta of
    2.023, position-pairing gave 1.897 to 2.031 depending on the gap pattern.

    Aligning on dates is necessary but not sufficient: after an inner join, the
    bar following a gap is a multi-day return for the leg that skipped and a
    one-day return for the others. Those rows are dropped too.
    """
    import pandas as pd
    days = pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03",
                           "2026-09-04", "2026-09-08"])
    a = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0], index=days)
    b = pd.Series([50.0, 50.5, 51.5, 52.0],
                  index=days[[0, 1, 3, 4]])          # 2026-09-03 missing
    bench = pd.Series([10.0, 10.1, 10.2, 10.3, 10.4], index=days)

    rets, bench_ret = srv._align_on_sessions({"A": a, "B": b}, bench)

    assert list(rets.columns) == ["A", "B"]
    kept = [str(d.date()) for d in rets.index]
    assert kept == ["2026-09-02", "2026-09-08"], (
        f"the bar after B's gap must be dropped, kept {kept}")
    assert len(bench_ret) == len(rets)


def test_alignment_keeps_a_clean_book_whole():
    """The mask must not throw away sessions when nothing is missing."""
    import pandas as pd
    days = pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"])
    a = pd.Series([100.0, 101.0, 102.0, 103.0], index=days)
    b = pd.Series([50.0, 50.5, 51.0, 51.5], index=days)
    bench = pd.Series([10.0, 10.1, 10.2, 10.3], index=days)

    rets, bench_ret = srv._align_on_sessions({"A": a, "B": b}, bench)
    assert len(rets) == 3, "three returns from four clean sessions"
