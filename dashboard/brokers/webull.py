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
from dashboard import webull_client as _wc


class WebullBroker:
    name = "webull"
    verified = True          # exercised end to end against a live account

    def __init__(self):
        self._trade_client = None
        self._account_id = None

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

    def rule_violations(self, order: dict) -> list:
        return _wb.order_rule_violations(order)

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
