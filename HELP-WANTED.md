# Help wanted: verify the Saxo Bank adapter

**`dashboard/brokers/saxo.py` has never been run against Saxo's API.** It is
written from their published OpenAPI reference and nothing else. Field names,
response shapes and error behaviour are what the documentation says, not what
the API does.

Everything else in this project was proven against the real thing — a live order
was drafted, previewed, placed, watched resting, and cancelled. This adapter is
the exception, and it is marked as such everywhere it appears: `verified = False`
on the class, `UNVERIFIED` in any tool output that uses it, and a
`SaxoNotVerified` exception on every path the docs did not pin down.

**If you have a Saxo account, an hour with a simulation token would close that
gap.** You do not need to write code.

---

## Why this needs a person

Two failures this project has already had say why documentation is not enough:

- Webull's `cancel_order` sat on an API generation that returns `404` outside
  HK and US. The docs did not say so in a way that helped; the emergency stop
  was simply dead until a live order needed pulling.
- Webull's `preview` priced an order cleanly and placement then refused it for
  a quantity-step rule that preview does not run.

Neither was findable by reading. Both were one real call away.

## What to do

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

## How to report

Open an issue titled **`saxo: verification report`** with whatever you got —
raw JSON is ideal, redacted of account keys and amounts. Partial answers are
useful; question 1 alone is worth an issue.

A verified adapter gets `verified = True` **only alongside the evidence**, and a
test asserts it is still `False` today, so nobody can flip it from a reading of
the code.

## Also welcome

- **A clean-machine install test.** `install.bat` and `install.sh` are covered
  by unit tests and inspection, never by a fresh OS. It is the first thing every
  new user touches and the least proven path here.
- **IG Markets.** REST, similar shape to Saxo. `dashboard/broker_protocol.py`
  is the interface to implement; `tests/test_broker_conformance.py` runs against
  any adapter you add.
- **Interactive Brokers.** A persistent socket with async callbacks rather than
  request/response, so it does not fit the current pacing model — budget for an
  architecture change, not an adapter.

Neither the protocol nor the conformance suite is finished. The second broker is
where you find out which parts of the first one were the broker and which were
just Webull; the third will find more.
