"""
Interactive Brokers adapter — BUILT FROM DOCUMENTATION, NEVER RUN AGAINST THE API.

    ┌──────────────────────────────────────────────────────────────────────┐
    │  UNVERIFIED. No call in this file has ever reached IBKR. It is        │
    │  written against their published Client Portal Web API reference and  │
    │  nothing else. Field names, response shapes and error behaviour are   │
    │  what the documentation says, not what the API does. Every tool that  │
    │  uses it says so in its output. See HELP-WANTED.md.                   │
    └──────────────────────────────────────────────────────────────────────┘

**Which IBKR API this is, and why.** IBKR publishes two trading interfaces:

  * the **TWS API** — a persistent socket to a desktop application, delivering
    results as asynchronous callbacks keyed by a request id, and
  * the **Client Portal Web API** — ordinary HTTPS request/response JSON.

This project's earlier note said IBKR "does not fit the current pacing model —
budget for an architecture change, not an adapter." That was written with only
the TWS API in mind and it was wrong. The Client Portal Web API is
request/response, so it drops into the same shape as Saxo with no change to the
protocol. The socket API remains the wrong fit; it is simply not the only door.

    REST      https://localhost:5000/v1/api      Client Portal Gateway (local)
              https://api.ibkr.com/v1/api        hosted, OAuth

    POST   /iserver/auth/status                  authenticated? competing?
    POST   /tickle                               keep the session alive
    GET    /iserver/accounts                     required before any order call
    GET    /portfolio/accounts
    GET    /portfolio/{accountId}/summary        buying power (base currency)
    GET    /portfolio/{accountId}/ledger         cash, per currency
    GET    /portfolio/{accountId}/positions/{page}
    GET    /iserver/secdef/search                symbol -> conid
    POST   /iserver/account/{accountId}/orders/whatif    price, non-binding
    POST   /iserver/account/{accountId}/orders           place
    POST   /iserver/reply/{replyId}              answer a confirmation question
    GET    /iserver/account/orders               live orders (carry order_ref)
    DELETE /iserver/account/{accountId}/order/{orderId}

Three things IBKR does that the first two brokers did not, and what each cost:

  * **The broker asks questions back.** A placement can return not an order but a
    list of warnings — "you are submitting an order without market data", "this
    order will most likely trigger and fill immediately", "price exceeds the
    percentage constraint of 3%" — each with a reply id that must be confirmed
    before anything is transmitted. Every client library I looked at answers
    these automatically from a table of canned replies. **This one does not.**
    They are the broker warning a human, and this project exists to put a human
    there. `place_order` raises `ConfirmationRequired`; a person answers it.

  * **Margin is pooled in one currency.** Webull reports buying power per
    currency and Saxo reports one account currency. IBKR reports per-currency
    *cash* in the ledger and a single *buying power* in the account's base
    currency, because margin is computed across the whole account. Cash is not
    buying power, so this refuses rather than substituting one for the other.

  * **Cancellation needs a lookup.** IBKR cancels by its own orderId, like Saxo.
    Unlike Saxo it documents `order_ref` on the live-orders response, which
    carries the client id we generated — so the mapping the protocol requires
    exists, and `cancel_order` performs it rather than raising.

Everywhere the documentation was ambiguous this raises rather than guesses. A
wrong guess in a broker adapter is not a bug that shows up in a log; it is an
order for the wrong quantity, or a buying-power check that passes when it
should not. `IbkrNotVerified` marks each such place and names what a verifier
should check.
"""
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from dashboard import capabilities as _cap
    from dashboard.broker_protocol import ConfirmationRequired
    from dashboard.envfile import load_env
except ImportError:  # imported as a top-level module from dashboard/
    import capabilities as _cap
    from broker_protocol import ConfirmationRequired
    from envfile import load_env

load_env()


class IbkrNotVerified(NotImplementedError):
    """
    A path the documentation did not pin down well enough to implement blind.

    Raised instead of guessing. In a broker adapter a wrong guess is not a log
    line, it is a real order with the wrong quantity.
    """


class IbkrError(RuntimeError):
    """IBKR returned an error, or the response was not the documented shape."""


#: IBKR expires an idle brokerage session in roughly six minutes. Half of
#: that leaves room for a slow call without tickling on every request.
TICKLE_AFTER_SECONDS = 180

GATEWAY_BASE = "https://localhost:5000/v1/api"
HOSTED_BASE = "https://api.ibkr.com/v1/api"

