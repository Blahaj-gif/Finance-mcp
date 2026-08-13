"""
A tool that can never work must not be offered.

Capability gating already hid tools a broker's API does not serve. It could not
see the commoner case: no broker at all. Every adapter constructs lazily so that
listing tools opens no socket, so `WebullBroker()` succeeds perfectly against an
empty `.env`, and the gate was keyed on *which broker is configured* rather than
on *whether it can be used*. A user with no credentials was offered four account
tools and every one failed with the same missing-key error.

The rule these pin down: absent credentials hide the tools, unknowable
credentials do not. The two mistakes do not cost the same. Offering a tool that
fails hands back an error naming the fix; hiding one that would have worked
leaves no way to find out it existed.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import capabilities
from dashboard import webull_client as wc
from dashboard.brokers.ibkr import IbkrBroker
from dashboard.brokers.saxo import SaxoBroker
from dashboard.brokers.webull import WebullBroker


# --------------------------------------------------------------------------
# The gate itself
# --------------------------------------------------------------------------

class _Stub:
    name = "stub"
    CAPABILITIES = frozenset((capabilities.ACCOUNTS, capabilities.POSITIONS))

    def __init__(self, answer, hint=None):
        self._answer = answer
        if hint is not None:
            self.credentials_hint = hint

    def credentials_present(self):
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def test_absent_credentials_offer_nothing():
    assert capabilities.effective(_Stub(False)) == frozenset()


def test_present_credentials_offer_what_the_adapter_declares():
    assert capabilities.effective(_Stub(True)) == _Stub.CAPABILITIES


def test_unknowable_credentials_fail_open():
    """
    `None` is not `False`. IBKR's gateway holds the session after a browser
    login and takes no token, so an empty environment is a correct setup for
    it, and answering False would hide the tools of a working install.
    """
    assert capabilities.effective(_Stub(None)) == _Stub.CAPABILITIES


def test_an_adapter_that_does_not_answer_at_all_fails_open():
    """Adapters predating this method keep every tool they declare."""
    class Old:
        name = "old"
        CAPABILITIES = frozenset((capabilities.ACCOUNTS,))

    assert capabilities.effective(Old()) == Old.CAPABILITIES


def test_a_broken_credential_check_fails_open():
    """A bug in the check must not silently delete the tool surface."""
    assert capabilities.effective(_Stub(RuntimeError("boom"))) == _Stub.CAPABILITIES


def test_the_reason_names_the_variable_rather_than_the_documentation():
    """
    "No credentials configured" sends someone to the README. A variable name
    sends them to the line they have to write.
    """
    reason = capabilities.missing_credentials(
        _Stub(False, hint="WEBULL_APP_KEY and WEBULL_APP_SECRET are not set"))
    assert "WEBULL_APP_KEY" in reason


def test_a_configured_broker_reports_no_reason():
    assert capabilities.missing_credentials(_Stub(True)) is None


# --------------------------------------------------------------------------
# Each adapter's own answer
# --------------------------------------------------------------------------

def test_webull_reads_the_module_attributes_not_the_environment(monkeypatch):
    """
    `.env` is loaded into these once at import, so they are the settled answer
    rather than whichever of two sources happened to win.
    """
    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "")
    assert WebullBroker().credentials_present() is False

    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "k")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "s")
    assert WebullBroker().credentials_present() is True


def test_webull_needs_both_halves(monkeypatch):
    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "k")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "")
    assert WebullBroker().credentials_present() is False


def test_saxo_is_configured_by_a_token(monkeypatch):
    monkeypatch.delenv("SAXO_ACCESS_TOKEN", raising=False)
    assert SaxoBroker().credentials_present() is False
    assert SaxoBroker(token="t").credentials_present() is True


def test_saxo_does_not_confuse_an_expired_token_with_a_missing_one():
    """
    Saxo's tokens die in twenty minutes on sim. A dead token is a runtime
    failure with a clear message; hiding the tools instead would make an
    hourly event look like a configuration change.
    """
    assert SaxoBroker(token="long-expired").credentials_present() is True


def test_the_ibkr_gateway_is_never_called_unconfigured(monkeypatch):
    monkeypatch.delenv("IBKR_ACCESS_TOKEN", raising=False)
    assert IbkrBroker(base_url="https://localhost:5000/v1/api"
                      ).credentials_present() is None


def test_a_gateway_on_another_host_is_still_a_gateway(monkeypatch):
    """
    The rule is keyed on IBKR's own hostname, not on "differs from the default
    URL". Running the gateway headless on another box or port is an ordinary
    setup, and it must not be mistaken for a misconfigured hosted deployment.
    """
    monkeypatch.delenv("IBKR_ACCESS_TOKEN", raising=False)
    for url in ("https://10.0.0.4:5000/v1/api",
                "https://gateway.internal:8443/v1/api",
                "https://localhost:7496/v1/api"):
        assert IbkrBroker(base_url=url).credentials_present() is None, url


def test_ibkrs_hosted_endpoint_without_a_token_is_decidably_unconfigured(monkeypatch):
    monkeypatch.delenv("IBKR_ACCESS_TOKEN", raising=False)
    assert IbkrBroker(base_url="https://api.ibkr.com/v1/api"
                      ).credentials_present() is False
    # With a token it is back to unknowable rather than proven good -- whether
    # the token is live costs a request, and this must not make one.
    assert IbkrBroker(base_url="https://api.ibkr.com/v1/api",
                      access_token="t").credentials_present() is None


# --------------------------------------------------------------------------
# What the server does with it
# --------------------------------------------------------------------------

def test_no_broker_tool_registers_without_credentials(monkeypatch):
    """
    The measurement that started this: get_account_info, get_open_positions,
    get_open_orders and get_portfolio_risk were all offered to an environment
    holding nothing, and all four failed identically when called.
    """
    import finance_mcp as srv

    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "")
    assert srv._active_capabilities() == frozenset()


def test_credentials_restore_the_full_surface(monkeypatch):
    import finance_mcp as srv

    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "k")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "s")
    assert capabilities.GATING <= srv._active_capabilities()


def test_summary_says_unconfigured_rather_than_listing_nothing():
    """
    "Capabilities: none" reads as a broker that cannot do anything. The truth
    is a broker that has not been asked yet, and the difference is one env var.
    """
    text = capabilities.summary(_Stub(False, hint="WEBULL_APP_KEY is not set"))
    assert "not configured" in text
    assert "WEBULL_APP_KEY" in text
    assert "Capabilities" not in text


# --------------------------------------------------------------------------
# The price feed
# --------------------------------------------------------------------------

def test_a_credential_less_install_does_not_try_the_broker_first(monkeypatch):
    """
    It is not a fallback if the first source could never have worked. Every
    price lookup paid for a round trip that was going to fail, and printed a
    line about the feed failing to someone whose only mistake was not having
    an account.
    """
    monkeypatch.setenv("FINANCE_BROKER", "webull")
    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "")
    labels = [label for _, label in wc._price_sources("AAPL", "D", 10)]
    assert labels == ["Yahoo Finance"]


def test_a_configured_install_still_leads_with_the_broker(monkeypatch):
    monkeypatch.setenv("FINANCE_BROKER", "webull")
    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "k")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "s")
    labels = [label for _, label in wc._price_sources("AAPL", "D", 10)]
    assert labels == ["Webull OpenAPI", "Yahoo Finance (Fallback)"]


def test_the_public_feed_is_not_called_a_degradation_when_it_is_the_feed():
    """
    Someone who never connected a broker was told on every price that "the
    primary Webull feed did not serve this request" -- an alarm about the
    absence of something they had not asked for.
    """
    banner = wc.fallback_warning("Yahoo Finance")
    assert "Warning" not in banner
    assert "did not serve" not in banner
    assert "No broker is configured" in banner


def test_a_real_substitution_is_still_a_warning():
    banner = wc.fallback_warning("Yahoo Finance (Fallback)")
    assert "Warning" in banner
    assert "did not serve" in banner


def test_a_brokers_own_feed_is_not_warned_about():
    """
    A Saxo user served by Saxo used to be told the Webull feed had failed --
    about a broker they never configured.
    """
    assert wc.fallback_warning("SAXO broker feed") == ""
