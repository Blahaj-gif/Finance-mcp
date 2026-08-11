# Contributing

The most valuable contribution here is not code. It is **running something
against a real account and reporting what happened** — see
[HELP-WANTED.md](HELP-WANTED.md).

## Running it

```bash
git clone https://github.com/Blahaj-gif/Finance-mcp
cd Finance-mcp
uv venv && uv pip install -e ".[dev,dashboard]"
cp .env.example .env          # fill in SEC_USER_AGENT at minimum
pytest -q                     # ~1200 tests, about 90 seconds, no network needed
```

The suite runs offline. If a test needs credentials it is written wrong — one
did once, and it passed locally because a developer `.env` existed while CI
failed. That is the failure mode to watch for.

## What the tests are for

They are not coverage. **Almost every test in this repository names an incident**
— the bug it exists to stop coming back — in its docstring. If you add a test,
say what it caught. If you cannot say, the test may not be worth its runtime.

```python
def test_the_warmup_is_not_reported_as_a_reading():
    """
    Absence must never look like a measurement. The opening bars of any series
    have no ADX and no Bollinger width, and the old classifier called them
    "Mixed Trend" — indistinguishable in the output from a genuine finding.
    """
```

## House rules

These are not style preferences. Each one is a bug that shipped.

**Fail loud, never drift.** A value you could not compute is not zero, and
absence must never look like a measurement. `buying_power` returning `0.00` on a
failed lookup is indistinguishable from an empty account, and only one of those
means you cannot trade.

**Say where a number came from.** Every price carries its feed and the age of
the bar it quotes. A fallback that does not announce itself is how a divergence
between two feeds went unnoticed for months.

**Refuse rather than guess.** An ambiguous ticker across exchanges, a currency
the broker cannot answer for, a cancel that cannot be keyed — these raise. In a
broker adapter a wrong guess is not a log line, it is an order for the wrong
quantity.

**Never widen what the assistant can do to the market.** Reads and drafts are
the tool surface. Submission lives in the dashboard behind a human click, and a
pull request that adds a code path from a tool to a live order will not be
merged whatever it is for.

**Nothing on stdout.** The server speaks JSON-RPC over it; a stray `print` is a
corrupted stream. Diagnostics go to stderr, and there is a test that checks.

## Adding a broker

`dashboard/broker_protocol.py` is the interface.
`tests/test_broker_conformance.py` runs the same suite against every adapter —
add yours to `ADAPTERS` and it inherits about a hundred tests.

Declare a `CAPABILITIES` set of what you actually implement. Tools register
against it, so an adapter that cannot cancel simply is not offered a cancel
tool, rather than being offered one that refuses.

**Do not set `verified = True`.** That flag means somebody ran the adapter
against the real API and reported what happened. A test asserts it is still
`False` for Saxo and IBKR, so it cannot be flipped from a reading of the code.

## Pull requests

CI runs the suite on Python 3.10 and 3.12. It is not a formality — it has
caught a test that only passed because of a local `.env`, and a second that
assumed Windows.

Small and explained beats large and quiet. If a change alters what a number
means, say so in the message; the commit log here is the project's real
documentation.
