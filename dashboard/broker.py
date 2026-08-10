"""
The broker: accounts, buying power, positions, and the order lifecycle.

Split out of webull_client, which had grown into a market-data client and a
trading client fused into one module. They are not the same thing and they do
not fail the same way: a price feed degrades to a fallback and announces it,
while an order path must refuse rather than substitute. Keeping both behind one
import made it easy to reach for the wrong one -- which is how cancel_order
ended up on order_v2, an API generation that does not serve this region, and
stayed broken until a live order needed cancelling.

The shared plumbing (the signed API client, the rate limiter, the redacting log
filter) stays in webull_client and is imported here. Only the trading surface
lives in this module.
"""
import os

try:
    from dashboard.webull_client import call_webull, unwrap
except ImportError:  # imported as a top-level module from dashboard/
    from webull_client import call_webull, unwrap


# Pin a specific account when the login has more than one. Without this, a
# second account appearing would silently change which account gets traded.
WEBULL_ACCOUNT_ID = os.getenv("WEBULL_ACCOUNT_ID", "").strip()


def list_accounts(trade_client) -> list:
    """All accounts visible to these credentials, normalised to a list of dicts."""
    accounts = unwrap(call_webull(trade_client.account_v2.get_account_list))
    if isinstance(accounts, dict):
        accounts = accounts.get("data", accounts.get("accounts", []))
    return accounts or []


def _account_id_of(account) -> str:
    if not isinstance(account, dict):
        return ""
    return str(account.get("account_id") or account.get("accountId") or "")


def get_primary_account_id(trade_client) -> str:
    """
    Resolve the account_id every trade endpoint requires.

    The SDK's account and order methods are all account-scoped --
    get_account_position(account_id), get_account_balance(account_id),
    get_order_open(account_id). Calling them with no argument raises TypeError,
    which is why the position, balance and open-order tools never worked.

    With multiple accounts, refuse to guess: picking accounts[0] would quietly
    route orders to whichever account the API happened to list first.
    """
    accounts = list_accounts(trade_client)
    if not accounts:
        raise RuntimeError("Webull returned no accounts for these credentials")

    if WEBULL_ACCOUNT_ID:
        for acct in accounts:
            if _account_id_of(acct) == WEBULL_ACCOUNT_ID:
                return WEBULL_ACCOUNT_ID
        available = [_account_id_of(a) for a in accounts]
        raise RuntimeError(
            f"WEBULL_ACCOUNT_ID={WEBULL_ACCOUNT_ID} is not among this login's "
            f"accounts: {available}"
        )

    if len(accounts) > 1:
        described = ", ".join(
            f"{_account_id_of(a)} ({a.get('account_label') or a.get('account_type', '?')})"
            for a in accounts
        )
        raise RuntimeError(
            f"This login has {len(accounts)} accounts and none is pinned. "
            f"Set WEBULL_ACCOUNT_ID in .env to one of: {described}"
        )

    account_id = _account_id_of(accounts[0])
    if not account_id:
        raise RuntimeError(f"Could not find an account_id in the account list: {accounts[0]!r}")
    return account_id


def get_buying_power(balance, currency: str = "USD") -> float:
    """
    Extract buying power for a currency from an account balance payload.

    Buying power is reported per currency under `account_currency_assets`; a
    THB-denominated account holding US equities has a separate USD line. The
    old code read a top-level `buyingPower` key that does not exist in this
    API, so it always saw 0.
    """
    if not isinstance(balance, dict):
        raise ValueError(f"Unexpected balance payload: {type(balance).__name__}")

    for asset in balance.get("account_currency_assets", []) or []:
        if str(asset.get("currency", "")).upper() == currency.upper():
            return float(asset.get("buying_power", 0) or 0)

    raise ValueError(
        f"No {currency} buying power line in the account balance "
        f"(currencies present: {[a.get('currency') for a in balance.get('account_currency_assets', []) or []]})"
    )


def get_position_quantity(positions, symbol: str) -> float:
    """Shares held of `symbol`, from a get_account_position payload."""
    if not isinstance(positions, list):
        return 0.0
    for p in positions:
        if not isinstance(p, dict):
            continue
        p_symbol = p.get("symbol") or (p.get("ticker") or {}).get("symbol") or ""
        if str(p_symbol).upper() == symbol.upper():
            return float(p.get("quantity", p.get("position", p.get("assetQuantity", 0))) or 0)
    return 0.0


# =====================================================================
# ORDER CONSTRUCTION / PREVIEW / PLACEMENT
# =====================================================================
# The order payload below was validated field-by-field against the live
# Webull TH preview endpoint. Notes that cost real time to discover:
#   * order_v3 is the correct API for TH. order_v2.place_order is documented
#     as HK/US only, and `place_stock_order` (which the dashboard called)
#     does not exist on this SDK at all.
#   * `entrust_type: "QTY"` is required. Omitting it returns an opaque
#     "System error", not a helpful parameter error.
#   * `support_trading_session` is required; "N" (regular session) is valid.
#   * Preview does NOT enforce buying power -- it happily prices a $450,000
#     order against a $333 account. Our own pre-trade checks are load-bearing.

_ORDER_TYPE_ALIASES = {
    "LMT": "LIMIT", "LIMIT": "LIMIT",
    "MKT": "MARKET", "MARKET": "MARKET",
    "STP": "STOP", "STOP": "STOP",
}


