# Finance MCP

A **Human-In-The-Loop (HITL) market research and trading desk** for Claude Desktop.

Finance MCP bridges the gap between LLM reasoning (Claude) and market execution (Webull OpenAPI). It provides Claude with 37 market intelligence tools while maintaining a strict, localized safety firewall via a Streamlit Dashboard.

---

## Features
* **Comprehensive AI Market Brain:** Live OHLCV, 50+ technical indicators, short interest, unusual options activity, news, earnings, insider trades, and SEC filings.
* **One-Call Company Profile:** `get_company_profile` fetches the business, filed financials, SEC filings, insider activity, short interest, price and news **concurrently** (2.3x faster than sequential), each section labelled with how much weight its numbers carry.
* **Risk Tooling:** `calculate_position_size` sizes trades from an account risk budget and an ATR-aware stop; `get_portfolio_risk` reports concentration, portfolio volatility, beta and correlated clusters; `get_options_analytics` adds IV rank, implied move, skew and Black-Scholes greeks.
* **Human-In-The-Loop Execution Desk:** Claude **cannot** execute trades. Suggestions are written to a local draft file, and submission requires a successful broker preview followed by your manual approval in the dashboard.
* **Pre-Trade Firewall:** Inspects live account inventory to block naked shorts and verifies per-currency buying power before a draft is accepted. An order that cannot be priced is blocked, never waved through.
* **Streamlit Visual Analytics:** Plotly charts, quantitative backtesting, live portfolio analytics, and adaptive signal breakdown.
* **Background Alert Daemon:** Native Windows notifications when price or volatility alerts are met, stamped with the bar that triggered them.

---

## Macro Calendar & Real-Time Filings

Two public sources sit alongside the broker feed, so Claude can see the events that move price as well as the price itself.

| Tool | What it gives you |
|---|---|
| `get_economic_calendar` | Scheduled US releases — CPI, PPI, jobs, JOLTS and more — with date, time and reference period, plus the latest actual prints. |
| `get_macro_data` | Historical CPI, core CPI, unemployment, payrolls, PPI and wages with MoM/YoY changes. `source="markets"` gives policy rates, the yield curve and financial conditions from FRED, the ECB and the Bank of England. |
| `get_edgar_filings` | SEC filings in three modes: one company's filings, the all-registrant live feed, or full-text search across filing bodies. |
| `get_insider_activity` | Parsed Form 3/4/5 — who traded, at what price, **whether the sale was under a Rule 10b5-1 plan**, and opening positions. `forms="144"` gives *proposed* sales, filed ahead of the trade, with the plan-adoption date. |
| `read_filing` | Form-aware: executive pay from a DEF 14A, the cover page of a 13D/13G, the press release from an 8-K, a named Item (Risk Factors, MD&A) from a 10-K, or a text search. |
| `get_institutional_holdings` | Latest 13F-HR portfolio, positions merged across manager rows. `source="NPORT"` gives a registered fund's monthly portfolio, including the bonds and derivatives 13F omits. |

**Filings are parsed, not forwarded.** Most of what an analyst wants from a filing is already a machine-readable field. "Was that sale pre-scheduled?" is `<aff10b5One>` in the Form 4 XML — a boolean. So the server extracts and answers rather than handing over a document: one Form 4 is ~6,600 tokens of raw XML, and a single 10-K is ~610,000 tokens, which is three times a 200k context window. `get_insider_activity` also separates real decisions (codes P/S) from compensation mechanics — grants, option exercises, and shares withheld for tax — which are routinely misreported as "insiders sold $X".

**On latency.** EDGAR acceptance timestamps are exact to the second, so an earnings 8-K (item `2.02`) is visible as soon as it is accepted. The delay you experience is your own polling interval, not the feed.

**Rate and quota handling.** BLS allows 25 API queries a day unregistered; the release-schedule pages are ordinary web fetches and deliberately do *not* draw on that budget. Results are cached (6h for macro series, 24h for schedules, 2min for filings), so a repeated calendar call costs nothing. The SEC's 10 req/s ceiling is enforced at the client.

**Normalization.** Filer names and release text arrive in mixed scripts and number conventions. A dedicated layer folds Unicode to a canonical form, expands atomic Latin letters that have no decomposition (`Ærø` → `AEro`, not `r`), converts non-ASCII digits, and parses numbers written US, European, Swiss or Indian style — including accounting negatives like `(1,234.56)`.

### Getting a BLS key (optional, free, ~2 minutes)

Unregistered access is capped at 25 API queries a day. To lift it to 500:

