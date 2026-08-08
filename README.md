# 🚀 Replicant Quant MCP

An **institutional-grade, Human-In-The-Loop (HITL) AI Quantitative Trading System** built natively for Claude Desktop. 

Replicant Quant bridges the gap between LLM reasoning (Claude) and market execution (Webull OpenAPI). It provides Claude with 32 market intelligence tools while maintaining a strict, localized safety firewall via a Streamlit Dashboard.

---

## ✨ Features
* **🧠 Comprehensive AI Market Brain:** Live OHLCV, 50+ technical indicators, short interest, unusual options activity, news, earnings, insider trades, and SEC filings.
* **⚡ Master Payload Endpoint:** `get_comprehensive_profile` fetches technicals, fundamentals, news and consensus in a single round-trip, cutting LLM latency.
* **📐 Risk Tooling:** `calculate_position_size` sizes trades from an account risk budget and an ATR-aware stop; `get_portfolio_risk` reports concentration, portfolio volatility, beta and correlated clusters; `get_options_analytics` adds IV rank, implied move, skew and Black-Scholes greeks.
* **🛡️ Human-In-The-Loop Execution Desk:** Claude **cannot** execute trades. Suggestions are written to a local draft file, and submission requires a successful broker preview followed by your manual approval in the dashboard.
* **📉 Pre-Trade Firewall:** Inspects live account inventory to block naked shorts and verifies per-currency buying power before a draft is accepted. An order that cannot be priced is blocked, never waved through.
* **📊 Streamlit Visual Analytics:** Plotly charts, quantitative backtesting, live portfolio analytics, and adaptive signal breakdown.
* **🚨 Background Alert Daemon:** Native Windows notifications when price or volatility alerts are met, stamped with the bar that triggered them.

---

## 📅 Macro Calendar & Real-Time Filings

Two public sources sit alongside the broker feed, so Claude can see the events that move price as well as the price itself.

| Tool | What it gives you |
|---|---|
| `get_economic_calendar` | Scheduled US releases — CPI, PPI, jobs, JOLTS and more — with date, time and reference period, plus the latest actual prints. |
| `get_macro_data` | Historical CPI, core CPI, unemployment, payrolls, PPI and wages with MoM/YoY changes. |
| `get_edgar_filings` | SEC filings in three modes: one company's filings, the all-registrant live feed, or full-text search across filing bodies. |

**On latency.** EDGAR acceptance timestamps are exact to the second, so an earnings 8-K (item `2.02`) is visible as soon as it is accepted. The delay you experience is your own polling interval, not the feed.

**Rate and quota handling.** BLS allows 25 API queries a day unregistered; the release-schedule pages are ordinary web fetches and deliberately do *not* draw on that budget. Results are cached (6h for macro series, 24h for schedules, 2min for filings), so a repeated calendar call costs nothing. The SEC's 10 req/s ceiling is enforced at the client.

**Normalization.** Filer names and release text arrive in mixed scripts and number conventions. A dedicated layer folds Unicode to a canonical form, expands atomic Latin letters that have no decomposition (`Ærø` → `AEro`, not `r`), converts non-ASCII digits, and parses numbers written US, European, Swiss or Indian style — including accounting negatives like `(1,234.56)`.

---

## 🔒 Data Integrity

Market data is the foundation everything else rests on, so it is checked rather than trusted. Every price frame — from either source — passes a single gate before any tool sees it:

* **Bar ordering is enforced.** The Webull API returns bars newest-first. Frames are sorted ascending and the invariant is asserted, so `.iloc[-1]` is always the most recent bar. *(Without this, tools reported the oldest bar of the window as the current price and computed every indicator on a time-reversed series.)*
* **Staleness is measured in trading sessions**, using a built-in NYSE calendar — not calendar days, which cannot tell a holiday weekend from an outage.
* **Sanity checks** reject NaN prices and impossible OHLC bars.
* **Failures are errors, not text.** Tools raise real MCP errors rather than returning `"Error: ..."` as content, so a failure can never be mistaken for a finding.
* **Source substitution is announced.** When the Webull feed fails and Yahoo serves the request, every affected tool says so.

Run the suite with `pytest`. It is entirely offline — no credentials, no network, no orders.

---

## 📥 1-Click Installation
You do **not** need to install Python, SDKs, or manually configure Claude. The installer dynamically downloads and wires everything for you.

1. Unzip this package anywhere on your computer (e.g. your Desktop).
2. Double-click the **`install.bat`** file.
3. The automated installer will:
   - Install the `uv` python engine globally if missing.
   - Inject the MCP server configuration dynamically into your `claude_desktop_config.json`.
   - Drop an **"MCP Dashboard"** shortcut on your Desktop.
   - Generate a clean `.env` template in the folder for your API keys.

---

## 🔑 Authentication
1. Open the newly generated `.env` file located in this folder.
2. Paste your Webull `WEBULL_APP_KEY` and `WEBULL_APP_SECRET`.
3. Save the file.

`.env` and `conf/token.txt` are gitignored, and SDK logs are credential-redacted at write time — the Webull SDK dumps the full signed request (key, HMAC signature, access token) at ERROR level, which routine rate-limit responses would otherwise write straight to disk.

### Optional settings

| Variable | Default | Purpose |
|---|---|---|
| `WEBULL_ACCOUNT_ID` | *(unset)* | Pin a specific account. **Required if your login has more than one** — the server refuses to guess rather than silently trading the wrong account. |
| `WEBULL_MIN_REQUEST_INTERVAL` | `0.25` | Seconds between Webull API calls. Pacing keeps list-sweeping tools (sector heatmap, watchlist scans) off the rate limiter. |
| `WEBULL_MAX_RETRIES` | `3` | Attempts before a rate-limited call gives up and falls back. |
| `WEBULL_RETRY_BACKOFF` | `0.75` | Base seconds for exponential backoff on HTTP 429. |
| `WEBULL_REGION_ID` | `th` | Webull region. Also gates the Yahoo `.BK` ticker fallback. |
| `SEC_USER_AGENT` | *(unset)* | **Required for the EDGAR tools.** The SEC's fair-access policy demands a descriptive User-Agent with a real contact address, e.g. `Your Name (you@example.com)`. Requests are refused locally without one rather than sent anonymously, which risks an IP ban. |
| `BLS_API_KEY` | *(unset)* | Optional. BLS works with no key at 25 queries/day; a [free key](https://data.bls.gov/registrationEngine/) raises it to 500/day and unlocks longer history. |

---

## 🧠 Usage Architecture

### 1. The Brain (Claude Desktop)
Restart your Claude Desktop application. Ask Claude to analyze a ticker (e.g., *"Run a comprehensive scan on NVDA and draft a trade if the MACD is crossing over"*). Claude will dynamically ingest the live Webull data and reason through the logic.

### 2. The Command Center (Streamlit)
Double-click the **MCP Dashboard** shortcut on your desktop. This is your visual interface.
- **Charts:** Review the AI's technical analysis overlays visually.
- **Portfolio:** Check your live P&L and Net Liquidation.
- **Execution Desk:** Review Claude's drafted trades. Submission is two-step by design: **① Preview with Webull** asks the broker to price and validate the order (non-binding), and only then does **② Approve & Submit** unlock. An order the broker will not preview is never sent, and a failed submission leaves the draft pending rather than marking it executed.

---
*Disclaimer: This is an open-source project for educational and experimental quantitative research. Algorithmic trading carries significant financial risk.*