def build_order(symbol: str, action: str, quantity, order_type: str = "LMT",
                limit_price=None, market: str = "US", instrument_type: str = "EQUITY",
                time_in_force: str = "DAY", client_order_id: str = None) -> dict:
    """Build a Webull v3 order payload. Raises ValueError on anything unsendable."""
    import uuid

    norm_type = _ORDER_TYPE_ALIASES.get(str(order_type).upper())
    if not norm_type:
        raise ValueError(f"Unsupported order_type {order_type!r}; use LMT/LIMIT or MKT/MARKET")

    side = str(action).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"action must be BUY or SELL, got {action!r}")

    qty = float(quantity)
    if qty <= 0:
        raise ValueError(f"quantity must be positive, got {quantity!r}")

    order = {
        "client_order_id": client_order_id or uuid.uuid4().hex,
        "symbol": str(symbol).upper(),
        "instrument_type": instrument_type,
        "market": market,
        "order_type": norm_type,
        "quantity": str(quantity),
        "side": side,
        "time_in_force": str(time_in_force).upper(),
        "support_trading_session": "N",
        "entrust_type": "QTY",
    }

    if norm_type == "LIMIT":
        if limit_price is None:
            raise ValueError("A LIMIT order requires a limit_price")
        order["limit_price"] = f"{float(limit_price):.2f}"

    return order


def preview_order(trade_client, account_id: str, order: dict) -> dict:
    """
    Ask the broker to price and validate an order without placing it.

    Non-binding, and a *weaker* check than it looks. Verified live: a BUY of 1
    ZETA at $0.01 previewed cleanly, returning a $0.01 cost and a $0.00 fee, and
    was then refused at submission with OPENAPI_ORDER_LMT_PRICE_QTY_STEP_1000 --
    a sub-$0.10 limit requires quantity above 1000. Preview prices an order; it
    does not run every rule the placement endpoint runs.

    So "the broker previewed it" means the order is priceable, not that it is
    acceptable. It remains the right gate -- an order the broker will not price
    should never be sent -- but a submission can still be refused after it, and
    the UI has to be able to say so rather than treating preview as a promise.
    """
    return unwrap(call_webull(trade_client.order_v3.preview_order, account_id, [order]))


def place_order(trade_client, account_id: str, order: dict) -> dict:
    """Submit an order for execution. Callers must preview first."""
    return unwrap(call_webull(trade_client.order_v3.place_order, account_id, [order]))


def cancel_order(trade_client, account_id: str, client_order_id: str) -> dict:
    """
    Cancel a working order by the client id we generated for it.

    Uses order_v3, like preview and place. The MCP tool used order_v2, whose own
    docstring says it covers "Webull HK and Webull US" only -- so every cancel
    from a TH account returned 404 SDK.UnknownServerError. The emergency stop
    had never worked in this region, which only surfaced when a live test order
    needed cancelling. v3 lists TH explicitly.

    Takes the *client* order id (`DRFT_9a32c8d5`), not the broker's `order_id`
    (`037VACVVDO80O0KCJR84000000`); passing the latter also 404s.
    """
    return unwrap(call_webull(trade_client.order_v3.cancel_order,
                              account_id, client_order_id))


# =====================================================================
# PRE-FLIGHT ORDER RULES
# =====================================================================
# Rules the broker enforces at *placement* but not at preview, learned the way
# they are usually learned: from a 417 after a clean preview. A BUY of 1 ZETA at
# $0.01 previewed at $0.01 cost and $0.00 fee, then came back
# OPENAPI_ORDER_LMT_PRICE_QTY_STEP_1000 -- "the latest limit order ranges from
# 0.01~0.099 and the quantity should be greater than 1000".
#
# Checking locally does not make the broker's answer less authoritative. It
# turns an opaque code arriving after approval into a sentence shown before it,
# which is the difference between a person understanding why their order was
# refused and assuming the tool is broken.

# (max_price_exclusive, minimum_quantity). Webull scales the lot size down as
# the limit price falls, so sub-penny names require thousands of shares.
PENNY_QUANTITY_STEPS = (
    (0.10, 1000),
)


def order_rule_violations(order: dict) -> list:
    """
    Broker rules this order would break, as plain sentences. Empty when clean.

    Advisory, never authoritative: the broker remains the decider, and a rule it
    applies that is not listed here will still refuse the order. This exists so
    the common refusals arrive before a human approves rather than after.
    """
    problems = []

    try:
        qty = float(order.get("quantity", 0))
    except (TypeError, ValueError):
        return ["Quantity is not a number."]

    if qty <= 0:
        problems.append("Quantity must be greater than zero.")

    limit_raw = order.get("limit_price")
    if str(order.get("order_type", "")).upper() == "LIMIT" and limit_raw is not None:
        try:
            limit = float(limit_raw)
        except (TypeError, ValueError):
            return problems + ["Limit price is not a number."]

        if limit <= 0:
            problems.append("Limit price must be greater than zero.")
        else:
            for max_price, min_qty in PENNY_QUANTITY_STEPS:
                if limit < max_price and qty <= min_qty:
                    problems.append(
                        f"Webull requires more than {min_qty:,} shares for a limit "
                        f"price under ${max_price:.2f}; this order is {qty:,.0f} "
                        f"at ${limit:,.2f}. Raise the quantity or the price.")
                    break

    if str(order.get("side", "")).upper() not in ("BUY", "SELL"):
        problems.append(f"Side must be BUY or SELL, not {order.get('side')!r}.")

    return problems
