"""
Broker adapters behind one interface (see dashboard/broker_protocol.py).

`webull` is the reference implementation and the only one exercised against a
live account -- placed, rested and cancelled a real order. `saxo` and `ibkr` are
built from their published REST documentation and have never been run against
either API; both say so in every piece of output they produce.
"""
import os

REGISTRY = {}


def register(adapter_name, factory):
    REGISTRY[adapter_name] = factory


def available() -> list:
    return sorted(REGISTRY)


def active_name() -> str:
    """Which broker this process is configured for."""
    return os.getenv("FINANCE_BROKER", "webull").strip().lower()


def get(adapter_name=None):
    """
    Build the configured broker adapter.

    Imports are deferred to the factory so that configuring Saxo does not
    require the Webull SDK to be installed, or the reverse.
    """
    adapter_name = (adapter_name or active_name()).lower()
    if adapter_name not in REGISTRY:
        raise ValueError(
            f"Unknown broker {adapter_name!r}. Available: {', '.join(available())}. "
            "Set FINANCE_BROKER to one of those.")
    return REGISTRY[adapter_name]()


def _register_builtins():
    def webull_factory():
        from dashboard.brokers.webull import WebullBroker
        return WebullBroker()

    def saxo_factory():
        from dashboard.brokers.saxo import SaxoBroker
        return SaxoBroker()

    def ibkr_factory():
        from dashboard.brokers.ibkr import IbkrBroker
        return IbkrBroker()

    register("webull", webull_factory)
    register("saxo", saxo_factory)
    register("ibkr", ibkr_factory)


_register_builtins()
