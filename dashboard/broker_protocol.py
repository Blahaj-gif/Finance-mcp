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

  * **Cancel takes the id we generated.** Webull keys cancellation on the client
    order id and 404s on its own. Saxo and IBKR key it on theirs. The protocol
    names the argument `client_order_id` regardless and makes the adapter do
    whatever lookup its broker needs -- IBKR's live-orders response carries
    `order_ref`, so it can; Saxo documents no mapping, so it refuses. A caller
    holding the id it generated must never have to know which of those it got.

  * **A broker may ask before it accepts.** IBKR can answer a placement with
    warnings instead of an order -- no market data, likely to fill immediately,
    price outside a percentage constraint -- each carrying a reply id that has
    to be confirmed before anything is transmitted. Client libraries normally
    answer these from a table of canned replies. Here they raise
    `ConfirmationRequired` and go to the person who approved the order, because
    a warning addressed to a human that a program answers is not a warning.

Normalised shapes returned to callers, so the dashboard and the MCP tools do not
branch on which broker is configured:

    Quote     {"cost": float|None, "fee": float|None, "currency": str}
    Placement {"order_id": str, "client_order_id": str, "raw": dict}
    Position  {"symbol": str, "quantity": float, "currency": str, ...}
    Account   {"id": str, "currency": str, "label": str, "raw": dict}
    Order     {"order_id": str, "client_order_id": str, "symbol": str,
               "action": str, "quantity": float, "filled": float,
               "limit_price": float|None, "status": str, "raw": dict}
"""
from typing import Protocol, runtime_checkable


class ConfirmationRequired(Exception):
    """
    The broker will not accept this order until someone answers it.

    Nothing has been transmitted when this is raised. `questions` holds the
    broker's own words, to be shown unedited -- paraphrasing a risk warning is
    how it stops being one -- and `reply_id` is what `confirm_order` needs
    once a person has answered.
    """

    def __init__(self, broker: str, reply_id: str, questions,
                 client_order_id: str = "", raw=None):
        self.broker = broker
        self.reply_id = reply_id
        self.questions = list(questions or [])
        self.client_order_id = client_order_id
        self.raw = raw
        joined = " | ".join(self.questions) or "(no message text returned)"
        super().__init__(
            f"{broker} will not place this order until it is confirmed: {joined}")


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

    def credentials_present(self):
        """
        Whether this adapter has what it needs to authenticate. **Offline.**

        Optional, and optional in a load-bearing way: an adapter that does not
        implement it fails open and keeps every tool it declares. Three answers,
        not two -- True, False, or None for "cannot be known without a network
        call", which is the honest answer for a deployment whose session lives
        somewhere this process cannot see.

        Never a claim that the credentials are *valid*. Only that they exist.
        Validity costs a request, and this is called while listing tools, which
        must work with no network at all.

        See dashboard/capabilities.missing_credentials for what is done with it.
        """
        ...

    def primary_account_id(self) -> str:
        """
        The account every other call is scoped to.

        Must refuse rather than guess when a login has several accounts:
        picking the first one silently routes orders to whichever the API
        happened to list first.
        """
        ...

    def accounts(self) -> list:
        """
        Every account these credentials can see, normalised.

        Distinct from `primary_account_id`, which refuses when there is more
        than one. This is how a caller finds out what to pin.
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

    def rule_violations(self, order: dict, rules: dict = None) -> list:
        """
        Rules this broker enforces at placement but not at preview, as plain
        sentences. Empty when clean.

        Advisory, never authoritative: the broker remains the decider. This
        exists so a common refusal arrives before someone approves an order
        rather than as an opaque error code afterwards.

        Called with no `rules` it must stay **offline** -- it runs before the
        networked checks precisely so that a malformed order on a machine with
        no credentials is refused for what is wrong with it. Pass the output of
        `contract_rules` to have it check the real tick and lot sizes instead of
        the handful published in the adapter.
        """
        ...

    def contract_rules(self, symbol: str) -> dict:
        """
        What this broker will actually accept for one instrument:

            {"tick_size": float|None, "quantity_step": float|None,
             "min_quantity": float|None, "currency": str, "raw": dict}

        The expensive lesson behind this method: Webull priced a 1-share $0.01
        order cleanly through preview and then refused it at placement for a
        quantity-step rule. Preview does not run every placement rule, and the
        rules are published -- they were simply never fetched.

        Optional. An adapter that cannot fetch them must raise rather than
        return empty, and must not declare the `contract_rules` capability.
        """
        ...

    def history_bars(self, symbol: str, interval: str = "D", count: int = 200):
        """
        Historical bars from the account this adapter is already authenticated
        against, as a DataFrame with time/open/high/low/close/volume, **oldest
        first**.

        Optional, and worth more than it looks: without it a Saxo or IBKR user
        gets the Yahoo fallback for every price in every tool, having supplied
        credentials to a broker that serves bars.

        Row order is the guarantee that matters. Webull returns newest-first
        and nothing sorted it, so every indicator ran on a reversed series and
        the sector heatmap ranked the worst performers as leaders.
        """
        ...

    def preview_order(self, order: dict) -> dict:
        """Ask the broker to price the order. Non-binding. Returns a Quote."""
        ...

    def place_order(self, order: dict) -> dict:
        """
        Submit for execution. Callers must preview first. Returns a Placement.

        Raises `ConfirmationRequired` if the broker answers with questions
        rather than an order id. Nothing has been sent when it does.
        """
        ...

    def confirm_order(self, reply_id: str, confirmed: bool = True) -> dict:
        """
        Answer a `ConfirmationRequired` raised by `place_order`.

        Brokers that never ask raise NotImplementedError rather than returning
        a success that did not happen. Callers still route through here, so the
        broker that does ask is not a special case at the call site.
        """
        ...

    def open_orders(self) -> list:
        """
        Working orders, normalised. Empty list when there are none.

        Every broker here has this and none of them agreed on the shape, which
        is why it is in the protocol rather than left to each tool. It is also
        what makes cancellation usable: the id a caller must pass to
        `cancel_order` is the `client_order_id` on these rows.
        """
        ...

    def cancel_order(self, client_order_id: str) -> dict:
        """Cancel a working order by the id we generated for it."""
        ...


