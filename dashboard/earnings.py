"""
Earnings: when a company reports, and whether that date is actually settled.

Shared by the MCP tool and the dashboard so the two cannot drift into saying
different things about the same quarter.

The load-bearing idea is that a report date has three very different states and
they look identical once formatted:

  confirmed   Yahoo publishes one date and its two feeds agree on it
  disputed    Yahoo's calendar and its own earnings table disagree (they do --
              AAPL showed 30 Oct on one and 29 Oct on the other)
  estimated   Yahoo publishes a window, because it does not know

Rendering an estimate as a date is wrong by up to a week, and nothing downstream
can tell. Only the SEC settles it, and only after the fact: an 8-K carrying Item
2.02 is the filing a company makes when it releases results, and its acceptance
timestamp is exact to the second.
"""
import datetime

try:
    from dashboard import econ_calendar as ec
    from dashboard import webull_client as wc
except ImportError:  # imported as a top-level module from dashboard/
    import econ_calendar as ec
    import webull_client as wc

STATUS_CONFIRMED = "confirmed"
STATUS_DISPUTED = "disputed"
STATUS_ESTIMATED = "estimated"
STATUS_UNKNOWN = "unknown"

STATUS_NOTE = {
    STATUS_CONFIRMED: "Yahoo's two feeds agree on this date. That is Yahoo's "
                      "assessment, not a company confirmation.",
    STATUS_DISPUTED: "Yahoo's calendar and its own earnings table disagree, so "
                     "the date is not settled. Treat the pair as a window.",
    STATUS_ESTIMATED: "Yahoo publishes a window, not a date. Sizing risk to the "
                      "first day of an estimated window is the mistake this flag "
                      "exists to stop.",
    STATUS_UNKNOWN: "Yahoo publishes no upcoming report date for this symbol.",
}


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return None


def next_report(symbol: str) -> dict:
    """
    The upcoming report, with how much the date can be trusted.

    Returns {"symbol", "status", "date", "window", "note", "estimate": {...}}.
    Never raises: a symbol Yahoo has nothing for is a status, not an error.
    """
    out = {"symbol": symbol.upper(), "status": STATUS_UNKNOWN, "date": None,
           "window": [], "estimate": {}, "note": STATUS_NOTE[STATUS_UNKNOWN]}
    try:
        ticker = wc.yahoo_ticker(symbol.upper())
    except Exception:
        return out

    try:
        cal = ticker.calendar or {}
    except Exception:
        cal = {}

    raw = cal.get("Earnings Date") or []
    window = [d for d in (_as_date(x) for x in
                          (raw if isinstance(raw, (list, tuple)) else [raw])) if d]
    out["window"] = window

    for key, name in (("Earnings Average", "avg"), ("Earnings Low", "low"),
                      ("Earnings High", "high")):
        if isinstance(cal.get(key), (int, float)):
            out["estimate"][name] = float(cal[key])

    # The second opinion: the newest row in the earnings table that has no
    # reported EPS yet is the same upcoming quarter, from a different feed.
    table_date = None
    try:
        frame = ticker.earnings_dates
        if frame is not None and not frame.empty and "Reported EPS" in frame.columns:
            pending = frame[frame["Reported EPS"].isna()]
            if not pending.empty:
                table_date = pending.index.min().date()
    except Exception:
        pass
    out["table_date"] = table_date

    if len(window) > 1 and window[0] != window[-1]:
        out["status"], out["date"] = STATUS_ESTIMATED, window[0]
    elif window:
        out["date"] = window[0]
        out["status"] = (STATUS_DISPUTED if table_date and table_date != window[0]
                         else STATUS_CONFIRMED)
    elif table_date:
        out["date"], out["status"] = table_date, STATUS_ESTIMATED

    out["note"] = STATUS_NOTE[out["status"]]
    return out


def upcoming(symbols, days_ahead=120, today=None) -> tuple[list, list]:
    """
    Report dates for several symbols, soonest first. Returns (rows, problems).

    One symbol failing never removes the others: a watchlist that silently drops
    a name reads as "nothing due" for that name.
    """
    today = today or datetime.date.today()
    horizon = today + datetime.timedelta(days=days_ahead)
    rows, problems = [], []

    for sym in symbols:
        try:
            info = next_report(sym)
        except Exception as e:
            problems.append(f"{sym}: {str(e)[:80]}")
            continue
        if info["date"] is None:
            problems.append(f"{sym}: no report date published")
            continue
        if not (today - datetime.timedelta(days=1) <= info["date"] <= horizon):
            continue
        info["days_away"] = (info["date"] - today).days
        rows.append(info)

    rows.sort(key=lambda r: r["date"])
    return rows, problems


def last_reported(symbol: str, limit: int = 4) -> list:
    """
    Quarters already released, confirmed against SEC 8-K Item 2.02.

    Yahoo supplies the estimate and the reported figure; EDGAR supplies proof
    the quarter was actually released and the moment it happened. Surprise is
    recomputed from the two EPS figures -- yfinance's own Surprise(%) column is
    already a percentage, and multiplying it by 100 turned every AAPL beat into
    "+674%".
    """
    rows = []
    try:
        frame = wc.yahoo_ticker(symbol.upper()).earnings_dates
    except Exception:
        frame = None

    if frame is not None and not frame.empty:
        done = frame[frame["Reported EPS"].notna()] if "Reported EPS" in frame.columns else frame
        for stamp, row in done.head(limit).iterrows():
            est, rep = row.get("EPS Estimate"), row.get("Reported EPS")
            surprise = None
            if est not in (None, 0) and est == est and rep == rep:
                surprise = (rep - est) / abs(est) * 100
            rows.append({"date": stamp.date(), "estimate": est, "reported": rep,
                         "surprise_pct": surprise, "filing": None})

    # Match each quarter to the 8-K that announced it, by nearest filing date.
    try:
        filings = ec.earnings_filings(symbol, limit=limit * 2)
    except Exception:
        filings = []
    for r in rows:
        best = min((f for f in filings if f.get("filing_date")),
                   key=lambda f: abs((datetime.date.fromisoformat(f["filing_date"]) - r["date"]).days),
                   default=None)
        if best and abs((datetime.date.fromisoformat(best["filing_date"]) - r["date"]).days) <= 3:
            r["filing"] = {"url": best["url"],
                           "accepted": (best.get("acceptance") or "").replace("T", " ")[:19],
                           "form": best.get("form", "8-K")}
    return rows
