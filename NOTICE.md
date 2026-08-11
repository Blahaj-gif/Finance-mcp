# Notices

These used to sit at the bottom of `LICENSE`. They are here instead because
anything appended to the MIT text makes GitHub's licence classifier report
"Other" rather than MIT — which costs the project a licence badge and a place on
lists that require an OSI-detected one, for no gain. `LICENSE` is now the MIT
text and nothing else. Nothing below has changed in substance.

---

## Not financial advice

**This software can place real orders against a real brokerage account.**

The assistant can draft an order and ask the broker to price it. It cannot send
one — that button exists only in the dashboard, after a broker preview, and no
configuration flag grants submission rights. Removing the human from that step
would require editing the source.

The consensus score is a hand-tuned, fixed-weight heuristic over five
indicators. Backtested over 250 daily bars it **underperformed buy-and-hold on
MU, SPY and NVDA**, and it is labelled as such wherever it appears. Read it as a
summary of what the indicators currently say, not as an edge.

The regime classifier's thresholds (ADX 23 and 20, Bollinger width at 1.5σ) are
conventional and unvalidated here. Nothing in this project has shown they
separate tradable trends from untradeable ones on any particular instrument.

Rehearse the approval flow with `WEBULL_ENVIRONMENT=paper` before running it
against money. **You are responsible for every order you approve.**

## Unverified broker adapters

`dashboard/brokers/saxo.py` and `dashboard/brokers/ibkr.py` have **never been
run against their APIs**. They are written from published REST documentation.
Both carry `verified = False`, announce it in every result they produce, and
raise rather than guess on paths the documentation did not pin down.

Only the Webull adapter has been exercised end to end against a live account.
See [HELP-WANTED.md](HELP-WANTED.md).

## Third-party data

Public sources are fetched live and nothing is redistributed here.

Series sourced from **FRED** are redistributed by the Federal Reserve Bank of
St. Louis under the terms of their original publishers. Two carry third-party
copyright and are **not licensed for redistribution**:

| Series | Copyright holder |
|---|---|
| `hy_spread` | ICE BofA index data |
| `vix` | CBOE |

They are fetched live for personal use and **no values are bundled in this
repository or its distributions**. Anyone building on this commercially should
check those terms themselves rather than rely on this note.

**SEC EDGAR** requires a descriptive User-Agent with a real contact address
under its fair-access policy. The filings tools refuse to send a request
without one rather than risk an IP ban on your address.

**Yahoo Finance** is used as a price fallback and has no public API terms
granting redistribution; it is queried for personal use only.
