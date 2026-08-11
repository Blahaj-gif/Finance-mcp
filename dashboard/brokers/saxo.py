"""
Saxo Bank adapter — BUILT FROM DOCUMENTATION, NEVER RUN AGAINST THE API.

    ┌──────────────────────────────────────────────────────────────────────┐
    │  UNVERIFIED. No call in this file has ever reached Saxo. It is       │
    │  written against their published OpenAPI reference and nothing else. │
    │  Field names, response shapes and error behaviour are what the docs  │
    │  say, not what the API does. Every tool that uses it says so in its  │
    │  output. See HELP-WANTED.md — verification is the ask.               │
    └──────────────────────────────────────────────────────────────────────┘

Everywhere the documentation was ambiguous, this raises rather than guesses. A
wrong guess in a broker adapter is not a bug that shows up in a log; it is an
order for the wrong quantity, or a buying-power check that passes when it should
not. `SaxoNotVerified` marks each such place, and the message names what a
verifier should check.

What the docs establish (fetched from developer.saxo):

    REST      https://gateway.saxobank.com/openapi        (live)
              https://gateway.saxobank.com/sim/openapi    (simulation)
    Auth      https://live.logonvalidation.net            (live)
              https://sim.logonvalidation.net             (simulation)

    GET    /port/v1/accounts/me
    GET    /port/v1/balances?AccountKey=&ClientKey=
    GET    /port/v1/positions?AccountKey=&ClientKey=
    GET    /port/v1/netpositions?AccountKey=&ClientKey=
    POST   /trade/v2/orders                      place
    POST   /trade/v2/orders/precheck             price and validate, non-binding
    DELETE /trade/v2/orders/{OrderIds}?AccountKey=

Two structural differences from Webull that the protocol had to absorb:

  * **Instruments are numeric.** Saxo trades a Uic (a universal instrument code)
    plus an AssetType, not a ticker string. "AAPL" is not an order field; it is
    a lookup against ref/v1/instruments that returns a Uic. That lookup can
    return several matches across exchanges, so it refuses when ambiguous.

  * **Saxo prices orders through a real precheck endpoint** rather than by
    inference. Our draft/preview/approve split maps onto it directly, which is
    mild evidence the split was the right shape rather than a Webull artefact.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from dashboard.envfile import load_env
except ImportError:  # imported as a top-level module from dashboard/
    from envfile import load_env

load_env()


class SaxoNotVerified(NotImplementedError):
    """
    A path the documentation did not pin down well enough to implement blind.

    Raised instead of guessing. In a broker adapter a wrong guess is not a log
    line, it is a real order with the wrong quantity.
    """


class SaxoError(RuntimeError):
    """Saxo returned an error, or the response was not the documented shape."""


LIVE_BASE = "https://gateway.saxobank.com/openapi"
SIM_BASE = "https://gateway.saxobank.com/sim/openapi"
LIVE_AUTH = "https://live.logonvalidation.net"
SIM_AUTH = "https://sim.logonvalidation.net"

# Saxo's own vocabulary. Ours is BUY/SELL and LMT/MKT; theirs is these.
_SIDE = {"BUY": "Buy", "SELL": "Sell"}
_ORDER_TYPE = {"LMT": "Limit", "LIMIT": "Limit",
               "MKT": "Market", "MARKET": "Market",
               "STP": "Stop", "STOP": "Stop"}
_DEFAULT_ASSET_TYPE = "Stock"


class SaxoBroker:
    name = "saxo"
    #: Never set this True from a reading of the code. It means somebody ran it
    #: against the API and reported what happened.
    verified = False

    def __init__(self, token=None, environment=None, session=None):
        self.environment = (environment or os.getenv("SAXO_ENVIRONMENT", "sim")).strip().lower()
        self.base = LIVE_BASE if self.environment in ("live", "prod") else SIM_BASE
        self.auth_base = LIVE_AUTH if self.environment in ("live", "prod") else SIM_AUTH
        self._token = token or os.getenv("SAXO_ACCESS_TOKEN", "").strip()
        self._account_key = os.getenv("SAXO_ACCOUNT_KEY", "").strip()
        self._client_key = os.getenv("SAXO_CLIENT_KEY", "").strip()
        # Injectable so the conformance tests can drive the adapter without a
        # network, and so a verifier can log every exchange.
        self._session = session or self._http

    # -- transport --------------------------------------------------------
    def _http(self, method: str, url: str, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise SaxoError(f"Saxo {method} {url} -> HTTP {e.code}: {detail}") from e

    def _request(self, method: str, path: str, params=None, body=None):
        if not self._token:
            raise SaxoError(
                "SAXO_ACCESS_TOKEN is not set. A 24-hour simulation token comes "
                "from the Developer Portal; live access needs the OAuth2 code "
                "flow. Nothing is sent without one.")
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
        headers = {"Authorization": f"Bearer {self._token}",
                   "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self._session(method, url, body, headers)

    # -- identity ---------------------------------------------------------
    def environment_label(self) -> str:
        return "LIVE" if self.environment in ("live", "prod") else "PAPER"

    def primary_account_id(self) -> str:
        """
        Saxo's AccountKey, which every other call is scoped by.

        Refuses to choose when a login has several accounts, for the same reason
        Webull does: picking the first would route orders to whichever the API
        happened to list first.
        """
        if self._account_key:
            return self._account_key

        payload = self._request("GET", "/port/v1/accounts/me")
        accounts = payload.get("Data") or []
        if not accounts:
            raise SaxoError("Saxo returned no accounts for this token.")
        if len(accounts) > 1:
            described = ", ".join(
                f"{a.get('AccountKey')} ({a.get('AccountId')}, {a.get('Currency')})"
                for a in accounts)
            raise SaxoError(
                f"This login has {len(accounts)} accounts and none is pinned. "
                f"Set SAXO_ACCOUNT_KEY to one of: {described}")
        self._account_key = accounts[0].get("AccountKey", "")
        if not self._account_key:
            raise SaxoError(f"No AccountKey in the account payload: {accounts[0]!r}")
        return self._account_key

    # -- account ----------------------------------------------------------
    def buying_power(self, currency: str = "USD") -> float:
        """
        Saxo reports one balance block per account, in that account's own
        currency -- there is no per-currency array like Webull's
        account_currency_assets. So asking for a currency the account is not
        denominated in cannot be answered by conversion here; converting at an
        unstated rate is how a buying-power check silently passes.
        """
        payload = self._request("GET", "/port/v1/balances",
                                params={"AccountKey": self.primary_account_id(),
                                        "ClientKey": self._client_key})
        account_currency = (payload.get("Currency") or "").upper()
        if account_currency and account_currency != currency.upper():
            raise SaxoError(
                f"This Saxo account is denominated in {account_currency}, not "
                f"{currency.upper()}. Saxo reports a single balance block per "
                "account rather than a line per currency, and converting here "
                "at an unstated rate would make a buying-power check pass when "
                "it should not. Ask for the account currency.")

        for field in ("CashAvailableForTrading", "MarginAvailableForTrading",
                      "TotalValue"):
            if field in payload:
                try:
                    return float(payload[field])
                except (TypeError, ValueError):
                    continue
        raise SaxoNotVerified(
            "No documented buying-power field was present in the balances "
            f"response (keys: {sorted(payload)[:12]}). VERIFIER: report which "
            "field a real account returns for available buying power.")

    def positions(self) -> list:
        payload = self._request("GET", "/port/v1/netpositions",
                                params={"AccountKey": self.primary_account_id(),
                                        "ClientKey": self._client_key})
        out = []
        for row in payload.get("Data") or []:
            base = row.get("NetPositionBase") or {}
            view = row.get("NetPositionView") or {}
            out.append({
                "symbol": base.get("Symbol") or base.get("Uic") or "",
                "uic": base.get("Uic"),
                "asset_type": base.get("AssetType"),
                "quantity": _num(base.get("Amount")),
                "cost": _num(view.get("AverageOpenPrice")),
                "last": _num(view.get("CurrentPrice")),
                "currency": (view.get("ExposureCurrency")
                             or base.get("Currency") or "").upper(),
                "raw": row,
            })
        return out

    def position_quantity(self, symbol: str) -> float:
        for p in self.positions():
            if str(p["symbol"]).upper() == symbol.upper():
                return float(p["quantity"] or 0)
        return 0.0

    # -- instruments ------------------------------------------------------
    def resolve_uic(self, symbol: str, asset_type: str = _DEFAULT_ASSET_TYPE) -> int:
        """
        Ticker -> Uic. Saxo orders address a numeric instrument, not a string.

        Refuses on an ambiguous match. The same ticker exists on several
        exchanges and the difference between them is which market an order
        reaches, which is not a detail to resolve by taking the first row.
        """
        payload = self._request("GET", "/ref/v1/instruments",
                                params={"Keywords": symbol, "AssetTypes": asset_type})
        matches = [m for m in (payload.get("Data") or [])
                   if str(m.get("Symbol", "")).upper().split(":")[0] == symbol.upper()]
        if not matches:
            raise SaxoError(f"Saxo has no {asset_type} instrument matching {symbol!r}.")
        uics = {m.get("Identifier") for m in matches}
        if len(uics) > 1:
            described = ", ".join(
                f"{m.get('Symbol')} (Uic {m.get('Identifier')}, "
                f"{m.get('ExchangeId')})" for m in matches[:6])
            raise SaxoError(
                f"{symbol!r} is ambiguous across exchanges: {described}. Pass a "
                "Uic explicitly rather than have this choose a market for you.")
        return int(matches[0]["Identifier"])

    # -- orders -----------------------------------------------------------
    def build_order(self, symbol, action, quantity, order_type="LMT",
                    limit_price=None, client_order_id=None,
                    asset_type=_DEFAULT_ASSET_TYPE, uic=None) -> dict:
        import uuid

        side = _SIDE.get(str(action).upper())
        if side is None:
            raise ValueError(f"action must be BUY or SELL, got {action!r}")
        norm_type = _ORDER_TYPE.get(str(order_type).upper())
        if norm_type is None:
            raise ValueError(f"Unsupported order_type {order_type!r}; use LMT or MKT")
        qty = float(quantity)
        if qty <= 0:
            raise ValueError(f"quantity must be positive, got {quantity!r}")

        order = {
            "AccountKey": self.primary_account_id(),
            "Uic": int(uic) if uic is not None else self.resolve_uic(symbol, asset_type),
            "AssetType": asset_type,
            "Amount": qty,
            "BuySell": side,
            "OrderType": norm_type,
            # GoodTillCancel vs DayOrder changes what happens overnight. Day is
            # the conservative default and matches the Webull adapter.
            "OrderDuration": {"DurationType": "DayOrder"},
            # Saxo requires this flag to distinguish a human-initiated order from
            # an algorithmic one. Every order this project sends is human
            # approved by construction, so it is always true.
            "ManualOrder": True,
            # Ours, echoed back, so cancellation keys on an id we chose.
            "ExternalReference": client_order_id or uuid.uuid4().hex,
        }
        if norm_type == "Limit":
            if limit_price is None:
                raise ValueError("A Limit order requires a limit_price")
            order["OrderPrice"] = float(limit_price)
        return order

    def rule_violations(self, order: dict) -> list:
        """
        Only what is checkable without the API. Saxo publishes per-instrument
        lot sizes and tick sizes through ref/v1/instruments/details, which this
        adapter does not fetch -- so this deliberately does NOT claim to know
        what Saxo will refuse. The precheck endpoint is the real gate.
        """
        problems = []
        try:
            amount = float(order.get("Amount", 0))
        except (TypeError, ValueError):
            return ["Amount is not a number."]
        if amount <= 0:
            problems.append("Amount must be greater than zero.")
        if order.get("BuySell") not in ("Buy", "Sell"):
            problems.append(f"BuySell must be Buy or Sell, not {order.get('BuySell')!r}.")
        if order.get("OrderType") == "Limit" and order.get("OrderPrice") in (None, 0):
            problems.append("A Limit order needs a non-zero OrderPrice.")
        if not order.get("Uic"):
            problems.append("No Uic — the instrument was never resolved.")
        return problems

    def preview_order(self, order: dict) -> dict:
        """
        Saxo's own precheck. Non-binding, like every preview: it is the broker
        pricing an order, not consenting to it.
        """
        payload = self._request("POST", "/trade/v2/orders/precheck", body=order)
        estimate = payload.get("EstimatedCashRequired")
        if estimate is None:
            estimate = payload.get("EstimatedCashSubjectToProfitLossRoundTrip")
        return {
            "cost": _num(estimate),
            "fee": _num((payload.get("EstimatedCosts") or {}).get("TotalCost")),
            "currency": (payload.get("Currency") or "").upper(),
            "pre_check_result": payload.get("PreCheckResult"),
            "raw": payload,
        }

    def place_order(self, order: dict) -> dict:
        payload = self._request("POST", "/trade/v2/orders", body=order)
        order_id = payload.get("OrderId")
        if not order_id:
            raise SaxoError(f"Saxo accepted the request but returned no OrderId: {payload!r}")
        return {
            "order_id": str(order_id),
            "client_order_id": str(order.get("ExternalReference", "")),
            "raw": payload,
        }

    def confirm_order(self, reply_id: str, confirmed: bool = True) -> dict:
        """
        Saxo validates through precheck before placement rather than asking
        afterwards, so there is nothing to answer. Documented; never exercised.
        """
        raise NotImplementedError(
            "Saxo validates through /trade/v2/orders/precheck before placement "
            "rather than asking a question after it. Nothing here needs "
            "answering.")

    def cancel_order(self, client_order_id: str) -> dict:
        """
        Saxo's cancel takes ITS OrderId in the path, not our external reference.

        Webull keys cancellation on the client id; Saxo does not, and there is
        no documented lookup from ExternalReference to OrderId. Guessing here
        would mean cancelling the wrong order or silently cancelling nothing --
        the exact failure the Webull cancel path already had once.
        """
        raise SaxoNotVerified(
            "Saxo cancels by its own OrderId (DELETE /trade/v2/orders/{OrderIds}"
            "?AccountKey=), not by the ExternalReference we generate, and the "
            "documentation does not describe a lookup between them. "
            f"Cancel {client_order_id!r} from Saxo's platform, or pass the "
            "OrderId to cancel_by_order_id(). VERIFIER: report whether orders "
            "?Status=Working returns ExternalReference so the two can be mapped.")

    def cancel_order_by_order_id(self, order_id: str) -> dict:
        """Cancel by Saxo's own OrderId. Documented shape; never executed."""
        payload = self._request(
            "DELETE", f"/trade/v2/orders/{urllib.parse.quote(str(order_id))}",
            params={"AccountKey": self.primary_account_id()})
        return {"order_id": str(order_id), "client_order_id": "", "raw": payload}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def oauth_authorize_url(client_id: str, redirect_uri: str, state: str,
                        environment: str = "sim") -> str:
    """
    Step one of Saxo's OAuth2 authorization-code flow.

    Documented shape, never exercised. The token exchange is deliberately not
    implemented here: it needs a client secret, and a half-tested credential
    exchange is worse than none -- it fails in ways that look like a bad
    password.
    """
    base = LIVE_AUTH if environment in ("live", "prod") else SIM_AUTH
    return f"{base}/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    })
