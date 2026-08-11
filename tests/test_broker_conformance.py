"""
One suite, every broker.

The point of a broker protocol is not that two classes share method names --
it is that the guarantees the execution path depends on hold for both. Those
guarantees were all learned the expensive way from Webull, and each test below
names the incident that produced it.

Saxo and IBKR are driven through an injected session returning documented
response shapes. That proves each adapter is wired to the API it was written
against; it proves nothing about what either API actually returns. See
HELP-WANTED.md.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import broker_protocol as bp
from dashboard import brokers


# =====================================================================
# Fakes
# =====================================================================

def saxo_session(method, url, body, headers):
    """Documented Saxo shapes. Not a recording -- Saxo has never been called."""
    if "/accounts/me" in url:
        return {"Data": [{"AccountKey": "AK1", "AccountId": "1", "Currency": "USD"}]}
    if "/ref/v1/instruments" in url:
        return {"Data": [{"Symbol": "AAPL:xnas", "Identifier": 211, "ExchangeId": "NASDAQ"}]}
    if "/port/v1/balances" in url:
        return {"Currency": "USD", "CashAvailableForTrading": 5000.0}
    if "/netpositions" in url:
        return {"Data": [{
            "NetPositionBase": {"Symbol": "AAPL", "Uic": 211, "AssetType": "Stock",
                                "Amount": 10, "Currency": "USD"},
            "NetPositionView": {"AverageOpenPrice": 300.0, "CurrentPrice": 313.06,
                                "ExposureCurrency": "USD"}}]}
    if "/precheck" in url:
        return {"EstimatedCashRequired": 3130.6, "Currency": "USD",
                "EstimatedCosts": {"TotalCost": 1.0}, "PreCheckResult": "Ok"}
    if "/trade/v2/orders" in url and method == "POST":
        return {"OrderId": "987654"}
    return {}


def make_saxo(session=saxo_session):
    from dashboard.brokers.saxo import SaxoBroker
    return SaxoBroker(token="fake-token", environment="sim", session=session)


def make_webull():
    """
    The real adapter with its network calls stubbed. Nothing here touches a
    broker; the live exercise of this path is recorded in the git history.
    """
    from dashboard.brokers.webull import WebullBroker

    class Stub(WebullBroker):
        verified = True

        def __init__(self):
            super().__init__()
            self._account_id = "ACC1"

        def _client(self):
            raise AssertionError("no network in conformance tests")

        def primary_account_id(self):
            return "ACC1"

        def buying_power(self, currency="USD"):
            from dashboard import broker as wb
            return wb.get_buying_power(
                {"account_currency_assets": [
                    {"currency": "HKD", "buying_power": "0.00"},
                    {"currency": "USD", "buying_power": "5000.00"}]}, currency)

        def positions(self):
            return [{"symbol": "AAPL", "quantity": 10.0, "cost": 300.0,
                     "last": 313.06, "currency": "USD", "raw": {}}]

        def position_quantity(self, symbol):
            return next((p["quantity"] for p in self.positions()
                         if p["symbol"].upper() == symbol.upper()), 0.0)

        def preview_order(self, order):
            return {"cost": 3130.6, "fee": 1.0, "currency": "USD", "raw": {}}

        def place_order(self, order):
            return {"order_id": "WB1", "client_order_id": order["client_order_id"],
                    "raw": {}}

        def cancel_order(self, client_order_id):
            return {"order_id": "WB1", "client_order_id": client_order_id, "raw": {}}

    return Stub()


IBKR_ACCOUNT = "U1234567"


def ibkr_session(method, url, body, headers):
    """
    Documented IBKR Client Portal Web API shapes. Not a recording -- IBKR has
    never been called. Money comes back as display strings on purpose: that is
    what the whatif endpoint documents, and it is the part most likely to be
    parsed into a wrong number.
    """
    path = url.split("/v1/api", 1)[-1].split("?")[0]

    if path == "/iserver/auth/status":
        return {"authenticated": True, "connected": True, "competing": False}
    if path == "/iserver/accounts":
        return {"accounts": [IBKR_ACCOUNT], "selectedAccount": IBKR_ACCOUNT}
    if path == "/portfolio/accounts":
        return [{"accountId": IBKR_ACCOUNT, "currency": "USD"}]
    if path == f"/portfolio/{IBKR_ACCOUNT}/ledger":
        return {"USD": {"currency": "USD", "cashbalance": 4000.0,
                        "settledcash": 4000.0, "key": "LedgerList"},
                "BASE": {"currency": "USD", "cashbalance": 4000.0,
                         "key": "LedgerList"}}
    if path == f"/portfolio/{IBKR_ACCOUNT}/summary":
        return {"buyingpower": {"amount": 5000.0, "currency": "USD",
                                "isNull": False, "severity": 0},
                "availablefunds": {"amount": 1250.0, "currency": "USD",
                                   "isNull": False},
                "netliquidation": {"amount": 7500.0, "currency": "USD",
                                   "isNull": False}}
    if path.startswith(f"/portfolio/{IBKR_ACCOUNT}/positions/"):
        if path.endswith("/0"):
            return [{"acctId": IBKR_ACCOUNT, "conid": 265598, "ticker": "AAPL",
                     "contractDesc": "AAPL", "position": 10.0, "avgCost": 300.0,
                     "avgPrice": 300.0, "mktPrice": 313.06, "mktValue": 3130.6,
                     "currency": "USD", "assetClass": "STK"}]
        return []
    if path == "/iserver/secdef/search":
        return [{"conid": "265598", "symbol": "AAPL", "companyName": "APPLE INC",
                 "companyHeader": "APPLE INC - NASDAQ", "description": "NASDAQ",
                 "sections": [{"secType": "STK"}, {"secType": "OPT"}]}]
    if path == f"/iserver/account/{IBKR_ACCOUNT}/orders/whatif":
        return {"amount": {"amount": "3,130.60 USD", "commission": "1.00 USD",
                           "total": "3,131.60 USD"},
                "equity": {"current": "7,500.00 USD", "after": "7,499.00 USD"},
                "initial": {"current": "0.00 USD", "after": "1,565.30 USD"},
                "maintenance": {"current": "0.00 USD", "after": "1,565.30 USD"},
                "warn": None, "error": None}
    if path == f"/iserver/account/{IBKR_ACCOUNT}/orders" and method == "POST":
        sent = (body or {}).get("orders", [{}])[0]
        return [{"order_id": "IB1", "order_status": "PreSubmitted",
                 "local_order_id": sent.get("cOID", "")}]
    if path == "/iserver/account/orders":
        return {"orders": [{"orderId": "IB1", "order_ref": "DRFT_live",
                            "ticker": "AAPL", "status": "Submitted",
                            "side": "BUY", "remainingQuantity": 1.0}],
                "snapshot": True}
    if path.startswith(f"/iserver/account/{IBKR_ACCOUNT}/order/") and method == "DELETE":
        return {"order_id": path.rsplit("/", 1)[-1], "msg": "Request was submitted"}
    return {}


def make_ibkr(session=ibkr_session):
    from dashboard.brokers.ibkr import IbkrBroker
    return IbkrBroker(base_url="https://localhost:5000/v1/api",
                      account_id=IBKR_ACCOUNT, session=session)


ADAPTERS = {"webull": make_webull, "saxo": make_saxo, "ibkr": make_ibkr}


@pytest.fixture(params=sorted(ADAPTERS))
def broker(request):
    return ADAPTERS[request.param]()


# =====================================================================
# The protocol itself
# =====================================================================

def test_every_adapter_satisfies_the_protocol(broker):
    assert isinstance(broker, bp.Broker)


def test_every_adapter_declares_whether_it_has_been_verified(broker):
    """
    A number from an adapter nobody has run is not the same kind of number as
    one from an adapter that has placed a real order. The distinction has to
    survive into output, so it starts as an attribute.
    """
    assert isinstance(broker.verified, bool)


def test_an_unverified_adapter_says_so_in_its_own_description(broker):
    text = bp.describe(broker)
    assert broker.name in text
    if not broker.verified:
        assert "UNVERIFIED" in text
        assert "never been run" in text


def test_the_registry_exposes_every_adapter(monkeypatch):
    assert set(brokers.available()) >= {"webull", "saxo", "ibkr"}
    monkeypatch.setenv("FINANCE_BROKER", "saxo")
    assert brokers.active_name() == "saxo"


def test_an_unknown_broker_is_refused_by_name():
    with pytest.raises(ValueError, match="Unknown broker"):
        brokers.get("etrade")


def test_a_broker_that_never_asks_says_so_rather_than_faking_a_confirmation(broker):
    """
    IBKR can answer a placement with questions; Webull and Saxo cannot. A
    no-op `confirm_order` on the two that never ask would report success for
    something that did not happen, so they refuse instead.
    """
    if broker.name == "ibkr":
        return
    with pytest.raises(NotImplementedError):
        broker.confirm_order("whatever")


# =====================================================================
# Guarantees the execution path depends on
# =====================================================================

def test_order_construction_refuses_what_cannot_be_sent(broker):
    """A draft that cannot become an order is noise in a human approval queue."""
    for bad in (dict(action="HOLD", quantity=1, order_type="LMT", limit_price=1.0),
                dict(action="BUY", quantity=0, order_type="LMT", limit_price=1.0),
                dict(action="BUY", quantity=-5, order_type="LMT", limit_price=1.0),
                dict(action="BUY", quantity=1, order_type="ICEBERG", limit_price=1.0),
                dict(action="BUY", quantity=1, order_type="LMT", limit_price=None)):
        with pytest.raises(ValueError):
            broker.build_order("AAPL", **bad)


def test_a_limit_order_carries_its_price(broker):
    order = broker.build_order("AAPL", "BUY", 10, "LMT", 313.06,
                               client_order_id="DRFT_test")
    body = str(order)
    assert "313.06" in body, f"limit price missing from {order!r}"


def test_the_client_order_id_we_supply_is_the_one_carried(broker):
    """
    Cancellation keys on an id we chose. If the adapter drops it, the order
    becomes uncancellable through this tool -- which is how the Webull cancel
    path was dead for months without anyone noticing.
    """
    order = broker.build_order("AAPL", "BUY", 1, "LMT", 1.0,
                               client_order_id="DRFT_deadbeef")
    assert "DRFT_deadbeef" in str(order)


def test_preview_returns_a_normalised_quote(broker):
    order = broker.build_order("AAPL", "BUY", 10, "LMT", 313.06,
                               client_order_id="DRFT_test")
    quote = broker.preview_order(order)
    assert set(quote) >= {"cost", "fee", "currency"}
    assert quote["cost"] == pytest.approx(3130.6)
    assert isinstance(quote["currency"], str)


def test_placement_returns_both_ids(broker):
    """
    The broker's id and ours. get_open_orders shows both, and passing the wrong
    one to cancel is a 404 -- so both have to come back from placement.
    """
    order = broker.build_order("AAPL", "BUY", 1, "LMT", 1.0,
                               client_order_id="DRFT_ids")
    placed = broker.place_order(order)
    assert set(placed) >= {"order_id", "client_order_id"}
    assert placed["client_order_id"] == "DRFT_ids"
    assert placed["order_id"]


def test_buying_power_is_asked_for_by_currency(broker):
    """
    A THB account holding US equities has a separate USD line. Summing them is
    a category error, and taking the first line printed "0.00 HKD" on an
    account with 333.83 USD in it.
    """
    assert broker.buying_power("USD") == pytest.approx(5000.0)


def test_buying_power_refuses_a_currency_it_cannot_answer_for(broker):
    with pytest.raises(Exception):
        broker.buying_power("XYZ")


def test_positions_are_normalised(broker):
    for p in broker.positions():
        assert set(p) >= {"symbol", "quantity", "currency"}
        assert isinstance(p["quantity"], float)


def test_position_quantity_is_zero_when_flat(broker):
    assert broker.position_quantity("NOTHELD") == 0.0
    assert broker.position_quantity("AAPL") == pytest.approx(10.0)


def test_rule_violations_returns_sentences_not_codes(broker):
    order = broker.build_order("AAPL", "BUY", 10, "LMT", 313.06,
                               client_order_id="DRFT_ok")
    problems = broker.rule_violations(order)
    assert isinstance(problems, list)
    assert all(isinstance(p, str) for p in problems)


# =====================================================================
# Saxo specifics: where it refuses rather than guesses
# =====================================================================

def test_saxo_refuses_to_cancel_by_an_id_saxo_does_not_key_on():
    """
    Webull cancels by the client id; Saxo cancels by its own OrderId and there
    is no documented mapping between them. Guessing means cancelling the wrong
    order or silently cancelling nothing -- the exact failure the Webull path
    already had once, found only when a live order needed pulling.
    """
    from dashboard.brokers.saxo import SaxoNotVerified
    with pytest.raises(SaxoNotVerified, match="OrderId"):
        make_saxo().cancel_order("DRFT_abc")


def test_saxo_refuses_an_ambiguous_ticker():
    """
    The same ticker lists on several exchanges. Which one an order reaches is
    not a detail to settle by taking the first row.
    """
    from dashboard.brokers.saxo import SaxoError

    def ambiguous(method, url, body, headers):
        if "/ref/v1/instruments" in url:
            return {"Data": [
                {"Symbol": "AAPL:xnas", "Identifier": 211, "ExchangeId": "NASDAQ"},
                {"Symbol": "AAPL:xetr", "Identifier": 4001, "ExchangeId": "XETRA"}]}
        return saxo_session(method, url, body, headers)

    with pytest.raises(SaxoError, match="ambiguous"):
        make_saxo(ambiguous).build_order("AAPL", "BUY", 1, "LMT", 1.0)


def test_saxo_refuses_to_convert_currencies_for_a_buying_power_check():
    """
    Saxo reports one balance block per account, not a line per currency.
    Converting at an unstated rate is how a buying-power check passes when it
    should not.
    """
    from dashboard.brokers.saxo import SaxoError

    def eur_account(method, url, body, headers):
        if "/port/v1/balances" in url:
            return {"Currency": "EUR", "CashAvailableForTrading": 5000.0}
        return saxo_session(method, url, body, headers)

    with pytest.raises(SaxoError, match="denominated in EUR"):
        make_saxo(eur_account).buying_power("USD")


def test_saxo_refuses_to_send_anything_without_a_token():
    from dashboard.brokers.saxo import SaxoBroker, SaxoError
    with pytest.raises(SaxoError, match="SAXO_ACCESS_TOKEN"):
        SaxoBroker(token="", environment="sim").primary_account_id()


def test_saxo_refuses_several_accounts_rather_than_choosing_one():
    from dashboard.brokers.saxo import SaxoError

    def two_accounts(method, url, body, headers):
        if "/accounts/me" in url:
            return {"Data": [{"AccountKey": "A", "AccountId": "1", "Currency": "USD"},
                             {"AccountKey": "B", "AccountId": "2", "Currency": "EUR"}]}
        return saxo_session(method, url, body, headers)

    with pytest.raises(SaxoError, match="SAXO_ACCOUNT_KEY"):
        make_saxo(two_accounts).primary_account_id()


def test_saxo_marks_an_undocumented_balance_field_rather_than_returning_zero():
    from dashboard.brokers.saxo import SaxoNotVerified

    def odd_balance(method, url, body, headers):
        if "/port/v1/balances" in url:
            return {"Currency": "USD", "SomeFieldWeHaveNeverSeen": 1.0}
        return saxo_session(method, url, body, headers)

    with pytest.raises(SaxoNotVerified, match="VERIFIER"):
        make_saxo(odd_balance).buying_power("USD")


def test_saxo_hits_the_documented_paths():
    """Pins the adapter to the endpoints it was written against."""
    seen = []

    def recording(method, url, body, headers):
        seen.append((method, url.split("/openapi")[-1].split("?")[0]))
        return saxo_session(method, url, body, headers)

    s = make_saxo(recording)
    order = s.build_order("AAPL", "BUY", 1, "LMT", 1.0, client_order_id="DRFT_p")
    s.preview_order(order)
    s.place_order(order)
    s.positions()
    s.buying_power("USD")

    paths = [p for _, p in seen]
    for expected in ("/port/v1/accounts/me", "/ref/v1/instruments",
                     "/trade/v2/orders/precheck", "/trade/v2/orders",
                     "/port/v1/netpositions", "/port/v1/balances"):
        assert expected in paths, f"{expected} was never called; got {paths}"


def test_saxo_sends_a_bearer_token_and_nothing_else():
    captured = {}

    def capture(method, url, body, headers):
        captured.update(headers)
        return saxo_session(method, url, body, headers)

    make_saxo(capture).primary_account_id()
    assert captured.get("Authorization") == "Bearer fake-token"


def test_the_saxo_adapter_is_not_marked_verified():
    """
    This flag means somebody ran it against the API and reported what happened.
    It must never be flipped by reading the code.
    """
    from dashboard.brokers.saxo import SaxoBroker
    assert SaxoBroker.verified is False, (
        "set verified = True only after a real run, alongside the evidence")


# =====================================================================
# IBKR specifics: the two things it does that neither other broker does
# =====================================================================

def test_ibkr_raises_its_confirmation_questions_instead_of_answering_them():
    """
    This is the whole reason the IBKR adapter is worth having in a
    human-approval tool. IBKR replies to a placement with warnings -- no market
    data, likely to fill immediately, price outside a percentage constraint --
    and every client library answers them from a table of canned replies so
    that placement looks like one call. A warning addressed to a person that a
    program answers is not a warning.
    """
    def asks(method, url, body, headers):
        if url.endswith(f"/iserver/account/{IBKR_ACCOUNT}/orders") and method == "POST":
            return [{"id": "reply-abc123",
                     "message": ["You are submitting an order without market "
                                 "data. We strongly recommend against this."],
                     "isSuppressed": False, "messageIds": ["o354"]}]
        return ibkr_session(method, url, body, headers)

    b = make_ibkr(asks)
    order = b.build_order("AAPL", "BUY", 1, "LMT", 1.0, client_order_id="DRFT_q")
    with pytest.raises(bp.ConfirmationRequired) as raised:
        b.place_order(order)

    e = raised.value
    assert e.reply_id == "reply-abc123"
    assert e.broker == "ibkr"
    assert e.client_order_id == "DRFT_q"
    # The broker's own words, unedited. Paraphrasing a risk warning is how it
    # stops being one.
    assert "without market data" in e.questions[0]
    assert "without market data" in str(e)


def test_ibkr_confirmation_goes_to_the_reply_endpoint_and_carries_the_human_answer():
    seen = []

    def recording(method, url, body, headers):
        seen.append((method, url.split("/v1/api", 1)[-1], body))
        if "/iserver/reply/" in url:
            return [{"order_id": "IB9", "order_status": "PreSubmitted",
                     "local_order_id": "DRFT_q"}]
        return ibkr_session(method, url, body, headers)

    placed = make_ibkr(recording).confirm_order("reply-abc123", confirmed=True)
    assert placed["order_id"] == "IB9"
    assert ("POST", "/iserver/reply/reply-abc123", {"confirmed": True}) in seen


def test_ibkr_can_decline_a_confirmation():
    """`confirmed=False` is a real answer, not an error path."""
    captured = {}

    def recording(method, url, body, headers):
        if "/iserver/reply/" in url:
            captured.update(body or {})
            return [{"order_id": "IB9", "order_status": "Cancelled"}]
        return ibkr_session(method, url, body, headers)

    make_ibkr(recording).confirm_order("reply-abc123", confirmed=False)
    assert captured == {"confirmed": False}


def test_ibkr_refuses_a_question_it_could_never_answer():
    """
    A message with no reply id cannot be confirmed, so raising
    ConfirmationRequired would hand the caller a dead end and leave an order in
    an unknown state without saying so.
    """
    from dashboard.brokers.ibkr import IbkrError

    def unanswerable(method, url, body, headers):
        if url.endswith(f"/iserver/account/{IBKR_ACCOUNT}/orders") and method == "POST":
            return [{"message": ["Something happened"]}]
        return ibkr_session(method, url, body, headers)

    b = make_ibkr(unanswerable)
    order = b.build_order("AAPL", "BUY", 1, "LMT", 1.0, client_order_id="DRFT_u")
    with pytest.raises(IbkrError, match="neither confirmed nor assumed placed"):
        b.place_order(order)


def test_ibkr_never_reports_a_placement_it_cannot_evidence():
    """
    Neither an order id nor a question means the outcome is unknown, and an
    unknown outcome reported as success is how a duplicate order gets sent.
    """
    from dashboard.brokers.ibkr import IbkrError

    def vague(method, url, body, headers):
        if url.endswith(f"/iserver/account/{IBKR_ACCOUNT}/orders") and method == "POST":
            return [{"order_status": "PreSubmitted"}]
        return ibkr_session(method, url, body, headers)

    b = make_ibkr(vague)
    order = b.build_order("AAPL", "BUY", 1, "LMT", 1.0, client_order_id="DRFT_v")
    with pytest.raises(IbkrError, match="unknown"):
        b.place_order(order)


def test_ibkr_cancels_by_our_id_through_the_lookup_saxo_lacks():
    """
    IBKR keys cancellation on its own orderId, like Saxo. Unlike Saxo it
    documents order_ref on the live-orders response, so the mapping the
    protocol requires exists and the adapter performs it.
    """
    seen = []

    def recording(method, url, body, headers):
        seen.append((method, url.split("/v1/api", 1)[-1]))
        return ibkr_session(method, url, body, headers)

    result = make_ibkr(recording).cancel_order("DRFT_live")
    assert result["order_id"] == "IB1"
    assert result["client_order_id"] == "DRFT_live"
    assert ("GET", "/iserver/account/orders") in seen
    assert ("DELETE", f"/iserver/account/{IBKR_ACCOUNT}/order/IB1") in seen


def test_ibkr_refuses_to_cancel_an_id_it_cannot_find():
    from dashboard.brokers.ibkr import IbkrError
    with pytest.raises(IbkrError, match="Nothing was cancelled"):
        make_ibkr().cancel_order("DRFT_never_placed")


def test_ibkr_flags_a_missing_order_ref_rather_than_cancelling_something_plausible():
    from dashboard.brokers.ibkr import IbkrNotVerified

    def no_ref(method, url, body, headers):
        if url.endswith("/iserver/account/orders"):
            return {"orders": [{"orderId": "IB1", "ticker": "AAPL"}]}
        return ibkr_session(method, url, body, headers)

    with pytest.raises(IbkrNotVerified, match="VERIFIER"):
        make_ibkr(no_ref).cancel_order("DRFT_live")


def test_ibkr_will_not_send_the_cancel_everything_sentinel():
    """IBKR documents -1 as "cancel every open order". Not reachable by accident."""
    with pytest.raises(ValueError, match="every open order"):
        make_ibkr().cancel_order_by_order_id("-1")


def test_ibkr_refuses_a_currency_it_pools_margin_away_from():
    """
    IBKR reports cash per currency and buying power in the base currency only.
    Reporting one under the other's name would be wrong in a safe direction,
    and a guard that is wrong in a safe direction is a guard nobody trusts.
    """
    from dashboard.brokers.ibkr import IbkrError
    with pytest.raises(IbkrError, match="computes buying power in USD"):
        make_ibkr().buying_power("EUR")


def test_ibkr_names_the_currency_to_ask_for_instead_of_just_refusing():
    from dashboard.brokers.ibkr import IbkrError

    def eur_base(method, url, body, headers):
        if url.endswith("/ledger"):
            return {"EUR": {"currency": "EUR", "cashbalance": 10.0},
                    "BASE": {"currency": "EUR", "cashbalance": 10.0}}
        return ibkr_session(method, url, body, headers)

    with pytest.raises(IbkrError, match="Ask for EUR"):
        make_ibkr(eur_base).buying_power("USD")


def test_ibkr_reads_money_out_of_the_display_strings_whatif_returns():
    """
    IBKR prices orders in strings meant for a screen -- "3,130.60 USD" -- and
    the comma is one bad parse away from a hundredfold error in the number a
    person approves.
    """
    quote = make_ibkr().preview_order(
        make_ibkr().build_order("AAPL", "BUY", 10, "LMT", 313.06,
                                client_order_id="DRFT_m"))
    assert quote["cost"] == pytest.approx(3130.6)
    assert quote["fee"] == pytest.approx(1.0)
    assert quote["currency"] == "USD"
    assert quote["initial_margin"] == pytest.approx(1565.3)


def test_ibkr_takes_the_expensive_end_of_a_commission_range():
    """
    IBKR sometimes prices commission as a range. The number is shown to a
    person deciding whether to spend it, so the direction to be wrong in is the
    one that costs less than expected.
    """
    def ranged(method, url, body, headers):
        if url.endswith("/orders/whatif"):
            return {"amount": {"amount": "3,130.60 USD",
                               "commission": "1.00 - 2.50 USD"}}
        return ibkr_session(method, url, body, headers)

    b = make_ibkr(ranged)
    quote = b.preview_order(b.build_order("AAPL", "BUY", 10, "LMT", 313.06,
                                          client_order_id="DRFT_r"))
    assert quote["fee"] == pytest.approx(2.5)


def test_ibkr_refuses_to_price_an_order_the_broker_errored_on():
    from dashboard.brokers.ibkr import IbkrError

    def errored(method, url, body, headers):
        if url.endswith("/orders/whatif"):
            return {"error": "Order size exceeds the limit"}
        return ibkr_session(method, url, body, headers)

    b = make_ibkr(errored)
    with pytest.raises(IbkrError, match="refused to price"):
        b.preview_order(b.build_order("AAPL", "BUY", 10, "LMT", 313.06,
                                      client_order_id="DRFT_e"))


def test_ibkr_stops_when_another_session_has_taken_the_login():
    """
    IBKR allows one brokerage session per login, so signing into the mobile app
    takes it -- and the symptom is calls that succeed while doing nothing.
    """
    from dashboard.brokers.ibkr import IbkrError

    def competing(method, url, body, headers):
        if url.endswith("/iserver/auth/status"):
            return {"authenticated": True, "connected": True, "competing": True}
        return ibkr_session(method, url, body, headers)

    b = make_ibkr(competing)
    order = b.build_order("AAPL", "BUY", 1, "LMT", 1.0, client_order_id="DRFT_c")
    with pytest.raises(IbkrError, match="competing"):
        b.place_order(order)


def test_ibkr_stops_when_the_gateway_is_not_logged_in():
    from dashboard.brokers.ibkr import IbkrError

    def logged_out(method, url, body, headers):
        if url.endswith("/iserver/auth/status"):
            return {"authenticated": False, "connected": True, "competing": False}
        return ibkr_session(method, url, body, headers)

    b = make_ibkr(logged_out)
    order = b.build_order("AAPL", "BUY", 1, "LMT", 1.0, client_order_id="DRFT_l")
    with pytest.raises(IbkrError, match="not authenticated"):
        b.place_order(order)


def test_ibkr_opens_the_brokerage_session_before_it_sends_an_order():
    """IBKR documents GET /iserver/accounts as a prerequisite. Skipping it fails
    at placement, which is the worst place to find out."""
    seen = []

    def recording(method, url, body, headers):
        seen.append(url.split("/v1/api", 1)[-1].split("?")[0])
        return ibkr_session(method, url, body, headers)

    b = make_ibkr(recording)
    order = b.build_order("AAPL", "BUY", 1, "LMT", 1.0, client_order_id="DRFT_s")
    seen.clear()
    b.place_order(order)
    assert seen.index("/iserver/accounts") < seen.index(
        f"/iserver/account/{IBKR_ACCOUNT}/orders")


def test_ibkr_refuses_an_ambiguous_ticker():
    from dashboard.brokers.ibkr import IbkrError

    def ambiguous(method, url, body, headers):
        if "/iserver/secdef/search" in url:
            return [{"conid": "265598", "symbol": "AAPL", "description": "NASDAQ",
                     "sections": [{"secType": "STK"}]},
                    {"conid": "1234567", "symbol": "AAPL", "description": "AEB",
                     "sections": [{"secType": "STK"}]}]
        return ibkr_session(method, url, body, headers)

    with pytest.raises(IbkrError, match="ambiguous"):
        make_ibkr(ambiguous).build_order("AAPL", "BUY", 1, "LMT", 1.0)


def test_ibkr_refuses_several_accounts_rather_than_choosing_one():
    from dashboard.brokers.ibkr import IbkrBroker, IbkrError

    def two_accounts(method, url, body, headers):
        if url.endswith("/portfolio/accounts"):
            return [{"accountId": "U1", "currency": "USD"},
                    {"accountId": "U2", "currency": "EUR"}]
        return ibkr_session(method, url, body, headers)

    b = IbkrBroker(base_url="https://localhost:5000/v1/api", account_id="",
                   session=two_accounts)
    with pytest.raises(IbkrError, match="IBKR_ACCOUNT_ID"):
        b.primary_account_id()


def test_ibkr_will_not_answer_for_buying_power_without_knowing_the_units():
    """
    A ledger with no readable BASE entry means the currency the answer would be
    denominated in is unknown. Answering anyway is how a guard passes for the
    wrong reason.
    """
    from dashboard.brokers.ibkr import IbkrNotVerified

    def no_base(method, url, body, headers):
        if url.endswith("/ledger"):
            return {"USD": {"cashbalance": 4000.0}}
        return ibkr_session(method, url, body, headers)

    with pytest.raises(IbkrNotVerified, match="VERIFIER"):
        make_ibkr(no_base).buying_power("USD")


def test_ibkr_marks_an_undocumented_summary_field_rather_than_returning_zero():
    from dashboard.brokers.ibkr import IbkrNotVerified

    def odd_summary(method, url, body, headers):
        if url.endswith("/summary"):
            return {"someFieldWeHaveNeverSeen": {"amount": 1.0}}
        return ibkr_session(method, url, body, headers)

    with pytest.raises(IbkrNotVerified, match="VERIFIER"):
        make_ibkr(odd_summary).buying_power("USD")


def test_ibkr_calls_an_unknown_environment_live_rather_than_paper(monkeypatch):
    """
    The failure directions are not symmetric. Calling a live account PAPER
    invites someone to approve an order they would have refused.
    """
    from dashboard.brokers.ibkr import IbkrBroker
    monkeypatch.delenv("IBKR_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("IBKR_ENVIRONMENT", raising=False)
    assert IbkrBroker(account_id="", session=ibkr_session).environment_label() == "LIVE"
    assert IbkrBroker(account_id="DU7654321").environment_label() == "PAPER"
    assert IbkrBroker(account_id="U1234567").environment_label() == "LIVE"


def test_ibkr_does_not_disable_tls_verification_on_its_own(monkeypatch):
    """
    The Client Portal Gateway serves a self-signed certificate, and the usual
    fix is a blanket verify=False. That is a decision about the connection
    carrying your orders, so it takes an explicit opt-in and says which one is
    in force.
    """
    import ssl
    from dashboard.brokers.ibkr import IbkrBroker

    monkeypatch.delenv("IBKR_TLS_INSECURE", raising=False)
    monkeypatch.delenv("IBKR_CACERT", raising=False)
    ctx = IbkrBroker(account_id=IBKR_ACCOUNT)._ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname

    monkeypatch.setenv("IBKR_TLS_INSECURE", "1")
    opted_in = IbkrBroker(account_id=IBKR_ACCOUNT)._ssl_context()
    assert opted_in.verify_mode == ssl.CERT_NONE


def test_ibkr_hits_the_documented_paths():
    """Pins the adapter to the endpoints it was written against."""
    seen = []

    def recording(method, url, body, headers):
        seen.append((method, url.split("/v1/api", 1)[-1].split("?")[0]))
        return ibkr_session(method, url, body, headers)

    b = make_ibkr(recording)
    order = b.build_order("AAPL", "BUY", 1, "LMT", 1.0, client_order_id="DRFT_p")
    b.preview_order(order)
    b.place_order(order)
    b.positions()
    b.buying_power("USD")

    paths = [p for _, p in seen]
    for expected in ("/iserver/secdef/search",
                     f"/iserver/account/{IBKR_ACCOUNT}/orders/whatif",
                     f"/iserver/account/{IBKR_ACCOUNT}/orders",
                     "/iserver/auth/status", "/iserver/accounts",
                     f"/portfolio/{IBKR_ACCOUNT}/positions/0",
                     f"/portfolio/{IBKR_ACCOUNT}/ledger",
                     f"/portfolio/{IBKR_ACCOUNT}/summary"):
        assert expected in paths, f"{expected} was never called; got {paths}"


def test_the_ibkr_adapter_is_not_marked_verified():
    from dashboard.brokers.ibkr import IbkrBroker
    assert IbkrBroker.verified is False, (
        "set verified = True only after a real run, alongside the evidence")


# =====================================================================
# The unverified status has to reach the reader
# =====================================================================

def test_get_data_sources_names_an_unverified_adapter(monkeypatch):
    """
    A figure from an adapter nobody has run is a different kind of figure from
    one that has placed a real order. Keeping that distinction inside the class
    helps nobody; it has to appear in the output a person or a model reads.
    """
    import finance_mcp as srv
    monkeypatch.setenv("FINANCE_BROKER", "saxo")
    monkeypatch.setattr(srv.brokers, "get", lambda name=None: make_saxo())
    out = srv.get_data_sources()
    assert "UNVERIFIED" in out
    assert "saxo" in out


def test_a_broken_adapter_is_reported_not_swallowed(monkeypatch):
    import finance_mcp as srv

    def boom(name=None):
        raise ValueError("no credentials configured")

    monkeypatch.setenv("FINANCE_BROKER", "saxo")
    monkeypatch.setattr(srv.brokers, "get", boom)
    out = srv.get_data_sources()
    assert "failed to load" in out and "no credentials" in out


def test_every_webull_only_tool_says_so_instead_of_blaming_missing_credentials(monkeypatch):
    """
    Adding an adapter to the registry is not the same as routing the execution
    tools through it. Before this guard, configuring FINANCE_BROKER=ibkr and
    calling get_open_positions failed with "Webull App Key and App Secret are
    not configured in .env" -- a true sentence about the wrong problem, which
    sends a model looking for credentials the user deliberately does not have.
    """
    import inspect

    import finance_mcp as srv
    monkeypatch.setenv("FINANCE_BROKER", "ibkr")

    for name in srv.WEBULL_ONLY_TOOLS:
        fn = getattr(srv, name)
        required = [p for p in inspect.signature(fn).parameters.values()
                    if p.default is inspect.Parameter.empty]
        args = [1.0 if p.annotation is float else "AAPL" for p in required]
        with pytest.raises(Exception) as raised:
            fn(*args)
        message = str(raised.value)
        assert "ibkr" in message, f"{name} did not name the configured broker"
        assert "Webull App Key" not in message, (
            f"{name} still blames missing Webull credentials")
        assert "broker-agnostic" in message, (
            f"{name} does not say which tools still work")


def test_no_webull_backed_tool_escapes_the_list():
    """
    The guard is only as good as the list. A ninth tool reaching for
    TradeClient without being declared would fail the old confusing way again,
    so the list is checked against the source rather than trusted.
    """
    import inspect
    import re

    import finance_mcp as srv

    source = open(srv.__file__, encoding="utf-8").read()
    declared = set(re.findall(r"@mcp\.tool\(\)\s*\n(?:@webull_backed\s*\n)?def (\w+)",
                              source))
    touches_broker = set()
    for name in declared:
        fn = getattr(srv, name, None)
        if fn is None:
            continue
        body = inspect.getsource(inspect.unwrap(fn))
        if any(marker in body for marker in
               ("TradeClient", "account_v2", "order_v3", "broker.get_")):
            touches_broker.add(name)

    assert touches_broker == set(srv.WEBULL_ONLY_TOOLS), (
        "WEBULL_ONLY_TOOLS is out of step with the code. Undeclared: "
        f"{sorted(touches_broker - set(srv.WEBULL_ONLY_TOOLS))}; "
        f"declared but broker-free: {sorted(set(srv.WEBULL_ONLY_TOOLS) - touches_broker)}")


def test_the_guard_stays_out_of_the_way_for_webull(monkeypatch):
    """It is a routing check, not a second set of credentials to satisfy."""
    import finance_mcp as srv
    monkeypatch.setenv("FINANCE_BROKER", "webull")

    called = {}

    @srv.webull_backed
    def sample():
        called["yes"] = True
        return "ok"

    assert sample() == "ok" and called


def test_the_broker_agnostic_tools_outnumber_the_webull_only_ones():
    """
    The claim the README makes. If a change inverts it the README is wrong,
    and a reader deciding whether this is usable with their broker is misled.
    """
    import re

    import finance_mcp as srv
    source = open(srv.__file__, encoding="utf-8").read()
    total = len(re.findall(r"@mcp\.tool\(\)\s*\n(?:@webull_backed\s*\n)?def \w+", source))
    assert total == 39, f"tool count changed to {total}; the README says 39"
    assert len(srv.WEBULL_ONLY_TOOLS) == 8
    readme = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "README.md"), encoding="utf-8").read()
    assert "31 of the 39" in readme, (
        "README no longer states how many tools work with any broker")


def test_the_help_wanted_document_matches_the_code():
    """
    A verification request that asks about code paths which no longer exist
    wastes the time of the person answering it.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc = open(os.path.join(root, "HELP-WANTED.md"), encoding="utf-8").read()

    referenced_by_adapter = {
        "saxo.py": ("buying_power", "cancel_order", "preview_order",
                    "positions", "build_order", "resolve_uic"),
        "ibkr.py": ("buying_power", "preview_order", "positions",
                    "build_order", "resolve_conid", "order_id_for"),
    }
    for filename, names in referenced_by_adapter.items():
        src = open(os.path.join(root, "dashboard", "brokers", filename),
                   encoding="utf-8").read()
        for referenced in names:
            assert referenced in doc, f"HELP-WANTED does not mention {referenced}"
            assert f"def {referenced}(" in src, f"{referenced} no longer exists in {filename}"

    for script in ("verify_saxo.py", "verify_ibkr.py"):
        assert os.path.exists(os.path.join(root, "tests", script)), (
            f"HELP-WANTED tells people to run tests/{script}")


@pytest.mark.parametrize("script", ["verify_saxo.py", "verify_ibkr.py"])
def test_the_verification_scripts_cannot_place_an_order(script):
    """
    They are handed to strangers to run against their own brokerage account.
    They read, and they preview, which both brokers document as non-binding.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "tests", script), encoding="utf-8").read()
    assert "place_order" not in src
    assert "cancel_order" not in src
    assert "confirm_order" not in src
    assert "READ ONLY" in src
