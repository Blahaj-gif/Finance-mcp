"""
One suite, every broker.

The point of a broker protocol is not that two classes share method names --
it is that the guarantees the execution path depends on hold for both. Those
guarantees were all learned the expensive way from Webull, and each test below
names the incident that produced it.

Saxo is driven through an injected session returning documented response shapes.
That proves the adapter is wired to the API it was written against; it proves
nothing about what Saxo actually returns. See HELP-WANTED.md.
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


ADAPTERS = {"webull": make_webull, "saxo": make_saxo}


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


def test_the_registry_exposes_both(monkeypatch):
    assert set(brokers.available()) >= {"webull", "saxo"}
    monkeypatch.setenv("FINANCE_BROKER", "saxo")
    assert brokers.active_name() == "saxo"


def test_an_unknown_broker_is_refused_by_name():
    with pytest.raises(ValueError, match="Unknown broker"):
        brokers.get("interactive-brokers")


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


def test_the_help_wanted_document_matches_the_code():
    """
    A verification request that asks about code paths which no longer exist
    wastes the time of the person answering it.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc = open(os.path.join(root, "HELP-WANTED.md"), encoding="utf-8").read()
    src = open(os.path.join(root, "dashboard", "brokers", "saxo.py"), encoding="utf-8").read()

    for referenced in ("buying_power", "cancel_order", "preview_order",
                       "positions", "build_order", "resolve_uic"):
        assert referenced in doc, f"HELP-WANTED does not mention {referenced}"
        assert f"def {referenced}(" in src, f"{referenced} no longer exists in saxo.py"

    assert os.path.exists(os.path.join(root, "tests", "verify_saxo.py")), (
        "HELP-WANTED tells people to run tests/verify_saxo.py")


def test_the_verification_script_cannot_place_an_order():
    """
    It is handed to strangers to run against their own brokerage account. It
    reads, and it prechecks, which Saxo documents as non-binding.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "tests", "verify_saxo.py"), encoding="utf-8").read()
    assert "place_order" not in src
    assert "cancel_order" not in src
    assert "READ ONLY" in src
