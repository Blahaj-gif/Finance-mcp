# Changelog

Dates are the release dates. Entries name what changed and, where it matters,
the bug that caused it — the commit log is the fuller record.

## Unreleased

This began as an audit of three outside reviews of this repository against the
source. They named two real defects between them; checking their claims found
the rest, and the rest are the more serious — the reviews were written from the
README, so everything that needed a file opened to see was still there.

Several entries below were then found by reviewing the fixes for the entries
above them, which is its own argument for the practice.

- **The buying-power check could be switched off from a tool call.** A
  `limit_price` on a `MKT` or `STP` draft was used as the order's price for the
  notional check, but `build_order` attaches a limit price only to a `LIMIT`
  order — so `draft_order("AAPL", "BUY", 1000000, "MKT", 0.0001)` priced a
  million shares at $100, cleared a $333 account, and would have submitted as a
  bare market order. The confirmation, the approval card and the check all read
  as though the price bounded it. A limit price on an order type that will not
  carry one is now refused rather than dropped.
- **A stop order reached the broker with no stop price.** Every adapter's alias
  table accepts `STP`, none of them attaches a price to anything but a `LIMIT`,
  and the tool's own docstring has always said `LMT` or `MKT` — so a `STP` draft
  was built as a stop order carrying no price at all. `draft_order` now accepts
  only the two types it documents. Found by the review of the fix above: its
  refusal message recommended "drop limit_price", and doing exactly that on a
  `STP` order was accepted.
- **A paper draft could be approved into the live account.** Every draft has
  always recorded the `environment` it was raised in, directly beneath the
  `broker` field whose whole purpose is that the dashboard rebuilds the order at
  approval time. Nothing ever read it back, so an order rehearsed against the
  sandbox — and cleared by guards measured against sandbox money — could be
  approved against the real account after a `WEBULL_ENVIRONMENT` change. Both
  fields are now checked at approval by `broker_protocol.draft_refusal`, and a
  draft that names neither is refused rather than assumed to match.
- **Pre-trade guards ran against one draft as though the queue were empty.** N
  drafts that were each individually affordable could sit in the queue together
  costing more than the account holds, or sum to a naked short, with each one
  click from submission. Buying power and inventory now count what the queue has
  already promised.
- **The dashboard bound every interface.** Streamlit leaves `server.address`
  unset, and unset is not localhost: measured, it listens on `0.0.0.0` *and*
  `[::]`, prints a Network URL on the LAN address and — because this app runs
  headless — an External URL too. A live brokerage account, its positions and a
  working submit button, with no authentication in front of them. Now pinned to
  `127.0.0.1` in `.streamlit/config.toml` for the two launch paths that run from
  the repo root, and in `dashboard/cli.py` for an installed copy. Still a
  default, not a lock — pass `--server.address` to override it deliberately.
- **Desktop notifications exported the broker secret.** The notifier is handed an
  environment so alert text travels as data rather than shell syntax, but it was
  handed a copy of the whole of `os.environ` — and `envfile.load` puts every key
  from `.env` there. Every alert exported `WEBULL_APP_SECRET` to a spawned
  PowerShell. It now gets an allowlist of OS plumbing, chosen that way because
  the credential nobody thinks to strip is the one that leaks.
- **The submit path retried an order the vendor's own SDK refuses to retry.**
  `place_order` went through the retrying wrapper, re-sending a byte-identical
  payload up to three times. Webull ships `RetryableMethods: ["GET"]` and
  documents it in words — *"Do no retry when it's not a GET request"* — and
  place is a POST. Nothing establishes a second POST would be rejected:
  `client_order_id` is documented as a uniqueness obligation on the *client*,
  never as an idempotency key, and Saxo states outright that its equivalent is
  not checked for uniqueness. Binding writes are paced and never repeated now;
  reads and preview still retry. The trigger was wrong in both directions too —
  a substring match meant a read timeout carrying the throttle token retried,
  which is the one failure where the order may already be at the engine.
- **An unknown submit outcome was reported as "nothing was sent".** A failure
  that does not *prove* the order was not placed now raises
  `AmbiguousSubmission`, decided by an allowlist that defaults to uncertainty: a
  4xx is the server having received and refused; a 5xx, a 408 and a bare
  transport failure are not. The dashboard names the client order id to look for
  in the order book, warns that resubmitting could place it twice, and marks the
  draft `OUTCOME_UNKNOWN` so it is never offered for approval again.
- **A working stop-loss was rendered blank and offered for cancellation.**
  `open_orders` looked for the instrument leg under `items`; Webull returns a
  combo envelope and puts it under `orders`, so every field fell back to the
  envelope and a real `STOP_LOSS` on a live account rendered with blank symbol,
  side and status. `cancel_order` establishes what it is cancelling now: an
  entry order cancels unobstructed — that is the kill-switch case and must stay
  fast — while a protective stop refuses with the instrument, side, quantity and
  stop price named, and takes `acknowledge_protective=true`. An id that is not
  among the working orders refuses too, because assuming "ordinary entry" is
  exactly the assumption that would silently remove a stop.
