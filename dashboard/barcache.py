"""
A small on-disk cache for validated bar frames.

The in-memory cache only helps a process that is already warm, and this project
runs two of them: the MCP server that Claude talks to, and the Streamlit
dashboard. They ask for the same bars constantly and each paid full price --
every dashboard rerun re-fetched what the MCP server had just validated, and
vice versa. Sharing the frames across processes turns those into file reads.

Only frames that have passed _validate_frame are written, so a cache hit is not
a way to skip validation -- it is a way to skip re-downloading something that
already passed it. Entries carry the source label and the moment of capture, and
a reader re-checks the age itself: a cached frame that has aged past its TTL is
discarded rather than served, and the staleness gate still runs on what comes
back.

Parquet where pyarrow is available, CSV otherwise. Both round-trip the six
columns this project uses; neither is asked to preserve dtypes beyond that,
because _validate_frame re-derives them on read.
"""
import hashlib
import json
import os
import time

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.getenv("FINMCP_BAR_CACHE_DIR", os.path.join(BASE_DIR, "conf", "barcache"))

# Longer than the 60s in-memory TTL: the point of the disk layer is to survive a
# process restart and a dashboard rerun, not to be the freshest copy. The
# staleness gate in _validate_frame is what guarantees the bars are current;
# this only decides how long we may avoid re-downloading them.
DISK_TTL_SECONDS = float(os.getenv("FINMCP_BAR_CACHE_TTL", "300"))
MAX_ENTRIES = int(os.getenv("FINMCP_BAR_CACHE_MAX", "400"))

_ENABLED = os.getenv("FINMCP_BAR_CACHE", "1").strip().lower() not in ("0", "false", "no")

try:
    import pyarrow  # noqa: F401
    _FORMAT = "parquet"
except Exception:
    _FORMAT = "csv"


def _key(symbol: str, interval: str, count: int) -> str:
    raw = f"{symbol.upper()}|{interval.upper()}|{int(count)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _paths(symbol, interval, count):
    stem = os.path.join(CACHE_DIR, _key(symbol, interval, count))
    return stem + "." + _FORMAT, stem + ".json"


def enabled() -> bool:
    return _ENABLED


def load(symbol: str, interval: str, count: int, ttl: float = None):
    """Return (frame, source, age_seconds) or None. Never raises."""
    if not _ENABLED:
        return None
    ttl = DISK_TTL_SECONDS if ttl is None else ttl
    data_path, meta_path = _paths(symbol, interval, count)
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        age = time.time() - float(meta["captured_at"])
        if age >= ttl:
            return None
        frame = (pd.read_parquet(data_path) if _FORMAT == "parquet"
                 else pd.read_csv(data_path))
        if frame.empty:
            return None
        # `time` must come back as the string form the rest of the code expects;
        # parquet preserves it, but CSV would hand back whatever it inferred.
        frame["time"] = frame["time"].astype(str)
        return frame, str(meta.get("source", "cache")), age
    except Exception:
        # A cache is an optimisation. A corrupt or half-written entry means
        # fetch again, never fail the request.
        return None


def store(symbol: str, interval: str, count: int, frame, source: str) -> bool:
    """Write a validated frame. Returns whether it was stored. Never raises."""
    if not _ENABLED or frame is None or frame.empty:
        return False
    data_path, meta_path = _paths(symbol, interval, count)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = data_path + ".tmp"
        if _FORMAT == "parquet":
            frame.to_parquet(tmp, index=False)
        else:
            frame.to_csv(tmp, index=False)
        os.replace(tmp, data_path)

        tmp_meta = meta_path + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as fh:
            json.dump({"symbol": symbol.upper(), "interval": interval.upper(),
                       "count": int(count), "source": source,
                       "captured_at": time.time(), "rows": int(len(frame))}, fh)
        os.replace(tmp_meta, meta_path)
        _prune()
        return True
    except Exception:
        return False


def _prune():
    """Keep the directory bounded; oldest entries go first."""
    try:
        metas = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)
                 if f.endswith(".json")]
        if len(metas) <= MAX_ENTRIES:
            return
        metas.sort(key=lambda p: os.path.getmtime(p))
        for meta_path in metas[:len(metas) - MAX_ENTRIES]:
            stem = meta_path[:-len(".json")]
            for path in (meta_path, stem + "." + _FORMAT):
                try:
                    os.remove(path)
                except OSError:
                    pass
    except Exception:
        pass


def clear():
    """Drop every entry. Used by tests and when a feed is known to be wrong."""
    try:
        for name in os.listdir(CACHE_DIR):
            try:
                os.remove(os.path.join(CACHE_DIR, name))
            except OSError:
                pass
    except Exception:
        pass


def stats() -> dict:
    """What is on disk right now, for get_data_sources."""
    try:
        names = [n for n in os.listdir(CACHE_DIR) if n.endswith(".json")]
        return {"enabled": _ENABLED, "format": _FORMAT, "entries": len(names),
                "dir": CACHE_DIR, "ttl_seconds": DISK_TTL_SECONDS}
    except Exception:
        return {"enabled": _ENABLED, "format": _FORMAT, "entries": 0,
                "dir": CACHE_DIR, "ttl_seconds": DISK_TTL_SECONDS}
