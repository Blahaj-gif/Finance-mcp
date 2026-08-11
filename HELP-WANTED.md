# Help wanted: verify the Saxo and Interactive Brokers adapters

**`dashboard/brokers/saxo.py` and `dashboard/brokers/ibkr.py` have never been
run against their APIs.** They are written from published REST documentation and
nothing else. Field names, response shapes and error behaviour are what the
documentation says, not what the APIs do.

Everything else in this project was proven against the real thing — a live order
was drafted, previewed, placed, watched resting, and cancelled through Webull.
These two are the exception, and they are marked as such everywhere they appear:
`verified = False` on the class, `UNVERIFIED` in any tool output that uses them,
and a `SaxoNotVerified` / `IbkrNotVerified` exception on every path the docs did
not pin down.

**If you have an account with either, an hour with a simulation or paper login
would close that gap.** You do not need to write code.

---

## Why this needs a person

Two failures this project has already had say why documentation is not enough:

- Webull's `cancel_order` sat on an API generation that returns `404` outside
  HK and US. The docs did not say so in a way that helped; the emergency stop
  was simply dead until a live order needed pulling.
- Webull's `preview` priced an order cleanly and placement then refused it for
  a quantity-step rule that preview does not run.

Neither was findable by reading. Both were one real call away.

---

# Saxo Bank

Saxo issues a **24-hour simulation token** from the Developer Portal — no
approval process, no live money, no OAuth flow needed for SIM.

```bash
export SAXO_ENVIRONMENT=sim
export SAXO_ACCESS_TOKEN=<your 24h token>
python -m tests.verify_saxo          # prints a report; sends no orders
```

The script only reads. It resolves an instrument, fetches balances, positions
and accounts, and **prechecks** an order without placing it. Placement is not
automated on purpose.

## The questions worth answering

Each maps to a `SaxoNotVerified` or a guess in the code:

| # | Question | Where |
|---|---|---|
| 1 | Which field in `GET /port/v1/balances` holds available buying power? The code tries `CashAvailableForTrading`, then `MarginAvailableForTrading`, then `TotalValue`. | `buying_power()` |
| 2 | Does `GET /port/v1/orders?Status=Working` return `ExternalReference`? If it does, cancellation can key on the id we generate instead of Saxo's. | `cancel_order()` |
| 3 | What does `POST /trade/v2/orders/precheck` return for cost? The code reads `EstimatedCashRequired`, falling back to `EstimatedCashSubjectToProfitLossRoundTrip`. | `preview_order()` |
| 4 | Are `NetPositionBase.Symbol` and `NetPositionView.AverageOpenPrice` the right fields, or does a real account use different ones? | `positions()` |
| 5 | Does `ManualOrder: true` behave as expected for a human-approved order, or does Saxo want something else? | `build_order()` |
| 6 | Does `ref/v1/instruments` return several rows for a common ticker? The adapter **refuses** on ambiguity rather than picking an exchange — is that the right call in practice, or unusably strict? | `resolve_uic()` |
| 7 | Does `/chart/v1/charts` return `Open/High/Low/Close` for a **Stock**, or only the bid/ask pairs the FX example shows? This is now the price feed for every tool when Saxo is configured. | `history_bars()` |
| 8 | Does `/ref/v1/instruments/details/{Uic}/{AssetType}` return `TickSize`, `LotSize` and `MinimumOrderSize` under those names? | `contract_rules()` |
| 9 | Does `/ca/v2/events` return `ElectionDeadline` on a voluntary event? A deadline is the whole point of one. | `corporate_actions()` |

---

# Interactive Brokers

This is the **Client Portal Web API**, not the TWS socket API. An earlier note in
this file said IBKR "does not fit the current pacing model — budget for an
architecture change, not an adapter." That was written with only the socket API
in mind and it was wrong: the Client Portal Web API is ordinary request/response
JSON and needed no protocol change at all.

A **paper account** answers every question below and is the better choice. Paper
account ids start `DU`.

