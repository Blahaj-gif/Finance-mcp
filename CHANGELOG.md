# Changelog

Dates are the release dates. Entries name what changed and, where it matters,
the bug that caused it — the commit log is the fuller record.

## 0.3.1 — 2026-08-12

**Security: earlier releases contained the author's account state. Upgrade, and
do not install 0.2.0 through 0.3.0.**

`include-package-data` defaults to *true* for a pyproject config, and setuptools
globs the filesystem rather than the git index. The dashboard writes its runtime
state beside its own code, so four published releases carried:

- a real Webull account id and the timestamp of its live-trading consent
- a portfolio's positions, cost basis and net liquidation, day by day
- a live order with the broker's own order id

No API key, secret or token was exposed, and nothing in those files grants
access to anything — this is personal financial data, not a credential
compromise. Nothing in the repository showed it either: every one of those files
is gitignored, which is exactly why review never caught it.

It was a correctness bug as well: a fresh install began with somebody else's
drafts already in the approval queue.

Fixed by setting `include-package-data = false` and deleting a package-data glob
that shipped nothing legitimate — the theme it claimed to carry is Python, and
the golden vectors live in `tests/`, which the package excludes. `.gitignore`
now covers `dashboard/*.json` as a glob; listing the files one by one had missed
`iv_history.json`. Tests read the packaging configuration and ask git directly,
so this fails before a build rather than after a publish.

## Unreleased

- **MCP tool annotations.** Tools declare `readOnlyHint`; `cancel_order`
  is the only `destructiveHint`. The safety claim was prose a model had to be
  persuaded by; it is now machine-readable, and a client that respects
  annotations can enforce it.
- **Macro release watching**, off by default behind `FINANCE_MACRO_WATCH=1`. A
  background thread fetches a BLS print as it publishes so it is in hand before
  anyone asks. Serial, 10 requests per 10 seconds against a documented ceiling
  of 50, and it stops the moment the print lands — a punctual release costs one
  call.
- **Release-aware macro caching.** Near a scheduled release the cache drops from
  six hours to three seconds, judged on the reference period rather than a
  timestamp.
- **Parser reconciliation.** 13F holdings are checked against the entry count
  and total value the filing declares on its own cover page, and the result is
  reported in the tool output. Verified across ten institutions and 82,701 rows,
  values matching to the dollar. Property-based tests over `parse_number` and
  all three reconciliations, mutation-checked.
- **CI builds the wheel.** The source checkout always has every module, which is
  why the suite could not catch `dashboard/brokers` being absent from the
  distribution. CI now builds, installs into a clean environment, and runs a
  real MCP handshake.
- Fixed: a test that passed on every day except the one its fixture named; a
  quota reserve larger than the whole unregistered BLS quota, which silently
  disabled release polling; `live_signals` failing mid-render on a stale module
  rather than at import.

## 0.3.0 — 2026-08-11

### Three brokers behind one protocol

- **Interactive Brokers adapter**, through the Client Portal **Web** API rather
  than the TWS socket API. An earlier note in this repo said IBKR needed "an
  architecture change, not an adapter"; that was written with only the socket
  API in mind and was wrong.
- IBKR can answer a placement with **warnings instead of an order**, each
  needing confirmation before anything transmits. Client libraries answer those
  from a table of canned replies. This raises `ConfirmationRequired` to the
  person who approved the order.
- `open_orders()` and `accounts()` added to the broker protocol. Every broker
  has both; none agreed on the shape.
- The eight account and order tools now route through `broker_protocol.py`
  instead of Webull's SDK.
- **Prices come from your broker** — Saxo's `/chart/v1/charts`, IBKR's
  `/iserver/marketdata/history` — for every tool, not just the account ones.
  Both sort ascending before returning.
- `contract_rules()` fetches real tick and lot sizes; `rule_violations()` uses
  them when supplied.
- Broker-specific tools where there is no common shape: `saxo_corporate_actions`
  and `ibkr_market_scanner`.

### Tools are registered per account

- A tool appears only when the configured broker can serve it. With
  `FINANCE_BROKER=saxo`, `cancel_order` is absent — Saxo cancels by its own
  order id and documents no mapping from ours.
- Capability is resolved per **account**, not per broker name: what the SDK
  implements ∩ what the regional entity serves ∩ what the account is entitled
  to. Cached, and a probe can only ever withdraw a capability.

### The regime classifier says which test it applied

- "Mixed Trend" covered three unrelated situations — the warm-up before ADX
  exists, a trend whose direction the EMAs dispute, and the gap between the two
  thresholds. On 250 bars of real data that was 28% of the series under one
  word, and one of the three was not a reading at all. Now `Insufficient
  History`, `Conflicted Trend` and `Transitional`.
- The expansion test compared Bollinger width against a rolling mean of itself;
  before that second window filled it compared against NaN, which is False, so
  those bars were labelled from ADX alone.
- The consensus score is NaN where the regime is unknown, rather than blending
  two undefined halves into a number.

### Three readings the strip could not make

- Volume against its own 20-bar average, the ADX behind the regime label, and
  the distance to the nearest high-volume node. None is scored into the
  BUY/SELL heuristic.
- Fixed: a 100-bar volume profile of a 60-bar frame returned no nodes, so the
  panel said "unknown" on a window that had a perfectly good profile in it.

### Fixes

- **Buying power showed `0.00` when the lookup failed** — indistinguishable
  from an empty account, and only one of those means you cannot trade.
- The portfolio panel presented base-currency totals beside a single USD line
  as though they were separate money. They are the same holdings converted; it
  now says so and lists every currency line.
- A draft raised against one broker could be approved and sent to another.
  Drafts record their broker; the execution page refuses a foreign one.
- The disk bar cache was shared across brokers once the feed followed
  `FINANCE_BROKER`. Namespaced.
- IBKR sessions expire after ~6 minutes idle. Kept warm on the way in, without
  a background thread outliving the call that made it.
- `live_signals` binds shared thresholds at import, so a process holding a
  stale `dashboard.indicators` fails once at import rather than mid-render.

## 0.2.2 — 2026-08-11

- Ownership proof for the MCP registry (`mcp-name:` in the published README).
  Listed at `io.github.Blahaj-gif/hitl-finance-mcp`.

## 0.2.1 — 2026-08-11

- **The install instructions named someone else's package.** `finance-mcp` on
  PyPI is an unrelated active project; this one publishes as
  `hitl-finance-mcp` while the *command* stays `finance-mcp`, which is how the
  docs drifted. A test now scans every install verb in the docs and installers.
- Nine broker environment variables the code reads were in no template —
  `.env.example`, both installers and `server.json` all described a
  Webull-only server.
- The dashboard used `use_container_width=`, which Streamlit documented for
  removal after 2025-12-31, against an unpinned dependency. Migrated and
  floored at `streamlit>=1.49`.

## 0.2.0 — 2026-08-11

First published release.

- MCP server over Webull OpenAPI, Yahoo Finance, SEC EDGAR, BLS, the Federal
  Reserve and the BEA, with a Streamlit dashboard as the sole path to
  execution.
- Saxo Bank adapter, built from published documentation and marked unverified.
- Renamed from `finance-mcp` to `hitl-finance-mcp` — the former belongs to
  another project.
- Fixed before it shipped: `dashboard/brokers` was missing from the wheel, so
  the installed server died on import. `packages = ["dashboard"]` is an
  explicit list, not a prefix, and only a clean-venv install shows it.