# Ours is BUY/SELL and LMT/MKT. IBKR happens to use the same words for both,
# which is not a reason to skip the mapping -- it is a reason the mapping is
# cheap, and it stays here so a change on their side is one line on ours.
_SIDE = {"BUY": "BUY", "SELL": "SELL"}
_ORDER_TYPE = {"LMT": "LMT", "LIMIT": "LMT",
               "MKT": "MKT", "MARKET": "MKT",
               "STP": "STP", "STOP": "STP"}
_DEFAULT_SEC_TYPE = "STK"

# IBKR names bar sizes its own way. The mapping lives at the boundary, which is
# where the Webull "H1" bug was fixed -- "H1" was silently not a Webull timespan
# and every hourly request fell through to Yahoo without saying so.
IBKR_BAR = {"M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
            "H1": "1h", "H4": "4h", "D": "1d", "W": "1w", "M": "1m"}

#: Roughly how much wall-clock `count` bars of each interval spans. IBKR asks
#: for a period, not a bar count, and asking for too little silently returns a
#: short frame -- so these round up and the caller trims.
_BARS_PER_DAY = {"M1": 390, "M5": 78, "M15": 26, "M30": 13, "H1": 7, "H4": 2,
                 "D": 1, "W": 0.2, "M": 0.05}

# "1,234.56 USD", "USD 1,234.56", "1.00 - 2.50 USD".
_MONEY = re.compile(r"-?[\d,]+(?:\.\d+)?")
_CURRENCY = re.compile(r"\b([A-Z]{3})\b")


