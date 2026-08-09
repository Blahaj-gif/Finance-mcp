"""
Portfolio value over time — recorded, and reconstructed.

The broker hands back a snapshot: what you hold now, at what cost, worth what
today. It does not hand back a history. So a P&L curve has to come from
somewhere, and there are only two honest places:

**Recorded.** Write down net liquidation once a day and plot what was written.
True by construction, and empty on day one. This module records.

**Reconstructed.** Take today's holdings, pull each symbol's price history, and
mark the current book back through time. Available immediately, and *not* your
P&L history: it assumes today's position was held for the whole window, so any
buy, trim or exit inside that window makes it a curve of something that never
happened. It answers a different, still-useful question -- "how has the book I
hold now performed?" -- and it is labelled as that everywhere it appears.

Both are returned, distinguished, and never silently merged. The recorded series
is the one that becomes trustworthy with time; the reconstruction is what makes
the panel useful before that.
"""
import datetime
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(BASE_DIR, "portfolio_history.json")

# Keep a couple of years. Beyond that the file grows without anyone reading it.
MAX_SNAPSHOTS = 800

_LOCK = threading.Lock()


def _load(path=None) -> list:
    path = path or HISTORY_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        # A corrupt history is not worth taking the dashboard down for; it is a
        # record of past values, not something anything else depends on.
        return []


def _save(rows, path=None):
    path = path or HISTORY_PATH
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows[-MAX_SNAPSHOTS:], fh, indent=1)
    os.replace(tmp, path)          # atomic: a crash mid-write cannot truncate it


def record_snapshot(net_liquidation, gross_exposure=None, unrealised_pnl=None,
                    currency=None, positions=None, path=None, today=None) -> dict:
    """
    Record one snapshot per calendar day, overwriting the day's earlier entry.

    Overwrite rather than append: the dashboard re-renders on every widget
    change, and appending would write dozens of rows for one day and make the
    curve a record of how often someone clicked.
    """
    day = (today or datetime.date.today()).isoformat()
    row = {
        "date": day,
        "net_liquidation": float(net_liquidation),
        "gross_exposure": float(gross_exposure) if gross_exposure is not None else None,
        "unrealised_pnl": float(unrealised_pnl) if unrealised_pnl is not None else None,
        "currency": currency,
        "positions": positions or [],
        "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with _LOCK:
        rows = [r for r in _load(path) if r.get("date") != day]
        rows.append(row)
        rows.sort(key=lambda r: r.get("date", ""))
        _save(rows, path)
    return row


def recorded_series(path=None) -> list:
    """Snapshots oldest-first. Empty until the dashboard has been opened on two days."""
    return [r for r in _load(path) if r.get("date") and r.get("net_liquidation") is not None]


def reconstruct_series(positions, price_history, cash=0.0):
    """
    Mark the CURRENT book back through time.

    positions:     [{"symbol", "quantity", "cost"}]
    price_history: {symbol: [(date, close), ...]} oldest-first
    cash:          added to every point, so the line is comparable to net liq

    Returns (series, coverage) where series is [{"date", "value", "pnl"}] over
    the dates every symbol has a price for, and coverage names any symbol that
    had to be dropped. Intersecting rather than forward-filling: a symbol with a
    gap would otherwise contribute a flat segment that looks like a real day of
    no movement.
    """
    usable = {}
    dropped = []
    for pos in positions:
        sym = pos.get("symbol")
        hist = price_history.get(sym)
        if not hist:
            dropped.append(sym)
            continue
        usable[sym] = {d: c for d, c in hist if c is not None}

    if not usable:
        return [], {"used": [], "dropped": dropped}

    common = set.intersection(*(set(v) for v in usable.values()))
    if not common:
        return [], {"used": [], "dropped": dropped + list(usable)}

    qty = {p["symbol"]: float(p.get("quantity", 0) or 0) for p in positions}
    basis = sum(float(p.get("cost", 0) or 0) * qty.get(p["symbol"], 0.0)
                for p in positions if p.get("symbol") in usable)

    series = []
    for day in sorted(common):
        value = sum(usable[s][day] * qty.get(s, 0.0) for s in usable)
        series.append({
            "date": day,
            "value": value + cash,
            # Against cost basis, not against the first point: a window that
            # opens mid-drawdown would otherwise show the position starting flat.
            "pnl": value - basis,
        })
    return series, {"used": sorted(usable), "dropped": dropped}


def position_contributions(positions):
    """
    Per-position unrealised P&L, largest absolute contribution first.

    A total tells you the book is down; this tells you which name did it, which
    is the question anyone actually asks next.
    """
    out = []
    for pos in positions:
        qty = float(pos.get("quantity", 0) or 0)
        cost = float(pos.get("cost", 0) or 0)
        last = float(pos.get("last", 0) or 0)
        if not qty:
            continue
        out.append({
            "symbol": pos.get("symbol", "?"),
            "pnl": (last - cost) * qty,
            "pnl_pct": ((last - cost) / cost * 100) if cost else 0.0,
            "value": last * qty,
        })
    return sorted(out, key=lambda r: abs(r["pnl"]), reverse=True)