```bash
# 1. Download the Client Portal Gateway from IBKR (a Java program).
bin/run.sh root/conf.yaml            # bin\run.bat root\conf.yaml on Windows
# 2. Open https://localhost:5000 in a browser and log in.
#    The certificate warning is expected — the gateway is self-signed by design.
export IBKR_TLS_INSECURE=1           # or point IBKR_CACERT at its certificate
python -m tests.verify_ibkr          # prints a report; submits no orders
```

The script only reads. It checks the session, resolves a contract, fetches the
ledger, summary and positions, and calls **whatif** — IBKR's own non-binding
pricing call — on an order that is never submitted.

## The questions worth answering

| # | Question | Where |
|---|---|---|
| 1 | Does `GET /portfolio/{id}/ledger` return a `BASE` entry carrying the account's base currency? | `base_currency()` |
| 2 | Which field in `GET /portfolio/{id}/summary` holds buying power, and is it an object with `amount` / `currency` / `isNull` or a bare number? The code tries `buyingpower`, then `availablefunds`, then `excessliquidity`. | `buying_power()` |
| 3 | Does `whatif` really return money as display strings — `"3,130.60 USD"` — and does `amount.commission` ever come back as a range? The parser takes the **upper** bound of a range; is that the right read? | `preview_order()` |
| 4 | Are `ticker`, `position`, `avgCost` and `mktPrice` the right fields on `GET /portfolio/{id}/positions/{page}`, and does a page really hold 30? | `positions()` |
| 5 | Does `GET /iserver/secdef/search` return several rows for a common US ticker? The adapter **refuses** on ambiguity rather than picking a listing — right call, or unusably strict? | `resolve_conid()` |
| 6 | Does `GET /iserver/account/orders` return `order_ref` carrying the `cOID` we sent? **This is the load-bearing one.** It is the only reason cancel-by-our-id works on IBKR and not on Saxo. | `order_id_for()` |
| 7 | Is `secType: "{conid}:STK"` accepted in the order body alongside `conid`, or does including it cause a rejection? | `build_order()` |
| 8 | Which confirmation messages does a plain limit order actually raise, and what are their `messageIds`? | `place_order()` |
| 9 | Does `/iserver/marketdata/history` ever return `priceFactor` other than 1 for a US stock? The adapter **refuses** rather than applying it blind, because dividing by a wrong factor is a silent hundredfold error in a price a person acts on. This is now the price feed for every tool when IBKR is configured. | `history_bars()` |
| 10 | Does `POST /iserver/contract/rules` return `sizeIncrement`, `minSize` and an `incrementRules` ladder under those names? | `contract_rules()` |
| 11 | Which `scan_code` and `location` values does a real account accept, and does the response key on `contracts`? | `market_scanner()` |

## The design question, which is not about field names

IBKR can answer a placement with **warnings instead of an order** — "you are
submitting an order without market data", "this order will most likely trigger
and fill immediately", "price exceeds the Percentage constraint of 3%" — each
carrying a reply id that must be confirmed before anything is transmitted.

Every client library I looked at answers these automatically from a table of
canned replies, so that placement looks like one call. **This adapter does not.**
It raises `ConfirmationRequired` and the questions go to the person who approved
the order. A warning addressed to a human that a program answers is not a
warning.

If you think that is wrong — that it makes the tool unusable in practice because
some question fires on every order — that is worth an issue more than any field
name here.

---

## How to report

Open an issue titled **`saxo: verification report`** or **`ibkr: verification
report`** with whatever you got — raw JSON is ideal, redacted of account ids and
amounts. Partial answers are useful; a single question is worth an issue.

An adapter gets `verified = True` **only alongside the evidence**, and a test
asserts each is still `False` today, so nobody can flip it from a reading of
the code.

## Also welcome

- **A clean-machine install test.** `install.bat` and `install.sh` are covered
  by unit tests and inspection, never by a fresh OS. It is the first thing every
  new user touches and the least proven path here.
- **IG Markets.** REST, similar shape to Saxo. `dashboard/broker_protocol.py`
  is the interface to implement; `tests/test_broker_conformance.py` runs against
  any adapter you add.

Neither the protocol nor the conformance suite is finished. The second broker is
where you find out which parts of the first one were the broker and which were
just Webull; the third found two more — that money can be a display string, and
that a broker can ask a question back.