- **The staleness gate counted a session whose bar cannot exist yet.**
  `sessions_stale` measured against `previous_trading_day(now + 1 day)`, which
  collapses to *today* on any trading day — so the newest bar in existence read
  "1 trading session old" and `check_connection` reported REACHABLE BUT 1
  SESSION BEHIND on a healthy feed. The reference is the exchange clock now, and
  a session counts once its close has passed and a vendor has had time to
  publish: four hours, measured as the smallest grace that never refuses a fresh
  bar. Tolerances retuned against both feeds, whose weekly stamping conventions
  turn out to be opposite.
- **A regular-session bar was printed as an after-hours print.** Bars are stored
  naive-UTC, correctly, but were printed that way too — so Friday's 15:45 ET
  close rendered as `19:45` with no marker, from a server that has no
  extended-hours data at all. Converted at the render boundary only: intraday
  bars carry their zone, session bars render as a bare date. The table was fixed
  in a second pass after the header, which had been left disagreeing with it.
- **A share count was a ratio of two currencies.** `calculate_position_size`
  divided the account's risk budget by the instrument's risk-per-share without
  checking they were denominated the same way — on a THB-denominated account
  sizing a USD stock, a share count wrong by the exchange rate. The rendering
  said so and nobody read it: `**THB equity**: $320,000.00`. Refused now with
  both codes named. `get_portfolio_risk` likewise grouped rather than summing
  across currencies, and suppresses portfolio beta when the book spans more than
  one.
- **Return series were paired by position, not date.** A holding that missed a
  session had every later return compared against a different day's return for
  every other holding, and the portfolio beta and correlations were computed on
  that. Aligned on dates now, and the bar after a gap is dropped as well — it is
  a multi-day return for the leg that skipped and a one-day return for the rest.
- **Two histories were keyed on the machine's calendar.** IV and portfolio
  snapshots took their default date from a UTC+7 host, so real observations were
  filed on Saturdays and Sundays. They are keyed on the *session* observed now,
  and the eight existing misdated rows were repaired onto it.
- **`IBKR_TLS_INSECURE` was a global kill switch wearing a localhost label.**
  Scoped to loopback, refusing clearly when pointed elsewhere rather than
  silently verifying — and naming a remedy that works, since `IBKR_CACERT` alone
  cannot help a remote gateway whose certificate is issued for `localhost`.
- **The order queue was a file two processes guessed at.** Both did
  read-all/modify/write-all, so a draft added between one process's read and its
  write disappeared. `dashboard/order_queue.py` owns it: every write re-reads,
  patches one draft by id, and replaces atomically. The file is restricted to
  its owner — on Windows through a DACL, because `os.chmod`'s mode is very
  nearly a no-op there. The order submitted is also checked against the order
  previewed, by rebuilding the re-read draft *only to compare*.
- **Pre-trade risk ran once, at draft time.** Buying power and inventory are
  re-checked at approval, against the desk that actually submits rather than
  whichever broker `FINANCE_BROKER` names, with an unpriceable sibling treated
  as a note rather than a refusal.
- **Drafts could not be cancelled.** The execution page has a Cancel button;
  clearing a draft previously meant editing the JSON by hand, which made every
  refusal that says "deal with this draft" a dead end.
- **Parsers reported what they found badly.** A Form 4 mismatch printed both
  sides of the discrepancy identically at four significant figures; an empty
  insider result read as "no insider activity" when a ticker's registrant had
  been succeeded; and a chain the filer explained in a footnote was called a
  mismatch rather than unverified — decided on the `footnoteId` attached to the
  failing balance, so it rests on structure rather than prose.
- **Numbers that meant something other than what they said.** Days-to-expiry
  counted from the machine's calendar rather than the exchange's; backtest win
  rate and profit factor were gross of the fee while the equity curve was
  charged for it; a mixed sweep ranked trading sessions against wall-clock
  hours; an IV rank could read 137/100; and a 13F gave its quarter and filing
  date without saying the positions were months old.
- **The cheap path existed and nothing told the model to take it.**
  `get_company_profile` spans 555 tokens for one section to 6,015 for all of
  them, but its description said *"Everything worth knowing… Start here"* and
  never mentioned narrowing. It now names the levers and what they cost.

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

- **A broker-free install no longer advertises tools that cannot work.**
  Capability gating asked which broker was configured, never whether it could be
  used. Every adapter constructs lazily so that listing tools opens no socket,
  so an empty `.env` built a `WebullBroker` quite happily and all eight account
  and order tools were offered — measured against a fresh environment holding
  only `SEC_USER_AGENT`, four zero-argument ones failed identically with the
  same missing-key error. Adapters now answer `credentials_present()` offline,
  and a broker with none registers nothing: **28 tools that all work, rather
  than 36 of which eight cannot.** The answer may be `None` for a deployment
  whose session lives somewhere this process cannot see — IBKR's Client Portal
  Gateway holds a browser login and takes no token — and `None` fails open,
  because hiding a working tool leaves nobody a way to find out why.
- **Yahoo is a feed, not an apology.** With no broker configured, the price path
  no longer tries a request that could never have succeeded, and the banner says
  "no broker is configured, so prices come from the public feed" instead of
  warning that the primary Webull feed failed to serve a request the user never
  made. A Saxo or IBKR user served by their own broker is no longer warned about
  Webull either.
- **The dashboard's Portfolio tab has an empty state.** It answered a missing
  key with a red box containing a broker exception, which reads as broken
  software rather than an unconfigured one. It now names the variable, says
  where it goes, and lists the six tabs that already work without it.
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
