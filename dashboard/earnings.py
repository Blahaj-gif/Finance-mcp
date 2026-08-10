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


# =====================================================================
# QUARTERLY FUNDAMENTALS, FROM THE FILING
# =====================================================================
# EPS is a consensus-vs-actual comparison. Revenue, cash flow and capex are not:
# no free source publishes consensus for them, so these tables show the *filed*
# figure and its change, never a "surprise". That is a different question from
# the EPS table and is labelled as one rather than sharing its columns.
#
# The numbers come from SEC XBRL -- the company's own tagged filing -- not from
# a third-party aggregator, so they are authoritative and stamped with the form
# that carried them.

METRICS = {
    "EPS":     {"label": "Diluted EPS", "unit": "per share", "kind": "eps"},
    "Revenue": {"label": "Revenue", "unit": "USD", "kind": "xbrl",
                "tags": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                         "Revenues", "SalesRevenueNet"]},
    "FCF":     {"label": "Free cash flow", "unit": "USD", "kind": "derived",
                "note": "Operating cash flow minus capital expenditure — derived "
                        "here, not a tagged figure in the filing."},
    "Capex":   {"label": "Capital expenditure", "unit": "USD", "kind": "xbrl",
                "tags": ["PaymentsToAcquirePropertyPlantAndEquipment"]},
    "Opex":    {"label": "Operating expenses", "unit": "USD", "kind": "xbrl",
                "tags": ["OperatingExpenses"]},
}

_OCF_TAGS = ["NetCashProvidedByUsedInOperatingActivities",
             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
_CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment"]

# A quarterly duration. XBRL mixes quarterly, half-year, nine-month and annual
# facts under the same tag; taking them all would put a full year next to a
# quarter in the same column and call both "Q".
_QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS = 80, 100


def _duration_facts(cik: str, tags, unit_hint="USD") -> list:
    """Every duration fact for the first tag that yields any, newest filing wins."""
    for tag in tags:
        try:
            data = ec.xbrl_concept(cik, "us-gaap", tag)
        except Exception:
            continue
        best = {}
        for unit, points in ((data or {}).get("units") or {}).items():
            if unit_hint not in unit:
                continue
            for p in points:
                start, end, val = p.get("start"), p.get("end"), p.get("val")
                if not (start and end) or val is None:
                    continue
                try:
                    d0 = datetime.date.fromisoformat(start)
                    d1 = datetime.date.fromisoformat(end)
                except ValueError:
                    continue
                key = (start, end)
                filed = p.get("filed", "")
                # A figure can be restated; the most recently filed version of a
                # period is the one the company stands behind now.
                if key in best and filed <= best[key]["filed"]:
                    continue
                best[key] = {"start": d0, "end": d1, "days": (d1 - d0).days,
                             "value": float(val), "form": p.get("form", ""),
                             "filed": filed, "fp": p.get("fp", "")}
        if best:
            return sorted(best.values(), key=lambda f: f["end"])
    return []


def _quarterly_points(cik: str, tags, unit_hint="USD") -> dict:
    """
    {period_end: fact} for single quarters.

    Cash-flow tags are filed year-to-date, not per quarter: Q1 is a 3-month
    fact, Q2 a 6-month, Q3 a 9-month, Q4 the full year. Filtering to
    quarter-length durations therefore returned exactly one row per year -- Q1 --
    with the "previous quarter" a year back, so a QoQ change was really a YoY
    one wearing the wrong label.

    So: use a genuine 3-month fact where the company files one, and otherwise
    difference consecutive year-to-date facts that share a fiscal-year start.
    That subtraction is only valid within one fiscal year, which is why the
    grouping is by start date rather than by proximity.
    """
    facts = _duration_facts(cik, tags, unit_hint)
    out = {}

    for f in facts:
        if _QUARTER_MIN_DAYS <= f["days"] <= _QUARTER_MAX_DAYS:
            out[f["end"].isoformat()] = f

    by_start = {}
    for f in facts:
        by_start.setdefault(f["start"], []).append(f)
    for start, group in by_start.items():
        group.sort(key=lambda f: f["end"])
        for prev, cur in zip(group, group[1:]):
            end = cur["end"].isoformat()
            if end in out:
                continue          # a directly filed quarter beats a derived one
            span = (cur["end"] - prev["end"]).days
            if not (_QUARTER_MIN_DAYS <= span <= _QUARTER_MAX_DAYS):
                continue
            out[end] = {"start": prev["end"], "end": cur["end"], "days": span,
                        "value": cur["value"] - prev["value"],
                        "form": cur["form"], "filed": cur["filed"],
                        "fp": cur["fp"], "derived": True}
    return out


def quarterly_metric(symbol: str, metric: str, limit: int = 4) -> dict:
    """
    The last `limit` quarters of one metric, newest first.

    Returns {"metric", "label", "unit", "rows": [...], "source", "note"}.
    Each row carries value, the change on the prior quarter and on the year-ago
    quarter, and the form the figure was filed on.
    """
    spec = METRICS.get(metric)
    if spec is None:
        raise ValueError(f"Unknown metric {metric!r}. Available: {', '.join(METRICS)}")

    info = ec.ticker_to_cik(symbol)
    cik = info["cik"]

    if spec["kind"] == "derived":                     # FCF
        ocf = _quarterly_points(cik, _OCF_TAGS)
        capex = _quarterly_points(cik, _CAPEX_TAGS)
        points = {end: {"value": v["value"] - capex[end]["value"],
                        "form": v["form"], "filed": v["filed"], "fp": v["fp"]}
                  for end, v in ocf.items() if end in capex}
    else:
        points = _quarterly_points(cik, spec["tags"])

    ends = sorted(points, reverse=True)
    rows = []
    for end in ends[:limit]:
        p = points[end]
        prior = next((e for e in ends if e < end), None)
        year_ago = next((e for e in ends
                         if abs((datetime.date.fromisoformat(end)
                                 - datetime.date.fromisoformat(e)).days - 365) <= 20), None)
        def pct(other):
            if other is None or not points[other]["value"]:
                return None
            base = points[other]["value"]
            return (p["value"] - base) / abs(base) * 100
        rows.append({"period_end": end, "value": p["value"], "form": p["form"],
                     "filed": p["filed"], "fiscal_period": p["fp"],
                     "qoq_pct": pct(prior), "yoy_pct": pct(year_ago)})

    return {"metric": metric, "label": spec["label"], "unit": spec["unit"],
            "rows": rows, "source": "SEC XBRL (as filed)",
            "note": spec.get("note", "")}