class IbkrBroker:
    name = "ibkr"

    CAPABILITIES = frozenset((
        _cap.ACCOUNTS, _cap.POSITIONS, _cap.BUYING_POWER, _cap.OPEN_ORDERS,
        _cap.PREVIEW_ORDER, _cap.PLACE_ORDER, _cap.CANCEL_ORDER,
        _cap.HISTORY_BARS, _cap.CONTRACT_RULES, _cap.MARKET_SCANNER))
    #: Never set this True from a reading of the code. It means somebody ran it
    #: against the API and reported what happened.
    verified = False

    def __init__(self, base_url=None, account_id=None, session=None,
                 access_token=None):
        self.base = (base_url or os.getenv("IBKR_BASE_URL", "")
                     or GATEWAY_BASE).rstrip("/")
        self._account_id = (account_id
                            or os.getenv("IBKR_ACCOUNT_ID", "").strip())
        # Only the hosted endpoint takes a bearer token. The local gateway holds
        # the session itself after a browser login and wants no Authorization
        # header at all.
        self._token = (access_token
                       or os.getenv("IBKR_ACCESS_TOKEN", "").strip())
        self._cacert = os.getenv("IBKR_CACERT", "").strip()
        self._insecure = os.getenv("IBKR_TLS_INSECURE", "").strip().lower() in (
            "1", "true", "yes")
        self._brokerage_session_ready = False
        # Monotonic, not wall clock: a session that survives a clock
        # change should not suddenly look six minutes idle.
        self._last_call = time.monotonic()
        # Injectable so the conformance tests can drive the adapter without a
        # network, and so a verifier can log every exchange.
        self._session = session or self._http

    # -- transport --------------------------------------------------------
    def _ssl_context(self):
        """
        The Client Portal Gateway serves a self-signed certificate on
        localhost. Turning verification off is a decision, not a default: this
        takes the explicit opt-in and says which one is in force, rather than
        quietly disabling TLS the way most gateway examples do.
        """
        ctx = ssl.create_default_context()
        if self._cacert:
            ctx.load_verify_locations(self._cacert)
        elif self._insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _http(self, method: str, url: str, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30,
                                        context=self._ssl_context()) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise IbkrError(f"IBKR {method} {url} -> HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLError) and not (self._cacert or self._insecure):
                raise IbkrError(
                    "The Client Portal Gateway's certificate did not verify. It "
                    "is self-signed by design. Either point IBKR_CACERT at the "
                    "gateway's certificate, or set IBKR_TLS_INSECURE=1 to accept "
                    "it unverified -- which is a decision about a connection "
                    "that carries your orders, so it is not made for you.") from e
            raise IbkrError(
                f"Could not reach IBKR at {url}: {reason}. If you are using the "
                "Client Portal Gateway, it has to be running and logged in "
                "through a browser first.") from e

    def _request(self, method: str, path: str, params=None, body=None):
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")})
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            return self._session(method, url, body, headers)
        finally:
            # Any call keeps the session warm, so idleness is measured
            # from the last one rather than from the last tickle.
            self._last_call = time.monotonic()

    # -- session ----------------------------------------------------------
    def auth_status(self) -> dict:
        """
        Whether this gateway is logged in and holds the trading session.

        `competing` is the one that matters. IBKR permits one brokerage session
        per login, so signing into the mobile app takes it from here -- and the
        symptom is calls that succeed while doing nothing. It is checked before
        anything is sent rather than diagnosed afterwards.
        """
        payload = self._request("POST", "/iserver/auth/status")
        if not isinstance(payload, dict):
            raise IbkrError(f"Unexpected auth status payload: {payload!r}")
        if not payload.get("authenticated"):
            raise IbkrError(
                "This IBKR session is not authenticated. Start the Client "
                "Portal Gateway, open https://localhost:5000 in a browser and "
                f"log in. Gateway said: {payload!r}")
        if payload.get("competing"):
            raise IbkrError(
                "Another IBKR session has taken over this login (competing = "
                "true) -- signing into the mobile or desktop app does that. "
                "Orders sent now may not reach the exchange. Close the other "
                "session and re-authenticate.")
        return payload

    def _ensure_brokerage_session(self):
        """
        IBKR documents GET /iserver/accounts as a prerequisite for order
        endpoints in a new session. Skipping it fails at placement, which is
        the worst place to find out.

        Also keeps the session warm. IBKR expires an idle brokerage session in
        roughly six minutes, and an MCP server is idle almost all the time --
        it wakes when someone asks a question. Client libraries run a
        background "tickler" thread for this; a thread that outlives the call
        that made it is a thing to avoid in a stdio server, so this tickles on
        the way in instead. Costs one request on a call that was about to make
        several.
        """
        if self._idle_seconds() > TICKLE_AFTER_SECONDS:
            try:
                self.tickle()
            except IbkrError:
                # A failed keep-alive is not itself a reason to refuse. Whatever
                # is wrong will be reported by auth_status a line below, in the
                # words IBKR used.
                pass

        if self._brokerage_session_ready:
            return
        self.auth_status()
        self._request("GET", "/iserver/accounts")
        self._brokerage_session_ready = True

    def _idle_seconds(self) -> float:
        return time.monotonic() - self._last_call

    def tickle(self) -> dict:
        """Keep the session alive. IBKR expires an idle one in about 6 minutes."""
        return self._request("POST", "/tickle")

    # -- identity ---------------------------------------------------------
    def environment_label(self) -> str:
        """
        IBKR paper accounts are numbered DU…; live accounts U…. Where the
        account is not known yet this says LIVE, because the failure directions
        are not symmetric: calling a live account PAPER invites someone to
        approve an order they would have refused.
        """
        account = self._account_id or os.getenv("IBKR_ACCOUNT_ID", "").strip()
        if account.upper().startswith("DU"):
            return "PAPER"
        if account.upper().startswith("U"):
            return "LIVE"
        env = os.getenv("IBKR_ENVIRONMENT", "").strip().lower()
        return "PAPER" if env in ("paper", "sim", "demo") else "LIVE"

    def primary_account_id(self) -> str:
        """
        Refuses to choose when a login has several accounts, for the same
        reason Webull and Saxo do: picking the first would route orders to
        whichever the API happened to list first.
        """
        if self._account_id:
            return self._account_id

        payload = self._request("GET", "/portfolio/accounts")
        accounts = payload if isinstance(payload, list) else (payload or {}).get("accounts") or []
        if not accounts:
            raise IbkrError("IBKR returned no accounts for this session.")
        if len(accounts) > 1:
            described = ", ".join(
                f"{a.get('accountId') or a.get('id')} ({a.get('currency', '?')})"
                for a in accounts if isinstance(a, dict))
            raise IbkrError(
                f"This login has {len(accounts)} accounts and none is pinned. "
                f"Set IBKR_ACCOUNT_ID to one of: {described}")
        first = accounts[0]
        self._account_id = str(first.get("accountId") or first.get("id") or "")
        if not self._account_id:
            raise IbkrError(f"No accountId in the account payload: {first!r}")
        return self._account_id

    # -- account ----------------------------------------------------------
    def accounts(self) -> list:
        payload = self._request("GET", "/portfolio/accounts")
        rows = payload if isinstance(payload, list) else (payload or {}).get("accounts") or []
        out = []
        for a in rows:
            if not isinstance(a, dict):
                continue
            account_id = str(a.get("accountId") or a.get("id") or "")
            out.append({
                "id": account_id,
                "currency": str(a.get("currency") or "").upper(),
                # DU… is a paper account. Saying so beside the id is cheaper
                # than a person inferring it from a prefix.
                "label": str(a.get("accountTitle") or a.get("desc") or "")
                         + (" (paper)" if account_id.upper().startswith("DU") else ""),
                "raw": a,
            })
        return out

    def ledger(self) -> dict:
        """Cash per currency, keyed by currency code plus a "BASE" entry."""
        payload = self._request(
            "GET", f"/portfolio/{urllib.parse.quote(self.primary_account_id())}/ledger")
        if not isinstance(payload, dict):
            raise IbkrError(f"Unexpected ledger payload: {payload!r}")
        return payload

    def base_currency(self) -> str:
        """The currency IBKR computes this account's margin in."""
        return self._base_currency(self.ledger())

    @staticmethod
    def _base_currency(ledger: dict) -> str:
        currency = str((ledger.get("BASE") or {}).get("currency") or "").upper()
        if not currency:
            raise IbkrNotVerified(
                "The ledger had no BASE entry carrying a currency (keys: "
                f"{sorted(ledger)[:12]}). Which currency this account's buying "
                "power is denominated in is not a thing to assume. VERIFIER: "
                "report how a real account reports its base currency.")
        return currency

    def buying_power(self, currency: str = "USD") -> float:
        """
        IBKR pools margin across the account and reports buying power in one
        currency — the base currency — while the ledger reports *cash* per
        currency. Those are different quantities: a margin account's buying
        power exceeds its cash, and a currency line can be negative against a
        positive buying power.

        So this answers for the base currency and refuses otherwise. Returning
        a currency's cash balance under the name "buying power" would be the
        safe direction numerically and still wrong, and a guard that is wrong
        in a safe direction is a guard nobody trusts.
        """
        wanted = currency.upper()
        ledger = self.ledger()
        # Raises rather than falling through to "answer for whatever was asked"
        # when the base currency is unreadable -- a buying-power check that does
        # not know its own units is not a check.
        base = self._base_currency(ledger)

        if wanted != base:
            known = ", ".join(sorted(k for k in ledger if k != "BASE"))
            raise IbkrError(
                f"This IBKR account computes buying power in {base}, not "
                f"{wanted}. IBKR pools margin across the whole account and "
                "reports cash — not buying power — per currency, so answering "
                f"in {wanted} would mean either converting at an unstated rate "
                "or reporting cash under the wrong name. Ask for "
                f"{base}. (Currency lines present: {known or 'none'}.)")

        summary = self._request(
            "GET",
            f"/portfolio/{urllib.parse.quote(self.primary_account_id())}/summary")
        if not isinstance(summary, dict):
            raise IbkrError(f"Unexpected account summary payload: {summary!r}")

        for field in ("buyingpower", "availablefunds", "excessliquidity"):
            if field in summary:
                value = _summary_amount(summary[field])
                if value is not None:
                    return value
        raise IbkrNotVerified(
            "No documented buying-power field was present in the account "
            f"summary (keys: {sorted(summary)[:12]}). VERIFIER: report which "
            "field a real account returns for available buying power, and "
            "whether it is a bare number or an object with an 'amount' key.")

    def positions(self) -> list:
        """
        IBKR pages positions 30 at a time. A short page ends it; the cap is a
        backstop so a paging change cannot turn this into an endless loop.
        """
        account = urllib.parse.quote(self.primary_account_id())
        out, page = [], 0
        while page < 40:
            payload = self._request("GET", f"/portfolio/{account}/positions/{page}")
            rows = payload if isinstance(payload, list) else (payload or {}).get("positions") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                out.append({
                    "symbol": (row.get("ticker") or row.get("contractDesc") or ""),
                    "conid": row.get("conid"),
                    "asset_type": row.get("assetClass"),
                    "quantity": _num(row.get("position")) or 0.0,
                    "cost": _num(row.get("avgCost")),
                    "last": _num(row.get("mktPrice")),
                    "currency": str(row.get("currency") or "").upper(),
                    "raw": row,
                })
            if len(rows) < 30:
                break
            page += 1
        return out

    def position_quantity(self, symbol: str) -> float:
        for p in self.positions():
            if str(p["symbol"]).upper() == symbol.upper():
                return float(p["quantity"] or 0)
        return 0.0

    # -- instruments ------------------------------------------------------
    def resolve_conid(self, symbol: str, sec_type: str = _DEFAULT_SEC_TYPE) -> int:
        """
        Ticker -> conid. IBKR orders address a numeric contract, not a string.

        Refuses on an ambiguous match. The same ticker lists on several
        exchanges and the difference between them is which market an order
        reaches, which is not a detail to resolve by taking the first row.
        """
        payload = self._request("GET", "/iserver/secdef/search",
                                params={"symbol": symbol, "secType": sec_type,
                                        "name": "false"})
        rows = payload if isinstance(payload, list) else (payload or {}).get("results") or []
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol", "")).upper() != symbol.upper():
                continue
            sections = row.get("sections") or []
            offered = {str(s.get("secType", "")).upper()
                       for s in sections if isinstance(s, dict)}
            if offered and sec_type.upper() not in offered:
                continue
            matches.append(row)

        if not matches:
            raise IbkrError(
                f"IBKR has no {sec_type} contract matching {symbol!r}.")
        conids = {str(m.get("conid")) for m in matches if m.get("conid")}
        if not conids:
            raise IbkrError(
                f"IBKR matched {symbol!r} but returned no conid: {matches[:2]!r}")
        if len(conids) > 1:
            described = ", ".join(
                f"{m.get('symbol')} (conid {m.get('conid')}, "
                f"{m.get('description') or m.get('companyHeader')})"
                for m in matches[:6])
            raise IbkrError(
                f"{symbol!r} is ambiguous across listings: {described}. Pass a "
                "conid explicitly rather than have this choose a market for you.")
        return int(next(iter(conids)))

    def contract_rules(self, symbol: str, sec_type=_DEFAULT_SEC_TYPE,
                       conid=None, is_buy: bool = True) -> dict:
        """
        IBKR's own tick and lot rules for one contract.

        `incrementRules` is a *ladder* — the tick widens above a price edge —
        so the number reported here is the increment at the bottom of the
        ladder, which is the one a limit near the money is checked against.
        whatif remains the authority for anything above it.
        """
        resolved = int(conid) if conid is not None else self.resolve_conid(symbol, sec_type)
        payload = self._request("POST", "/iserver/contract/rules",
                                body={"conid": str(resolved), "isBuy": bool(is_buy)})
        if not isinstance(payload, dict) or not payload:
            raise IbkrError(f"No contract rules returned for {symbol!r}: {payload!r}")

        ladder = payload.get("incrementRules") or []
        increment = None
        if isinstance(ladder, list) and ladder and isinstance(ladder[0], dict):
            increment = _num(ladder[0].get("increment"))
        if increment is None:
            increment = _num(payload.get("increment"))

        return {
            "symbol": symbol.upper(),
            "conid": resolved,
            "tick_size": increment,
            "quantity_step": _num(payload.get("sizeIncrement")),
            "min_quantity": _num(payload.get("minSize")
                                 or payload.get("sizeIncrement")),
            "currency": str(payload.get("currency") or "").upper(),
            "order_types": payload.get("orderTypes") or [],
            "raw": payload,
        }

    # -- market data ------------------------------------------------------
    def history_bars(self, symbol: str, interval: str = "D", count: int = 200,
                     sec_type=_DEFAULT_SEC_TYPE, conid=None, outside_rth=False):
        """
        Bars from IBKR, so an IBKR user is not on the Yahoo fallback for every
        price in the server having supplied IBKR credentials.

        Returned oldest-first. IBKR documents ascending, but this sorts anyway:
        Webull returned newest-first and nothing sorted it, so every indicator
        ran on a reversed series and the sector heatmap ranked the worst
        performers as leaders.
        """
        import pandas as pd

        bar = IBKR_BAR.get(str(interval).upper())
        if bar is None:
            raise ValueError(
                f"Unsupported interval {interval!r} for IBKR; use one of "
                f"{sorted(IBKR_BAR)}")
        resolved = int(conid) if conid is not None else self.resolve_conid(symbol, sec_type)
        payload = self._request("GET", "/iserver/marketdata/history", params={
            "conid": str(resolved), "bar": bar,
            "period": _ibkr_period(interval, count),
            "outsideRth": "true" if outside_rth else "false"})
        if not isinstance(payload, dict):
            raise IbkrError(f"Unexpected history payload: {payload!r}")

        rows = payload.get("data") or []
        if not rows:
            raise IbkrError(f"IBKR returned no bars for {symbol!r} ({interval}).")

        # IBKR scales some instruments' prices by priceFactor. It is 1 for US
        # equities, and dividing by a wrong factor is a silent hundredfold
        # error in a price a person acts on, so anything but 1 stops here.
        factor = _num(payload.get("priceFactor")) or 1.0
        if factor != 1.0:
            raise IbkrNotVerified(
                f"IBKR returned priceFactor={factor} for {symbol!r}. Applying it "
                "blind would risk a silently mis-scaled price. VERIFIER: report "
                "whether prices in `data` are already scaled or need dividing by "
                "priceFactor.")

        frame = pd.DataFrame([{
            "time": r.get("t"),
            "open": _num(r.get("o")), "high": _num(r.get("h")),
            "low": _num(r.get("l")), "close": _num(r.get("c")),
            "volume": _num(r.get("v")) or 0.0,
        } for r in rows if isinstance(r, dict)])

        if frame.empty or frame["close"].isna().all():
            raise IbkrNotVerified(
                "IBKR returned history rows this adapter could not read as OHLC "
                f"(keys: {sorted(rows[0])[:10]}). VERIFIER: report the field "
                "names a real history response uses.")
        # `t` is epoch milliseconds.
        frame["time"] = pd.to_datetime(frame["time"], unit="ms", errors="coerce")
        if frame["time"].isna().any():
            raise IbkrError("IBKR returned a bar with an unparseable timestamp; "
                            "unorderable bars must not proceed.")
        frame = frame.sort_values("time").reset_index(drop=True)
        frame.attrs["resolved_symbol"] = symbol.upper()
        return frame

    # -- scanner ----------------------------------------------------------
    def market_scanner(self, scan_code: str = "TOP_PERC_GAIN",
                       instrument: str = "STK", location: str = "STK.US.MAJOR",
                       limit: int = 25) -> list:
        """
        IBKR's own scanner. Neither other broker has anything comparable, which
        is why this is an ibkr-prefixed tool and not part of the protocol.
        """
        payload = self._request("POST", "/iserver/scanner/run", body={
            "instrument": instrument, "type": scan_code,
            "location": location, "filter": []})
        rows = (payload or {}).get("contracts") or (payload or {}).get("Contracts") or []
        out = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            out.append({
                "symbol": str(row.get("symbol") or row.get("contract_description_1") or ""),
                "conid": row.get("con_id") or row.get("conid"),
                "company": str(row.get("company_name") or ""),
                "detail": str(row.get("scan_data") or row.get("contract_description_2") or ""),
                "raw": row,
            })
        return out

    def scanner_parameters(self) -> dict:
        """The scan codes and locations this account may use. Large payload."""
        return self._request("GET", "/iserver/scanner/params")

    # -- orders -----------------------------------------------------------
    def build_order(self, symbol, action, quantity, order_type="LMT",
                    limit_price=None, client_order_id=None,
                    sec_type=_DEFAULT_SEC_TYPE, conid=None, tif="DAY",
                    outside_rth=False) -> dict:
        import uuid

        # Validated before anything is resolved, so a malformed draft costs no
        # network call and cannot be reported as a broker failure.
        side = _SIDE.get(str(action).upper())
        if side is None:
            raise ValueError(f"action must be BUY or SELL, got {action!r}")
        norm_type = _ORDER_TYPE.get(str(order_type).upper())
        if norm_type is None:
            raise ValueError(f"Unsupported order_type {order_type!r}; use LMT or MKT")
        qty = float(quantity)
        if qty <= 0:
            raise ValueError(f"quantity must be positive, got {quantity!r}")
        if norm_type == "LMT" and limit_price is None:
            raise ValueError("A LMT order requires a limit_price")

        resolved = int(conid) if conid is not None else self.resolve_conid(symbol, sec_type)
        order = {
            "acctId": self.primary_account_id(),
            "conid": resolved,
            # IBKR's own disambiguator, "conid:type".
            "secType": f"{resolved}:{sec_type}",
            "orderType": norm_type,
            "side": side,
            "quantity": qty,
            # DAY rather than GTC. What happens to a resting order overnight is
            # a decision, and the conservative one matches the other adapters.
            "tif": tif,
            "outsideRTH": bool(outside_rth),
            # Ours, echoed back as order_ref on live orders, which is what makes
            # cancel-by-client-id possible here and impossible on Saxo.
            "cOID": client_order_id or uuid.uuid4().hex,
        }
        if norm_type == "LMT":
            order["price"] = float(limit_price)
        return order

    def rule_violations(self, order: dict, rules: dict = None) -> list:
        """
        Structural checks always; real tick and lot rules when `rules` comes
        from contract_rules(). Without them this does NOT claim to know what
        IBKR will refuse — whatif and the confirmation questions are the real
        gates, and both are the broker's, not ours.
        """
        try:
            from dashboard.broker import contract_rule_violations
        except ImportError:
            from broker import contract_rule_violations
        problems = contract_rule_violations(order, rules, quantity_key="quantity",
                                            price_key="price")
        try:
            quantity = float(order.get("quantity", 0))
        except (TypeError, ValueError):
            return ["quantity is not a number."]
        if quantity <= 0:
            problems.append("quantity must be greater than zero.")
        if order.get("side") not in ("BUY", "SELL"):
            problems.append(f"side must be BUY or SELL, not {order.get('side')!r}.")
        if order.get("orderType") == "LMT" and not order.get("price"):
            problems.append("A LMT order needs a non-zero price.")
        if not order.get("conid"):
            problems.append("No conid — the contract was never resolved.")
        if not order.get("cOID"):
            problems.append("No cOID — this order could not be cancelled by id.")
        return problems

    def preview_order(self, order: dict) -> dict:
        """
        IBKR's whatif. Non-binding, like every preview: it is the broker
        pricing an order and reporting its margin impact, not consenting to it.
        """
        account = urllib.parse.quote(self.primary_account_id())
        payload = self._request("POST", f"/iserver/account/{account}/orders/whatif",
                                body={"orders": [order]})
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            raise IbkrError(f"Unexpected whatif payload: {payload!r}")

        error = payload.get("error")
        if error:
            raise IbkrError(f"IBKR refused to price this order: {error}")

        amount = payload.get("amount") or {}
        cost, cost_ccy = _money(amount.get("amount"))
        fee, fee_ccy = _money(amount.get("commission"))
        return {
            "cost": cost,
            "fee": fee,
            "currency": cost_ccy or fee_ccy or "",
            "warning": payload.get("warn") or None,
            "initial_margin": _money((payload.get("initial") or {}).get("after"))[0],
            "maintenance_margin": _money((payload.get("maintenance") or {}).get("after"))[0],
            "raw": payload,
        }

    def place_order(self, order: dict) -> dict:
        """
        Submit for execution — or surface what IBKR wants answered first.

        IBKR may respond with warnings rather than an order id, each carrying a
        reply id. Client libraries generally answer these from a table of canned
        replies so that placement looks like one call. This does not: the
        warnings are the broker addressing a person, and the whole point of this
        project is that there is one. `ConfirmationRequired` carries the
        questions up to whoever approved the order.
        """
        self._ensure_brokerage_session()
        account = urllib.parse.quote(self.primary_account_id())
        payload = self._request("POST", f"/iserver/account/{account}/orders",
                                body={"orders": [order]})
        return self._interpret_order_response(
            payload, str(order.get("cOID", "")))

    def confirm_order(self, reply_id: str, confirmed: bool = True) -> dict:
        """
        Answer one of IBKR's confirmation questions.

        Only ever called with an answer a human gave. `confirmed=False` is a
        real option and IBKR treats it as a refusal to place.
        """
        if not reply_id:
            raise ValueError("confirm_order needs the reply id from the question")
        payload = self._request("POST",
                                f"/iserver/reply/{urllib.parse.quote(str(reply_id))}",
                                body={"confirmed": bool(confirmed)})
        return self._interpret_order_response(payload, "")

    def _interpret_order_response(self, payload, client_order_id: str) -> dict:
        rows = payload if isinstance(payload, list) else [payload]
        if not rows or not isinstance(rows[0], dict):
            raise IbkrError(f"Unrecognised order response from IBKR: {payload!r}")
        first = rows[0]

        if first.get("error"):
            raise IbkrError(f"IBKR rejected the order: {first['error']}")

        if "message" in first:
            messages = first.get("message")
            if isinstance(messages, str):
                messages = [messages]
            reply_id = str(first.get("id", ""))
            if not reply_id:
                # A question with no reply id cannot be answered, so raising
                # ConfirmationRequired would hand the caller a dead end. Say
                # that instead: an order in an unknown state is the thing a
                # person most needs told.
                raise IbkrError(
                    "IBKR returned a message but no reply id, so this order "
                    "can be neither confirmed nor assumed placed. Check the "
                    f"order book before retrying. Response: {payload!r}")
            raise ConfirmationRequired(
                broker=self.name,
                reply_id=reply_id,
                questions=[str(m) for m in (messages or [])],
                client_order_id=client_order_id,
                raw=payload)

        order_id = first.get("order_id") or first.get("orderId")
        if not order_id:
            raise IbkrError(
                "IBKR returned neither an order id nor a question, so whether "
                f"this order was placed is unknown: {payload!r}")
        return {
            "order_id": str(order_id),
            "client_order_id": str(first.get("local_order_id") or client_order_id or ""),
            "order_status": first.get("order_status"),
            "raw": payload,
        }

    def live_orders(self) -> list:
        """IBKR's rows, unnormalised. open_orders() is the protocol shape."""
        payload = self._request("GET", "/iserver/account/orders")
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        rows = (payload or {}).get("orders") or []
        return [r for r in rows if isinstance(r, dict)]

    def open_orders(self) -> list:
        out = []
        for o in self.live_orders():
            out.append({
                "order_id": str(o.get("orderId") or o.get("order_id") or ""),
                "client_order_id": str(o.get("order_ref") or ""),
                "symbol": str(o.get("ticker") or o.get("symbol") or ""),
                "action": str(o.get("side") or "").upper(),
                # IBKR reports what is left, not what was asked for, so the
                # original size is remaining + filled.
                "quantity": ((_num(o.get("remainingQuantity")) or 0.0)
                             + (_num(o.get("filledQuantity")) or 0.0)),
                "filled": _num(o.get("filledQuantity")) or 0.0,
                "limit_price": _num(o.get("price")),
                "status": str(o.get("status") or ""),
                "raw": o,
            })
        return out

    def order_id_for(self, client_order_id: str) -> str:
        """
        Our id -> IBKR's, through the live-orders response.

        This is the lookup Saxo does not document, and it is the only reason
        cancel_order works here. If IBKR stops returning order_ref this must
        fail loudly rather than fall back to cancelling something plausible.
        """
        orders = self.live_orders()
        for row in orders:
            if str(row.get("order_ref") or "") == str(client_order_id):
                order_id = row.get("orderId") or row.get("order_id")
                if not order_id:
                    raise IbkrError(
                        f"Found the working order for {client_order_id!r} but it "
                        f"carried no orderId: {row!r}")
                return str(order_id)

        if orders and not any("order_ref" in row for row in orders):
            raise IbkrNotVerified(
                "No row in the live-orders response carried order_ref, so the "
                "client order id we generate cannot be mapped to IBKR's "
                "orderId, and cancelling by our id is not possible. VERIFIER: "
                "report the field a real account returns for the customer order "
                "id on GET /iserver/account/orders.")
        raise IbkrError(
            f"No working IBKR order carries the client id {client_order_id!r}. "
            "It may have filled, been cancelled already, or never been placed. "
            "Nothing was cancelled.")

    def cancel_order(self, client_order_id: str) -> dict:
        """Cancel a working order by the id we generated for it."""
        order_id = self.order_id_for(client_order_id)
        return self._cancel_by_order_id(order_id, client_order_id)

    def cancel_order_by_order_id(self, order_id: str) -> dict:
        """Cancel by IBKR's own orderId, when that is the id in hand."""
        return self._cancel_by_order_id(order_id, "")

    def _cancel_by_order_id(self, order_id: str, client_order_id: str) -> dict:
        if str(order_id).strip() in ("", "-1"):
            # IBKR documents -1 as "cancel every open order". That is a
            # different action from cancelling one, and it is not reachable by
            # accident from here.
            raise ValueError(
                "Refusing to send -1 to IBKR's cancel endpoint: it cancels "
                "every open order on the account. Cancel them individually.")
        account = urllib.parse.quote(self.primary_account_id())
        payload = self._request(
            "DELETE",
            f"/iserver/account/{account}/order/{urllib.parse.quote(str(order_id))}")
        return {
            "order_id": str(order_id),
            "client_order_id": str(client_order_id),
            "raw": payload,
        }


# =====================================================================
# Parsing
# =====================================================================

def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ibkr_period(interval: str, count: int) -> str:
    """
    Bar count -> the period string IBKR wants, rounded up.

    Trading days, not calendar days, and a margin on top: asking for exactly
    the span needed returns a frame one weekend short, and a short frame is how
    an indicator quietly computes over the wrong window.
    """
    per_day = _BARS_PER_DAY.get(str(interval).upper(), 1)
    days = max(1, int(count / per_day * 1.6) + 2)
    if days <= 30:
        return f"{days}d"
    months = max(1, int(days / 30) + 1)
    return f"{months}m" if months <= 11 else f"{max(1, int(months / 12) + 1)}y"


def _summary_amount(value):
    """
    IBKR's account summary reports each line as an object carrying `amount`,
    but a bare number appears in some responses. Both are read; anything else
    is left for the caller to refuse rather than coerced to zero.
    """
    if isinstance(value, dict):
        if value.get("isNull"):
            return None
        return _num(value.get("amount"))
    return _num(value)


def _money(value):
    """
    IBKR's whatif reports money as a display string -- "1,234.56 USD" -- and
    sometimes as a range, "1.00 - 2.50 USD", where the commission is not yet
    known exactly.

    Returns (amount, currency). For a range this takes the **upper** bound:
    the number is used to tell a person what an order will cost them, and the
    direction to be wrong in is the expensive one.
    """
    if value is None or value == "":
        return None, ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), ""

    text = str(value)
    currency_match = _CURRENCY.search(text)
    currency = currency_match.group(1) if currency_match else ""
    numbers = [float(n.replace(",", "")) for n in _MONEY.findall(text)]
    if not numbers:
        return None, currency
    return max(numbers), currency
