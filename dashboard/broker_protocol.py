"""
What a broker has to be able to do, expressed once.

This exists because the second broker is where you find out which parts of the
first one were the broker and which were Webull. The interface below is the
answer for two of them; it will be wrong somewhere for the third, and that is
the point of writing it down rather than leaving it implied.

Three rules the protocol enforces on every implementation:

  * **Preview is not a promise.** Every broker prices an order more cheaply than
    it validates one. Webull priced a 1-share $0.01 order and then refused it at
    placement for a quantity-step rule; Saxo has a separate precheck endpoint
    precisely because pricing and accepting are different questions. So
    `preview` returns an estimate, `rule_violations` catches what we know the
    broker will refuse, and neither is treated as consent.

  * **Money is per currency.** A THB account holding US equities has a separate
    USD buying-power line, and summing them is a category error. Buying power is
    always asked for by currency and never returned as a single number.

  * **Cancel takes the id we generated.** Both brokers key cancellation on the
    client order id rather than their own, and both 404 on the other one. The
    protocol names the argument `client_order_id` so a caller cannot pass the
    wrong id and discover it during an emergency.

Normalised shapes returned to callers, so the dashboard and the MCP tools do not
branch on which broker is configured:

    Quote     {"cost": float|None, "fee": float|None, "currency": str}
    Placement {"order_id": str, "client_order_id": str, "raw": dict}
    Position  {"symbol": str, "quantity": float, "currency": str, ...}
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class Broker(Protocol):
    """
    The surface every broker adapter provides.

    `runtime_checkable` so a test can assert an adapter satisfies it without
    importing the adapter's dependencies. Note that this only checks method
    *names* -- the conformance tests check behaviour, which is the part that
    actually differs.
    """

    #: Short identifier used in output and config, e.g. "webull", "saxo".
    name: str

    #: False until someone has run this adapter against the real API and said
    #: so. Anything unverified must announce itself in tool output rather than
    #: read like the paths that have been exercised for real.
    verified: bool

    def environment_label(self) -> str:
        """"LIVE" or "PAPER" -- the word a human sees before approving."""
        ...

    def primary_account_id(self) -> str:
        """
        The account every other call is scoped to.

        Must refuse rather than guess when a login has several accounts:
        picking the first one silently routes orders to whichever the API
        happened to list first.
        """
        ...

    def buying_power(self, currency: str = "USD") -> float:
        """Available buying power in one currency. Raises if that line is absent."""
        ...

    def positions(self) -> list:
        """Open positions, normalised. Empty list for a flat account."""
        ...

    def position_quantity(self, symbol: str) -> float:
        """Shares held of one symbol; 0.0 when flat."""
        ...

    def build_order(self, symbol: str, action: str, quantity, order_type: str = "LMT",
                    limit_price=None, client_order_id: str = None) -> dict:
        """
        A broker-native order payload. Raises ValueError on anything unsendable
        -- an order that cannot be constructed should never reach a human queue.
        """
        ...

    def rule_violations(self, order: dict) -> list:
        """
        Rules this broker enforces at placement but not at preview, as plain
        sentences. Empty when clean.

        Advisory, never authoritative: the broker remains the decider. This
        exists so a common refusal arrives before someone approves an order
        rather than as an opaque error code afterwards.
        """
        ...

    def preview_order(self, order: dict) -> dict:
        """Ask the broker to price the order. Non-binding. Returns a Quote."""
        ...

    def place_order(self, order: dict) -> dict:
        """Submit for execution. Callers must preview first. Returns a Placement."""
        ...

    def cancel_order(self, client_order_id: str) -> dict:
        """Cancel a working order by the id we generated for it."""
        ...


def describe(broker) -> str:
    """
    One line naming the broker and how much of it has been proven, for anywhere
    a human or a model reads output that came from it.
    """
    label = getattr(broker, "environment_label", lambda: "?")()
    if getattr(broker, "verified", False):
        return f"{broker.name} ({label})"
    return (f"{broker.name} ({label}) — UNVERIFIED: this adapter has never been "
            "run against the live API. Treat every figure as unconfirmed.")