1. Register at [data.bls.gov/registrationEngine](https://data.bls.gov/registrationEngine/) — the key arrives by return email.
2. Add `BLS_API_KEY=<your key>` to `.env` and restart the server.
3. Run the `validate_bls_key` tool to confirm it was accepted.

That last step matters: a mistyped key does not raise an error, it silently drops you back to the 25/day tier, which only shows up days later as an exhausted quota. Registered access also extends history from 10 to 20 years and returns BLS's own computed percentage changes, which the server checks its own arithmetic against.

Use `get_data_sources` at any time to see every source's configuration and remaining quota.

---

## Where the numbers come from

Not all data carries the same weight, and the tools say which is which.

| Class | Source | Validation |
|---|---|---|
| **Prices & bars** | Webull OpenAPI, Yahoo fallback | Full integrity gate: ordering, session-based staleness, OHLC sanity |
| **Filed financials** | SEC EDGAR XBRL | Authoritative — the filing itself, stamped with form and filing date |
| **Macro** | BLS | Official series; our MoM/YoY cross-checked against BLS's own figures on the registered tier |
| **Third-party fundamentals** | Yahoo | Cross-checked against the XBRL filing where a comparable figure exists; disagreements are reported |
| **Consensus score** | Computed here | A fixed-weight heuristic, labelled as such — it underperformed buy & hold in backtest |

`get_company_financials` returns the filed figures directly. Where Yahoo and the filing disagree, the filing wins and the tool says so.

**IV rank** is the one measure that cannot be sourced authoritatively for free: no public feed publishes implied-volatility history. Rather than pretend otherwise, the server records one ATM IV observation per symbol per day as options are queried, and reports a true IV rank once a symbol has 30 days of its own history. Until then it shows a realised-volatility proxy, explicitly labelled, with a count of how many more observations are needed.

---

## Data Integrity

Market data is the foundation everything else rests on, so it is checked rather than trusted. Every price frame — from either source — passes a single gate before any tool sees it:

* **Bar ordering is enforced.** The Webull API returns bars newest-first. Frames are sorted ascending and the invariant is asserted, so `.iloc[-1]` is always the most recent bar. *(Without this, tools reported the oldest bar of the window as the current price and computed every indicator on a time-reversed series.)*
* **Staleness is measured in trading sessions**, using a built-in NYSE calendar — not calendar days, which cannot tell a holiday weekend from an outage.
* **Sanity checks** reject NaN prices and impossible OHLC bars.
* **Failures are errors, not text.** Tools raise real MCP errors rather than returning `"Error: ..."` as content, so a failure can never be mistaken for a finding.
* **Source substitution is announced.** When the Webull feed fails and Yahoo serves the request, every affected tool says so.

Run the suite with `pytest`. It is entirely offline — no credentials, no network, no orders.

---

## Installation

### Path A — the installer (Windows, no Python required)

1. Unzip this package anywhere (e.g. your Desktop).
2. Double-click **`install.bat`**.
3. It installs the `uv` Python engine if missing, registers the server in
   `%APPDATA%\Claude\claude_desktop_config.json` under the name `finance`,
   writes a `.env` template, and drops a **Finance MCP Dashboard** shortcut on
   your Desktop.
4. Fill in `.env` (see [Authentication](#authentication)).
5. **Restart Claude Desktop.** The server is only read at startup.

The installer prints an `ACTION NEEDED` block if `WEBULL_APP_KEY` or
`SEC_USER_AGENT` are still at their placeholder values, so a half-configured
install does not look like a finished one.

Nothing is installed system-wide beyond `uv`; dependencies are resolved into a
cache the first time the server or dashboard runs, so the first launch is
slower than the rest.

### Path B — clone the repo

No installer, no shortcut. You wire up the two entry points yourself.

```bash
git clone <repo> && cd finance-mcp
uv venv && uv pip install -e ".[dev,dashboard]"
cp .env.example .env      # then edit it — see Authentication below
```

**The MCP server** (what Claude talks to). Add this to
`%APPDATA%\Claude\claude_desktop_config.json` on Windows, or
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, and
restart Claude Desktop:

```json
{
  "mcpServers": {
    "finance": {
      "command": "uv",
      "args": ["run", "--with", "pandas", "--with", "numpy", "--with", "fastmcp",
               "--with", "yfinance", "--with", "tabulate", "--with", "lxml",
               "--with", "html5lib", "--with", "webull-openapi-python-sdk",
               "/absolute/path/to/finance_mcp.py"]
    }
  }
}
```

Use an **absolute** path, and forward slashes even on Windows. Claude Code users
can instead run `claude mcp add finance -- uv run /absolute/path/to/finance_mcp.py`.

**The dashboard** (what you look at). Run it from the **repo root**, not from
`dashboard/` — `app.py` resolves its sibling modules and `.streamlit/config.toml`
relative to the working directory, and launching from elsewhere loses the theme:

```bash
streamlit run dashboard/app.py            # inside the venv
# or, without activating anything:
uv run --with streamlit --with plotly --with pandas --with numpy \
       --with yfinance --with lxml --with html5lib --with tabulate \
       --with webull-openapi-python-sdk streamlit run dashboard/app.py
```

It opens on <http://localhost:8501>. Add `--server.port 8899` to move it.

**The alert daemon** (optional, Windows toast notifications). The dashboard
starts it automatically in a background thread; to run it standalone:

```bash
python -m dashboard.alert_watcher
```

**Verify the install** — `pytest` runs the whole suite offline, with no
credentials and no network:

```bash
pytest -q
```

Then ask Claude to run **`get_data_sources`** — it reports which credentials are
configured, which feeds are reachable and what quota is left, without touching
your account. **`check_connection`** confirms the Webull session specifically.

---

## Authentication
1. Open the newly generated `.env` file located in this folder.
2. Paste your Webull `WEBULL_APP_KEY` and `WEBULL_APP_SECRET`.
3. Save the file.

`.env` and `conf/token.txt` are gitignored, and SDK logs are credential-redacted at write time — the Webull SDK dumps the full signed request (key, HMAC signature, access token) at ERROR level, which routine rate-limit responses would otherwise write straight to disk.

### Optional settings

| Variable | Default | Purpose |
|---|---|---|
| `WEBULL_ENVIRONMENT` | `prod` | `prod` trades the real account. **`paper`** (aliases `uat`, `sandbox`, `simulated`) routes every call to Webull's simulated environment for your region, so the whole approval path — draft, preview, approve, submit — can be rehearsed without risking anything. The dashboard shows `LIVE` or `PAPER` beside the wordmark and on the Execution tab. If no sandbox host is published for your region the client refuses to start rather than falling through to production. |
| `WEBULL_ACCOUNT_ID` | *(unset)* | Pin a specific account. **Required if your login has more than one** — the server refuses to guess rather than silently trading the wrong account. |
| `WEBULL_MIN_REQUEST_INTERVAL` | `0.25` | Seconds between Webull API calls. Pacing keeps list-sweeping tools (sector heatmap, watchlist scans) off the rate limiter. |
| `WEBULL_MAX_RETRIES` | `3` | Attempts before a rate-limited call gives up and falls back. |
| `WEBULL_RETRY_BACKOFF` | `0.75` | Base seconds for exponential backoff on HTTP 429. |
| `WEBULL_REGION_ID` | `th` | Webull region. Also gates the Yahoo `.BK` ticker fallback. |
| `SEC_USER_AGENT` | *(unset)* | **Required for the EDGAR tools.** The SEC's fair-access policy demands a descriptive User-Agent with a real contact address, e.g. `Your Name (you@example.com)`. Requests are refused locally without one rather than sent anonymously, which risks an IP ban. |
| `BLS_API_KEY` | *(unset)* | Optional. BLS works with no key at 25 queries/day; a [free key](https://data.bls.gov/registrationEngine/) raises it to 500/day and unlocks longer history. |

---

## Usage Architecture

### 1. The Brain (Claude Desktop)
Restart your Claude Desktop application. Ask Claude to analyze a ticker (e.g., *"Run a comprehensive scan on NVDA and draft a trade if the MACD is crossing over"*). Claude will dynamically ingest the live Webull data and reason through the logic.

### 2. The Command Center (Streamlit)
Double-click the **Finance MCP Dashboard** shortcut, or run
`streamlit run dashboard/app.py` from the repo root. Eight tabs:

| Tab | What it is for |
|---|---|
| **Charts** | Candles with overlays, a volume pane and the forecast cone. Drag to pan, scroll to zoom, double-click to reset; drag a single axis to scale it alone. Weekends and market holidays are collapsed, so there are no blank stretches. |
| **Backtest** | Runs the adaptive consensus rules over the loaded window and reports CAGR, Sharpe, max drawdown, profit factor and exposure. |
| **Journal** | Theses Claude logged via `log_journal_entry`, with a drift warning when the logged price has moved away from the market. |
| **Signals** | The four indicator verdicts behind the consensus score, and the regime weighting matrix that produced them. |
| **Execution** | The approval desk. See below. |
| **Portfolio** | Live balance, buying power and open positions with P&L, straight from the broker. |
| **Alerts** | Price, RSI and MACD-cross alerts; the daemon fires Windows notifications and stamps which bar triggered. |
| **Data** | Every computed indicator column for the loaded window, newest first. |

**DISPLAY** in the top right switches the visual theme (**Terminal**, the
default; **Research**; **Slate**), the chart overlay palette and row density.

**The Execution tab is the only place an order can be submitted.** Submission is
two-step by design: **1 — Preview with Webull** asks the broker to price and
validate the order (non-binding), and only then does **2 — Approve and submit**
unlock. An order the broker will not preview is never sent, and a failed
submission leaves the draft pending rather than marking it executed.

### 3. What Claude can and cannot do

| | |
|---|---|
| Read prices, filings, macro series, your balance and your positions | **yes** |
| Draft an order to a local JSON file, and preview it with the broker | **yes** |
| Submit an order | **no** — the submit button exists only in the dashboard, and only after a broker preview |

There is no configuration flag that grants Claude submission rights. Removing
the human from that step would require editing the source.

---
*Disclaimer: This is an open-source project for educational and experimental quantitative research. Algorithmic trading carries significant financial risk.*
