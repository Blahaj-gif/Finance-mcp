"""
Webull adapter. The reference implementation.

This is the only adapter that has been run against a live account: a real order
was drafted, previewed, placed, watched resting, and cancelled. Everything the
protocol asserts about broker behaviour was learned here, including the two
things that cost the most to find --

  * cancel_order lives on order_v3. order_v2 documents itself as HK/US only and
    returns 404 SDK.UnknownServerError everywhere else, so the emergency stop
    had never worked in this region.
  * preview prices an order without running every placement rule. A 1-share
    $0.01 order priced cleanly at $0.01 and was then refused with
    OPENAPI_ORDER_LMT_PRICE_QTY_STEP_1000.

The logic itself stays in dashboard/broker.py; this is the protocol face over it,
so nothing that already worked had to be rewritten to gain an interface.
"""
from dashboard import broker as _wb
from dashboard import capabilities as _cap
from dashboard import webull_client as _wc


class WebullBroker:
    name = "webull"
    verified = True          # exercised end to end against a live account

    #: What this adapter implements. What the regional entity serves, and what
    #: this account is entitled to buy, are two further questions the code
    #: cannot answer -- see dashboard/capabilities.py.
    CAPABILITIES = frozenset((
        _cap.ACCOUNTS, _cap.POSITIONS, _cap.BUYING_POWER, _cap.OPEN_ORDERS,
        _cap.PREVIEW_ORDER, _cap.PLACE_ORDER, _cap.CANCEL_ORDER,
        _cap.HISTORY_BARS))

    #: What to tell someone whose environment is empty. Named, not generic:
    #: "no credentials" sends people to the README, a variable name sends them
    #: to the line they have to write.
    credentials_hint = ("WEBULL_APP_KEY and WEBULL_APP_SECRET are not set in "
                        ".env or the environment")

    def __init__(self):
        self._trade_client = None
        self._account_id = None

    def credentials_present(self) -> bool:
        """
        Whether this adapter has the secrets to authenticate with. Offline.

        Reads the module attributes rather than `os.getenv` for two reasons: the
        `.env` file is loaded into them once at import, so they are the settled
        answer rather than whichever of two sources happened to win; and a test
        can substitute them without touching the process environment.

        True is not a claim that the keys are *valid* -- finding that out costs
        a signed request, and this is called while listing tools.
        """
        return bool(_wc.WEBULL_APP_KEY and _wc.WEBULL_APP_SECRET)

    # -- plumbing ---------------------------------------------------------
    def _client(self):
        if self._trade_client is None:
            from webull.trade.trade_client import TradeClient
            self._trade_client = TradeClient(_wc.get_api_client())
        return self._trade_client

    def environment_label(self) -> str:
        return _wc.environment_label()

    def primary_account_id(self) -> str:
        if self._account_id is None:
            self._account_id = _wb.get_primary_account_id(self._client())
        return self._account_id

    # -- account ----------------------------------------------------------
    def _balance(self):
        return _wc.unwrap(_wc.call_webull(
            self._client().account_v2.get_account_balance, self.primary_account_id()))

    def accounts(self) -> list:
        out = []
        for a in _wb.list_accounts(self._client()):
            if not isinstance(a, dict):
                continue
            out.append({
                "id": str(a.get("account_id") or a.get("accountId") or ""),
                "currency": str(a.get("currency") or "").upper(),
                "label": str(a.get("account_type") or a.get("accountType") or ""),
                "raw": a,
            })
        return out

    def buying_power(self, currency: str = "USD") -> float:
        return _wb.get_buying_power(self._balance(), currency)

    def positions(self) -> list:
        raw = _wc.unwrap(_wc.call_webull(
            self._client().account_v2.get_account_position, self.primary_account_id()))
        out = []
        for p in raw or []:
            if not isinstance(p, dict):
                continue
            out.append({
                "symbol": p.get("symbol") or (p.get("ticker") or {}).get("symbol") or "",
                "quantity": float(p.get("quantity", p.get("position", 0)) or 0),
                "cost": float(p.get("cost_price", p.get("costPrice", 0)) or 0),
                "last": float(p.get("last_price", p.get("lastPrice", 0)) or 0),
                "currency": (p.get("currency") or "").upper(),
                "raw": p,
            })
        return out

    def position_quantity(self, symbol: str) -> float:
        raw = _wc.unwrap(_wc.call_webull(
            self._client().account_v2.get_account_position, self.primary_account_id()))
        return _wb.get_position_quantity(raw, symbol)

    # -- orders -----------------------------------------------------------
    def build_order(self, symbol, action, quantity, order_type="LMT",
                    limit_price=None, client_order_id=None) -> dict:
        return _wb.build_order(symbol=symbol, action=action, quantity=quantity,
                               order_type=order_type, limit_price=limit_price,
                               client_order_id=client_order_id)

    def rule_violations(self, order: dict, rules: dict = None) -> list:
        return _wb.order_rule_violations(order, rules)

    def contract_rules(self, symbol: str) -> dict:
        """
        Not fetched, and not faked either.

        The SDK offers get_trade_instrument_detail, which takes an
        instrument_id rather than a ticker; the catalogue call that would map
        one to the other returned SDK.UnknownServerError when probed. Why is
        not established -- entity, entitlement and a bad call all produce
        refusals that read alike -- and the honest response to an unmapped
        instrument is not to invent its tick size. The one rule this adapter
        does know is published, hardcoded, and applied by rule_violations
        without pretending to have come from the API.
        """
        raise NotImplementedError(
            "Webull's instrument catalogue did not answer when probed "
            "(SDK.UnknownServerError), so there is no reliable ticker -> "
            "instrument_id path and tick and lot sizes are not fetched. "
            "rule_violations still applies the published quantity-step rule.")

    def history_bars(self, symbol: str, interval: str = "D", count: int = 200):
        """
        Webull's own bars. This is the feed fetch_data has always used; naming
        it here means the price path is the protocol's rather than an exception
        beside it.
        """
        return _wc.get_webull_data(symbol, interval, count)

    def preview_order(self, order: dict) -> dict:
        quote = _wb.preview_order(self._client(), self.primary_account_id(), order)
        return {
            "cost": _as_float(quote.get("estimated_cost")),
            "fee": _as_float(quote.get("estimated_transaction_fee")),
            "currency": (quote.get("currency") or "USD").upper(),
            "raw": quote,
        }

    def place_order(self, order: dict) -> dict:
        res = _wb.place_order(self._client(), self.primary_account_id(), order)
        return {
            "order_id": str(res.get("order_id", "")),
            "client_order_id": str(res.get("client_order_id", order.get("client_order_id", ""))),
            "raw": res,
        }

    def open_orders(self) -> list:
        raw = _wc.unwrap(_wc.call_webull(
            self._client().order_v3.get_order_open, self.primary_account_id()))
        out = []
        for o in raw or []:
            if not isinstance(o, dict):
                continue
            # Webull nests the instrument leg; a stock order has exactly one.
            leg = (o.get("items") or [{}])[0] if isinstance(o.get("items"), list) else {}
            out.append({
                "order_id": str(o.get("order_id") or o.get("orderId") or ""),
                "client_order_id": str(o.get("client_order_id")
                                       or o.get("clientOrderId") or ""),
                "symbol": str(o.get("symbol") or leg.get("symbol") or ""),
                "action": str(o.get("side") or leg.get("side") or "").upper(),
                "quantity": _as_float(o.get("quantity", leg.get("quantity"))) or 0.0,
                "filled": _as_float(o.get("filled_quantity",
                                          o.get("filledQuantity"))) or 0.0,
                "limit_price": _as_float(o.get("limit_price", o.get("limitPrice"))),
                "status": str(o.get("order_status") or o.get("status") or ""),
                "raw": o,
            })
        return out

    def confirm_order(self, reply_id: str, confirmed: bool = True) -> dict:
        """
        Webull accepts or refuses an order outright; it never asks a question
        back. Saying so is better than a no-op that reports success for
        something that did not happen.
        """
        raise NotImplementedError(
            "Webull does not ask for confirmation after placement — it accepts "
            "or rejects. Nothing here needs answering.")

    def cancel_order(self, client_order_id: str) -> dict:
        res = _wb.cancel_order(self._client(), self.primary_account_id(), client_order_id)
        return {
            "order_id": str(res.get("order_id", "")),
            "client_order_id": str(res.get("client_order_id", client_order_id)),
            "raw": res,
        }


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