def draft_refusal(draft: dict, broker_name: str, environment_label: str):
    """
    Why this draft must not be submitted through this desk, or None if it may.

    A draft carries symbol and quantity, not a broker-native order, so the
    dashboard rebuilds it at approval time and nothing in the file itself would
    catch a mismatch. Two things therefore have to be checked back rather than
    trusted: which broker it was raised against, and which environment. The
    second was recorded on every draft from the start and never read, so an
    order rehearsed against the sandbox -- and cleared by pre-trade guards
    measured against sandbox money -- could be approved into the real account.

    An unlabelled draft is refused rather than assumed to match. A check that
    could not run is not a check that passed, and the remedy is cheap: cancel
    it and ask again.
    """
    drafted_for = str(draft.get("broker") or "").lower()
    if not drafted_for:
        return ("This draft does not record which broker it was raised against, so "
                "it cannot be shown to belong on this desk. Cancel it and re-draft.")
    if drafted_for != str(broker_name).lower():
        return (f"This draft was raised against {drafted_for}, and this page submits "
                f"to {broker_name}. Cancel it and re-draft, or submit it on "
                f"{drafted_for}'s own platform. Nothing has been sent.")

    drafted_in = str(draft.get("environment") or "").lower()
    if not drafted_in:
        return ("This draft does not record which environment it was raised in, so "
                f"it cannot be shown to belong to the {environment_label} account "
                "it would reach. Cancel it and re-draft.")
    if drafted_in != str(environment_label).lower():
        return (f"This draft was raised against the {drafted_in} surface and this "
                f"desk is {environment_label}. The buying-power and inventory checks "
                f"it passed were measured against the {drafted_in} account, so they "
                "say nothing about this one. Cancel it and re-draft.")
    return None


def _draft_cost(draft: dict, price_of=None):
    """
    What a draft would spend, or None if it cannot be priced without guessing.

    A limit order carries its own ceiling, so it costs nothing to price and
    needs no feed. Anything else has to be asked about, and `price_of` may
    decline -- an unpriced draft is named by the caller, never treated as free.
    """
    try:
        qty = float(draft.get("quantity") or 0)
    except (TypeError, ValueError):
        return None
    recorded = draft.get("est_notional")
    if recorded is not None:
        try:
            return float(recorded)
        except (TypeError, ValueError):
            return None
    if draft.get("limit_price"):
        return qty * float(draft["limit_price"])
    if price_of is None:
        return None
    price = price_of(str(draft.get("symbol", "")).upper())
    return qty * float(price) if price else None


