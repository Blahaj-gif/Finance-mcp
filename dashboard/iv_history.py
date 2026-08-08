"""
A local implied-volatility history.

IV rank is defined against an *implied* volatility history: where does today's
IV sit inside the range it has traded over the past year? No free feed provides
that history, so the tool has been substituting realised volatility, which is a
different quantity -- it measures what the stock did, not what options were
priced at.

The fix that does not require a paid feed is to start recording. Every options
call stamps one observation per symbol per day. Once a symbol has enough of
them, the rank becomes a real IV rank; until then it stays the realised-vol
proxy and says so. The store is small, append-only, and self-pruning.
"""
import datetime
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(BASE_DIR, "iv_history.json")

# Below this, a "rank" would be noise dressed up as a statistic.
MIN_OBSERVATIONS = 30
# One year of trading days is the conventional lookback for IV rank.
LOOKBACK_DAYS = 365
# Bound the file: a year of daily observations per symbol is all that is used.
MAX_PER_SYMBOL = 400

_LOCK = threading.Lock()


def _load():
    if not os.path.exists(HISTORY_PATH):
        return {}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return json.loads(content) if content else {}
    except (json.JSONDecodeError, OSError):
        # A corrupt history must never break an options call.
        return {}


def _save(data):
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, HISTORY_PATH)


def record_snapshot(symbol: str, atm_iv: float, spot: float = None, dte: int = None,
                    today: datetime.date = None) -> int:
    """
    Record one ATM IV observation. Idempotent per symbol per day.

    Returns how many observations that symbol now has.
    """
    if atm_iv is None or atm_iv <= 0:
        return observation_count(symbol)

    symbol = symbol.upper()
    day = str(today or datetime.date.today())

    with _LOCK:
        data = _load()
        rows = data.get(symbol, [])

        for row in rows:
            if row.get("date") == day:
                row.update({"atm_iv": float(atm_iv), "spot": spot, "dte": dte})
                break
        else:
            rows.append({"date": day, "atm_iv": float(atm_iv), "spot": spot, "dte": dte})

        rows.sort(key=lambda r: r["date"])
        if len(rows) > MAX_PER_SYMBOL:
            rows = rows[-MAX_PER_SYMBOL:]

        data[symbol] = rows
        _save(data)
        return len(rows)


def observation_count(symbol: str) -> int:
    return len(_load().get(symbol.upper(), []))


def _window(symbol: str, today: datetime.date = None):
    today = today or datetime.date.today()
    cutoff = str(today - datetime.timedelta(days=LOOKBACK_DAYS))
    return [r for r in _load().get(symbol.upper(), [])
            if r.get("date", "") >= cutoff and r.get("atm_iv")]


def iv_rank(symbol: str, current_iv: float, today: datetime.date = None):
    """
    True IV rank and percentile from recorded history, or None if there is not
    yet enough of it.

    rank       — where current IV sits between the 1-year low and high (0-100)
    percentile — share of observations below current IV (0-100)
    """
    rows = _window(symbol, today)
    if len(rows) < MIN_OBSERVATIONS or current_iv is None:
        return None

    values = [r["atm_iv"] for r in rows]
    lo, hi = min(values), max(values)
    rank = ((current_iv - lo) / (hi - lo) * 100) if hi > lo else 50.0
    percentile = sum(1 for v in values if v < current_iv) / len(values) * 100

    return {
        "rank": max(0.0, min(100.0, rank)),
        "percentile": percentile,
        "observations": len(rows),
        "low": lo,
        "high": hi,
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
    }


def coverage() -> dict:
    """How much IV history exists per symbol, and whether it is usable yet."""
    data = _load()
    return {
        sym: {
            "observations": len(rows),
            "usable": len(rows) >= MIN_OBSERVATIONS,
            "needs": max(0, MIN_OBSERVATIONS - len(rows)),
            "from": rows[0]["date"] if rows else None,
            "to": rows[-1]["date"] if rows else None,
        }
        for sym, rows in data.items()
    }