def pretrade_refusal(adapter, draft: dict, pending, price_of=None):
    """
    Why this draft must not be submitted on risk grounds now, and what is
    unclear. Returns (refusal_or_None, notes).

    The buying-power and inventory checks used to run when a draft was created
    and never again, so a draft cleared on Monday could be approved on Friday
    against money that had since moved. This is that check, at the moment it
    matters.

    Three things it deliberately does NOT do, each of which would make it worse
    than no check at all:

      * It takes the adapter as an argument rather than resolving the ambient
        one. The submit path is pinned to Webull; a check that read IBKR's
        buying power and then cleared an order bound for Webull would print
        "verified" against an account the order never touches.
      * An unpriceable sibling is a note, not a refusal. At draft time that is
        an inconvenience the model routes around; at approval it would block a
        previewed, affordable, legitimate order because of an unrelated draft,
        with hand-editing the live queue as the only way out.
      * It prices a limit order from its own limit, so approving one reaches no
        price feed at all. A submit button that depends on a scraped endpoint
        being up is a submit button that fails when it is not.
    """
    notes, side = [], str(draft.get("action", "")).upper()
    others = [d for d in pending
              if d.get("draft_id") != draft.get("draft_id")
              and d.get("status") == "PENDING_APPROVAL"]

    if side == "SELL":
        held = float(adapter.position_quantity(draft.get("symbol", "")))
        spoken_for = sum(
            float(d.get("quantity") or 0) for d in others
            if str(d.get("action", "")).upper() == "SELL"
            and str(d.get("symbol", "")).upper() == str(draft.get("symbol", "")).upper())
        wanted = float(draft.get("quantity") or 0)
        if wanted + spoken_for > held:
            return (f"Inventory has moved since this was drafted: selling {wanted:g} "
                    f"with {spoken_for:g} already spoken for by other pending draft(s) "
                    f"exceeds the {held:g} share(s) held. That is a naked short.",
                    notes)
        return None, notes

    if side != "BUY":
        return None, notes

    cost = _draft_cost(draft, price_of)
    if cost is None:
        notes.append("This order could not be priced, so it was not re-checked "
                     "against buying power.")
        return None, notes

    committed = 0.0
    for other in others:
        if str(other.get("action", "")).upper() != "BUY":
            continue
        other_cost = _draft_cost(other, price_of)
        if other_cost is None:
            notes.append(
                f"Pending draft {other.get('draft_id', '?')} could not be priced, "
                "so it is not counted in the total below.")
        else:
            committed += other_cost

    power = float(adapter.buying_power("USD"))
    if cost + committed > power:
        detail = (f" and ~{committed:,.2f} is already committed by other pending "
                  f"draft(s)") if committed else ""
        return (f"Buying power has moved since this was drafted: this order needs "
                f"~{cost:,.2f}{detail}, against buying power of {power:,.2f}.",
                notes)
    return None, notes


def rebuilt_differs(previewed: dict, rebuilt: dict):
    """
    How the draft on disk now differs from the order the broker actually priced,
    or None if they are the same order.

    The submit path deliberately sends the payload built at preview time -- the
    page's own invariant is that it never submits an order the broker has not
    validated, and rebuilding at approval would defeat it. But that leaves a
    gap: the queue is a local file, and a draft edited between the preview and
    the click makes the approval card describe one order while a different one
    is already priced and ready to send. Rebuilding the draft *only to compare*
    closes it without touching what is sent.

    The comparison is over the whole payload rather than a hand-listed few
    fields, because `build_order` derives several -- time in force, the entrust
    type, the two-decimal limit normalisation -- and a snapshot of the obvious
    ones would miss exactly the changes nobody thought of.
    """
    if previewed == rebuilt:
        return None
    keys = sorted(set(previewed) | set(rebuilt))
    changed = [f"{k}: previewed {previewed.get(k)!r}, draft now says {rebuilt.get(k)!r}"
               for k in keys if previewed.get(k) != rebuilt.get(k)]
    return "; ".join(changed) if changed else None


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
