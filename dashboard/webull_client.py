import os
import sys
import threading
import datetime
import pandas as pd
import numpy as np

try:
    from dashboard import market_calendar
    from dashboard import barcache
except ImportError:  # when imported as a top-level module from dashboard/
    import market_calendar
    import barcache

# .env lives in one place now (dashboard/envfile.py) because three modules read
# environment variables at import time and only this one used to load the file --
# so econ_calendar and central_banks were silently empty unless this module had
# been imported first. The name is kept: tests and callers use it.
try:
    from dashboard.envfile import load_env
except ImportError:  # imported as a top-level module from dashboard/
    from envfile import load_env

load_env()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WEBULL_APP_KEY = os.getenv("WEBULL_APP_KEY")
WEBULL_APP_SECRET = os.getenv("WEBULL_APP_SECRET")
# Optional, and only used when WEBULL_ENVIRONMENT=paper. Webull's sandbox is a
# separate deployment with its own app registry -- a production key returns 401
# there, not a permissions error.
WEBULL_PAPER_APP_KEY = os.getenv("WEBULL_PAPER_APP_KEY", "").strip()
WEBULL_PAPER_APP_SECRET = os.getenv("WEBULL_PAPER_APP_SECRET", "").strip()
WEBULL_REGION_ID = os.getenv("WEBULL_REGION_ID", "th")
WEBULL_ENVIRONMENT = os.getenv("WEBULL_ENVIRONMENT", "prod")
WEBULL_TOKEN_DIR = os.getenv("WEBULL_TOKEN_DIR", os.path.join(BASE_DIR, "conf"))


# ---------------------------------------------------------------------
# Paper trading (sandbox)
# ---------------------------------------------------------------------
# Webull's OpenAPI has a simulated environment, but the Python SDK ships only
# production hosts (webull/core/data/endpoints.json), so the sandbox has to be
# injected per region. This map is taken from Webull's own MCP server,
# webull-inc/webull-openapi-mcp (webull_openapi_mcp/sdk_client.py, UAT_ENDPOINTS)
# -- the same values it feeds to the same SDK call. Some regions have migrated
# to *.sandbox.webull.* and some have not; both forms are current upstream.
#
# This code previously tried to `import webull_openapi_mcp`, a package this
# project does not depend on and does not ship. Setting WEBULL_ENVIRONMENT=uat
# therefore raised ModuleNotFoundError at client construction -- a documented
# setting that broke the app rather than switching it to paper.
SANDBOX_ENDPOINTS = {
    "us": {"api": "api.sandbox.webull.com",
           "quotes-api": "api.sandbox.webull.com",
           "events-api": "events-api.sandbox.webull.com"},
    "hk": {"api": "api.sandbox.webull.hk",
           "quotes-api": "data-api.sandbox.webull.hk",
           "events-api": "events-api.sandbox.webull.hk"},
    "jp": {"api": "jp-openapi-alb.uat.webullbroker.com",
           "quotes-api": "data-api.uat.webullbroker.com",
           "events-api": "jp-openapi-events.uat.webullbroker.com"},
    "sg": {"api": "sg-api.uat.webullbroker.com",
           "quotes-api": "data-api.uat.webullbroker.com",
           "events-api": "sg-events-api.uat.webullbroker.com"},
    "th": {"api": "th-api.uat.webullbroker.com",
           "quotes-api": "data-api.uat.webullbroker.com",
           "events-api": "th-events-api.uat.webullbroker.com"},
    "my": {"api": "my-api.uat.webullbroker.com",
           "quotes-api": "data-api.uat.webullbroker.com",
           "events-api": "my-events-api.uat.webullbroker.com"},
    "uk": {"api": "uk-api.uat.webullbroker.com",
           "quotes-api": "data-api.uat.webullbroker.com",
           "events-api": "uk-events-api.uat.webullbroker.com"},
    "mx": {"api": "us-openapi-alb.uat.webullbroker.com",
           "quotes-api": "us-openapi-quotes-api.uat.webullbroker.com",
           "events-api": "us-openapi-events.uat.webullbroker.com"},
    "br": {"api": "us-openapi-alb.uat.webullbroker.com",
           "quotes-api": "us-openapi-quotes-api.uat.webullbroker.com",
           "events-api": "us-openapi-events.uat.webullbroker.com"},
    "eu": {"api": "eu-api.uat.webullbroker.com",
           "quotes-api": "eu-api.uat.webullbroker.com",
           "events-api": "eu-events-api.uat.webullbroker.com"},
    "za": {"api": "au-api.uat.webullbroker.com",
           "quotes-api": "au-api.uat.webullbroker.com",
           "events-api": "au-events-api.uat.webullbroker.com"},
    "au": {"api": "au-api.uat.webullbroker.com",
           "quotes-api": "au-api.uat.webullbroker.com",
           "events-api": "au-events-api.uat.webullbroker.com"},
}

PAPER_ALIASES = {"uat", "sandbox", "paper", "simulated"}


def is_paper_environment() -> bool:
    """True when this process is pointed at Webull's simulated environment."""
    return WEBULL_ENVIRONMENT.strip().lower() in PAPER_ALIASES


def resolved_credentials():
    """
    The (key, secret) pair this process would actually authenticate with.

    The sandbox is a separate Webull deployment with its own registry, so paper
    takes its own pair when one is set and falls back to the live pair when it
    is not. That rule lived only inside `get_api_client`, which was fine while
    nothing else needed to know — and stopped being fine the moment tool
    registration started asking whether credentials exist at all. A gate that
    checked only the live pair would have hidden all eight account tools from
    someone running paper with paper keys, which is a working setup.

    One rule, one place. Either half may be empty; the caller decides what that
    means.
    """
    if is_paper_environment():
        return (WEBULL_PAPER_APP_KEY or WEBULL_APP_KEY,
                WEBULL_PAPER_APP_SECRET or WEBULL_APP_SECRET)
    return WEBULL_APP_KEY, WEBULL_APP_SECRET


def have_credentials() -> bool:
    """Whether there is anything to authenticate with. Offline; no network call."""
    key, secret = resolved_credentials()
    return bool(key and secret)


def environment_label() -> str:
    """A word for the surface being traded, for anywhere a human can see it."""
    return "PAPER" if is_paper_environment() else "LIVE"


# =====================================================================
# DATA INTEGRITY ERRORS
# =====================================================================
# These deliberately propagate out of the MCP tools as real errors instead of
# being flattened into a returned string. Silently handing back untrustworthy
# market data is worse than a hard failure, because it reads as authoritative.

class DataIntegrityError(RuntimeError):
    """Market data failed a structural sanity check (ordering, NaN, OHLC bounds)."""


class StaleDataError(DataIntegrityError):
    """The newest bar is older than the tolerance for its interval."""


# Mapping interval between Webull and Yahoo Finance
INTERVAL_WEBULL_TO_YF = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "M60": "1h",
    "M120": "1h",      # Yahoo has no 2h bar; the caller resamples
    "M240": "1h",      # likewise 4h
    "D": "1d",
    "W": "1wk",
    "M": "1mo",
    "Y": "3mo",        # Yahoo's coarsest is quarterly
}

INTERVAL_YF_TO_WEBULL = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
                         "1h": "H1", "1d": "D", "1wk": "W", "1mo": "M"}

# Webull's own timespan vocabulary, from the API's error message:
#   [M1, M5, M15, M30, M60, M120, M240, D, W, M, Y]
#
# Note there is no "H1". Our canonical name for an hourly bar has always been
# H1, and it was passed through to the API unchanged -- so every hourly request
# returned HTTP 417 UNSUPPORTED_TIMESPAN and fell back to Yahoo. The fallback
# announced itself, but nothing said the timeframe could *never* succeed, so it
# read as a flaky broker rather than a name the broker does not have. Both the
# dashboard's 1H and 4H views and get_multi_timeframe's hourly leg were served
# by Yahoo while the daily leg came from Webull -- two feeds, two adjustment
# conventions, one confluence score.
WEBULL_TIMESPAN = {
    "M1": "M1", "M5": "M5", "M15": "M15", "M30": "M30",
    "H1": "M60", "M60": "M60", "M120": "M120", "M240": "M240",
    "D": "D", "W": "W", "M": "M", "Y": "Y",
}

# Staleness thresholds.
#
# Daily and slower intervals are measured in *trading sessions* using the
# exchange calendar (see market_calendar.py), which is far tighter than a
# calendar-day heuristic: a 5-day window had to be wide enough to absorb a
# holiday weekend, and was therefore wide enough to hide a real 3-day outage.
STALENESS_TOLERANCE_SESSIONS = {
    "D": 1,    # newest completed session; today's bar does not exist until close
    "W": 6,    # a little over one week
    "M": 25,   # a little over one month
}

# Intraday still uses wall-clock hours -- session-counting says nothing useful
# about a 15-minute bar. Kept loose enough to absorb a long holiday weekend.
STALENESS_TOLERANCE_HOURS = {
    "M1": 96,
    "M5": 96,
    "M15": 96,
    "M30": 96,
    "H1": 96,
}
DEFAULT_STALENESS_TOLERANCE_HOURS = 120


def _parse_bar_times(raw: pd.Series) -> pd.Series:
    """
    Parse a bar-time column into tz-naive datetimes.

    Handles ISO-8601 strings (what the Webull OpenAPI returns, e.g.
    "2026-08-06T04:00:00.000+0000") and epoch milliseconds. Raises rather than
    returning partial results: unparseable timestamps mean unorderable bars,
    and unorderable bars are exactly how stale prices got reported as current.
    """
    if pd.api.types.is_numeric_dtype(raw):
        # Epoch seconds vs milliseconds: anything past ~1e11 is milliseconds.
        unit = "ms" if float(pd.to_numeric(raw, errors="coerce").max() or 0) > 1e11 else "s"
        parsed = pd.to_datetime(raw, unit=unit, errors="coerce", utc=True)
    else:
        parsed = pd.to_datetime(raw, errors="coerce", utc=True)

    if parsed.isna().any():
        bad = raw[parsed.isna()].head(3).tolist()
        raise DataIntegrityError(
            f"Could not parse {int(parsed.isna().sum())} bar timestamp(s); samples: {bad}"
        )

    # Drop tz so Webull (UTC) and Yahoo (exchange-local) frames share one dtype.
    return parsed.dt.tz_localize(None)


def _validate_frame(df: pd.DataFrame, symbol: str, interval: str, source: str) -> pd.DataFrame:
    """
    Structural gate that every price frame passes through, whatever its source.

    Webull and Yahoo are treated as interchangeable by every caller, but they do
    not agree on row order or session coverage. This is the one place that
    guarantees the contract callers actually rely on: oldest-first, newest last,
    fresh, and numerically sane.
    """
    required = ["time", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise DataIntegrityError(f"{symbol}: missing column(s) {missing} from {source}")

    if df.empty:
        raise DataIntegrityError(f"{symbol}: {source} returned zero bars")

    times = _parse_bar_times(df["time"])

    # ---- Ordering invariant --------------------------------------------
    # The bug this whole module exists to prevent: Webull returns bars
    # newest-first, every consumer reads .iloc[-1] as "latest".
    if not times.is_monotonic_increasing:
        raise DataIntegrityError(
            f"{symbol}: bars from {source} are not in ascending time order "
            f"(first={times.iloc[0]}, last={times.iloc[-1]}). "
            "Refusing to serve -- .iloc[-1] would not be the most recent bar."
        )

    # ---- Staleness ------------------------------------------------------
    newest = times.iloc[-1]
    iv = interval.upper()

    if iv in STALENESS_TOLERANCE_SESSIONS:
        # Session-based: exact, and immune to weekends and holidays.
        max_sessions = STALENESS_TOLERANCE_SESSIONS[iv]
        stale_by = market_calendar.sessions_stale(newest.date())
        if stale_by > max_sessions:
            raise StaleDataError(
                f"{symbol}: newest {interval} bar is {newest:%Y-%m-%d} — "
                f"{stale_by} trading session(s) behind, exceeding the "
                f"{max_sessions}-session tolerance. Source: {source}. "
                "Refusing to return stale prices."
            )
    else:
        tolerance_h = STALENESS_TOLERANCE_HOURS.get(iv, DEFAULT_STALENESS_TOLERANCE_HOURS)
        age_h = (datetime.datetime.utcnow() - newest.to_pydatetime()).total_seconds() / 3600.0
        if age_h > tolerance_h:
            raise StaleDataError(
                f"{symbol}: newest {interval} bar is {newest:%Y-%m-%d %H:%M} "
                f"({age_h / 24:.1f} days old), exceeding the {tolerance_h / 24:.1f}-day "
                f"tolerance for this interval. Source: {source}. "
                "Refusing to return stale prices."
            )

    # ---- Numeric sanity on the bar callers will quote -------------------
    latest = df.iloc[-1]
    for col in ("open", "high", "low", "close"):
        if pd.isna(latest[col]):
            raise DataIntegrityError(f"{symbol}: newest bar has NaN '{col}' from {source}")
    if not (float(latest["low"]) <= float(latest["close"]) <= float(latest["high"])):
        raise DataIntegrityError(
            f"{symbol}: newest bar violates low<=close<=high "
            f"(l={latest['low']}, c={latest['close']}, h={latest['high']}) from {source}"
        )

    out = df.copy()
    out["time"] = times.dt.strftime("%Y-%m-%d %H:%M:%S")
    return out[required].reset_index(drop=True)


# Calendar days of history needed per bar, per interval, plus Yahoo's hard caps.
# (Trading days are ~5/7 of calendar days, hence the 1.45x padding on intraday
# and daily; weekly/monthly are padded generously since they are cheap.)
_YF_PERIOD_RULES = {
    "1m":  (1 / (60 * 6.5), "7d", 7),
    "5m":  (5 / (60 * 6.5), "60d", 59),
    "15m": (15 / (60 * 6.5), "60d", 59),
    "30m": (30 / (60 * 6.5), "60d", 59),
    "1h":  (1 / 6.5, "730d", 729),
    "1d":  (1.45, "max", None),
    "1wk": (7.4, "max", None),
    "1mo": (31.5, "max", None),
}


def _yf_period_for(yf_interval: str, count: int) -> str:
    """Smallest Yahoo `period` that still covers `count` bars of `yf_interval`."""
    per_bar, max_period, cap_days = _YF_PERIOD_RULES.get(yf_interval, (1.45, "max", None))
    # +5 bars of headroom so indicator warm-up never lands one bar short.
    days = int((count + 5) * per_bar) + 5
    if cap_days is not None:
        return f"{min(days, cap_days)}d"
    if days > 3650:
        return max_period
    return f"{days}d"


class YahooThrottledError(RuntimeError):
    """Yahoo returned nothing because we are being rate-limited, not because the symbol is unknown."""


class SymbolNotFoundError(ValueError):
    """Yahoo has no data for this symbol -- delisted, mistyped, or never listed."""


# The canary is a symbol Yahoo will always have. If it answers, an empty result
# for the requested symbol is about the symbol; if it is also empty, we are
# throttled. Cached briefly so a watchlist sweep of dead tickers does not fire
# one canary per name.
_CANARY_SYMBOL = os.getenv("YF_CANARY_SYMBOL", "SPY")
_CANARY_TTL_SECONDS = 30.0
_canary_state = {"checked_at": 0.0, "alive": None}


def _canary_is_alive() -> bool:
    """Can Yahoo serve anything at all right now?"""
    import time
    now = time.monotonic()
    if _canary_state["alive"] is not None and now - _canary_state["checked_at"] < _CANARY_TTL_SECONDS:
        return _canary_state["alive"]
    try:
        probe = yahoo_ticker(_CANARY_SYMBOL).history(period="5d", interval="1d")
        alive = not probe.empty
    except Exception:
        alive = False
    _canary_state.update(checked_at=now, alive=alive)
    return alive


def _explain_empty_yahoo_result(symbol: str) -> Exception:
    """Decide whether an empty Yahoo response means 'throttled' or 'no such symbol'."""
    if _canary_is_alive():
        return SymbolNotFoundError(
            f"Yahoo Finance has no data for '{symbol}'. The feed is answering "
            f"normally ({_CANARY_SYMBOL} resolved), so this is the symbol, not the "
            "connection -- check the ticker, or the listing may have been delisted."
        )
    return YahooThrottledError(
        f"Yahoo Finance returned no data for '{symbol}', and the {_CANARY_SYMBOL} "
        "canary is also empty -- the feed is rate-limiting this address, not "
        "missing the symbol. Retry shortly, or raise YF_MIN_REQUEST_INTERVAL."
    )


def yahoo_feed_delay(symbol: str) -> dict:
    """
    How far behind Yahoo's own clock is, measured rather than assumed.

    Yahoo does not publish `exchangeDataDelayedBy` on the chart endpoint, but it
    does return `regularMarketTime` -- the timestamp of the last regular-session
    print it holds. Comparing that to now gives the real observed lag for this
    symbol on this exchange, which is what matters when a fallback quote is
    about to be used for a decision.

    Returns {} when the metadata is unavailable rather than guessing a number.
    """
    try:
        md = yahoo_ticker(symbol.upper()).history_metadata or {}
        market_time = md.get("regularMarketTime")
        if not market_time:
            return {}
        last = datetime.datetime.utcfromtimestamp(int(market_time))
        lag = (datetime.datetime.utcnow() - last).total_seconds()
        return {
            "last_print_utc": last.strftime("%Y-%m-%d %H:%M:%S"),
            "observed_lag_seconds": round(lag, 1),
            "observed_lag_minutes": round(lag / 60.0, 1),
            "exchange": md.get("fullExchangeName") or md.get("exchangeName"),
            "exchange_timezone": md.get("exchangeTimezoneName"),
            "currency": md.get("currency"),
            # Outside trading hours the lag is dominated by the session gap, not
            # by feed delay, so say which reading this is.
            "market_open": lag < 900,
        }
    except Exception:
        return {}


def get_yfinance_data(symbol: str, interval: str = "D", count: int = 200) -> pd.DataFrame:
    """Fetch historical data from Yahoo Finance as a robust fallback."""
    # Map Webull interval to Yahoo Finance interval.
    #
    # This used to default to "1d" for anything unrecognised, so a typo'd or
    # unsupported interval came back as daily bars wearing the requested
    # interval's name -- indicators computed on the wrong timeframe entirely,
    # with nothing in the output to show it.
    yf_interval = INTERVAL_WEBULL_TO_YF.get(interval)
    if yf_interval is None:
        raise ValueError(
            f"No Yahoo interval for '{interval}'. "
            f"Known: {', '.join(sorted(INTERVAL_WEBULL_TO_YF))}.")

    # Size the download to what was actually asked for. A fixed period="1y"
    # pulled ~250 daily bars to answer a 26-bar heatmap request, and weekly and
    # monthly asked for "max" -- full listing history -- every time.
    period = _yf_period_for(yf_interval, count)

    ticker_symbol = symbol.upper()
    ticker = yahoo_ticker(ticker_symbol)
    df = ticker.history(period=period, interval=yf_interval)

    resolved_symbol = ticker_symbol
    if df.empty and not ticker_symbol.endswith(".BK") and WEBULL_REGION_ID.lower() == "th":
        # Thai listings need a .BK suffix on Yahoo. Only attempt this when the
        # account is actually a Thai one -- otherwise a mistyped US ticker can
        # silently resolve to an unrelated Bangkok listing.
        ticker_symbol_bk = f"{ticker_symbol}.BK"
        ticker = yahoo_ticker(ticker_symbol_bk)
        df = ticker.history(period=period, interval=yf_interval)
        if not df.empty:
            resolved_symbol = ticker_symbol_bk

    if df.empty:
        # An empty frame has two very different causes and Yahoo does not say
        # which: the symbol does not exist, or we are being throttled and it
        # answered 200 with nothing in it. Reporting the second as the first
        # sends the caller off to check a ticker that was never the problem.
        raise _explain_empty_yahoo_result(symbol)

    # Standardize columns to lowercase: datetime, open, high, low, close, volume
    # yfinance returns oldest-first, so .tail() keeps the most recent bars.
    df = df.tail(count).reset_index()
    df.columns = [col.lower() for col in df.columns]

    if "date" in df.columns:
        df = df.rename(columns={"date": "time"})
    elif "datetime" in df.columns:
        df = df.rename(columns={"datetime": "time"})

    out = df[["time", "open", "high", "low", "close", "volume"]].copy()
    # Surfaced by fetch_data so a ticker substitution is never silent.
    out.attrs["resolved_symbol"] = resolved_symbol
    return out


# ---------------------------------------------------------------------
# Webull SDK client (built once, not per request)
# ---------------------------------------------------------------------
# Previously an ApiClient was constructed and its loggers re-registered on every
# single call. That re-added handlers each time, which is why conf/webull_sdk.log
# reached 10.7 MB with lines duplicated ~20x -- and at the SDK's default DEBUG
# level those lines contained the app key, request signatures and full response
# bodies. One client, one handler registration, WARNING level.
# RLock, not Lock: get_data_client() acquires this and then calls
# get_api_client(), which acquires it again on the same thread.
_CLIENT_LOCK = threading.RLock()
_API_CLIENT = None
_DATA_CLIENT = None

# ---------------------------------------------------------------------
# Request pacing
# ---------------------------------------------------------------------
# Tools that sweep a list -- get_sector_heatmap (11 ETFs), scan_watchlist,
# get_multi_timeframe (3 intervals) -- fire their calls back to back and trip
# Webull's rate limit. A 429 is not harmless here: it drops the request to the
# Yahoo fallback, so the primary feed silently stops being the source of truth.
# Pacing plus a bounded retry keeps requests on Webull instead.
WEBULL_MIN_REQUEST_INTERVAL = float(os.getenv("WEBULL_MIN_REQUEST_INTERVAL", "0.25"))  # seconds
WEBULL_MAX_RETRIES = int(os.getenv("WEBULL_MAX_RETRIES", "3"))
WEBULL_RETRY_BACKOFF = float(os.getenv("WEBULL_RETRY_BACKOFF", "0.75"))  # seconds, doubled each retry


class _RateLimiter:
    """Serialises calls so that consecutive requests are at least `min_interval` apart."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self):
        import time
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self.min_interval

    def penalise(self, seconds: float):
        """Push the next allowed slot out after a rate-limit rejection."""
        import time
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


_RATE_LIMITER = _RateLimiter(WEBULL_MIN_REQUEST_INTERVAL)


def _is_rate_limited(exc) -> bool:
    if getattr(exc, "http_status", None) == 429:
        return True
    if str(getattr(exc, "error_code", "")).upper() == "TOO_MANY_REQUESTS":
        return True
    return "TOO_MANY_REQUESTS" in str(exc).upper()


def call_webull(fn, *args, **kwargs):
    """
    Invoke a Webull SDK call under the shared pacing budget, retrying on 429.

    Raises the final exception if every attempt is rate-limited, so the caller's
    fallback still works -- this reduces fallbacks, it does not hide failures.
    """
    import time

    last_exc = None
    for attempt in range(WEBULL_MAX_RETRIES):
        _RATE_LIMITER.acquire()
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not _is_rate_limited(e):
                raise
            last_exc = e
            backoff = WEBULL_RETRY_BACKOFF * (2 ** attempt)
            _RATE_LIMITER.penalise(backoff)
            print(f"Webull rate limit hit; retrying in {backoff:.2f}s "
                  f"(attempt {attempt + 1}/{WEBULL_MAX_RETRIES})", file=sys.stderr)
            time.sleep(backoff)
    raise last_exc


# ---------------------------------------------------------------------
# Yahoo pacing
# ---------------------------------------------------------------------
# Yahoo was the one feed with no pacing at all, which made it the weakest link
# rather than the safety net: it is the fallback for every Webull failure *and*
# the primary source for ten fundamentals tools, so a profile sweep could fire
# a dozen unpaced requests. Yahoo answers a burst with HTTP 429, and yfinance
# surfaces that as YFRateLimitError -- or, worse, as an empty frame, which the
# validator then reports as "no data" rather than "throttled".
#
# Deliberately slower than the Webull budget (0.35s vs 0.25s): Yahoo's limit is
# undocumented and IP-scoped, so there is no quota to reason about, only
# observed behaviour.
YF_MIN_REQUEST_INTERVAL = float(os.getenv("YF_MIN_REQUEST_INTERVAL", "0.35"))  # seconds
YF_MAX_RETRIES = int(os.getenv("YF_MAX_RETRIES", "3"))
YF_RETRY_BACKOFF = float(os.getenv("YF_RETRY_BACKOFF", "1.5"))  # seconds, doubled each retry

_YF_LIMITER = _RateLimiter(YF_MIN_REQUEST_INTERVAL)


def _is_yahoo_rate_limited(exc) -> bool:
    try:
        from yfinance.exceptions import YFRateLimitError
        if isinstance(exc, YFRateLimitError):
            return True
    except ImportError:
        pass
    text = str(exc).upper()
    return "TOO MANY REQUESTS" in text or "RATE LIMIT" in text or "429" in text


def call_yahoo(fn, *args, **kwargs):
    """
    Invoke a yfinance call under the shared Yahoo pacing budget, retrying on 429.

    Mirrors `call_webull`. Re-raises the final exception once retries are spent
    so the caller still fails loudly -- this reduces throttling, it does not
    convert a throttled request into a silent empty result.
    """
    import time

    last_exc = None
    for attempt in range(YF_MAX_RETRIES):
        _YF_LIMITER.acquire()
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not _is_yahoo_rate_limited(e):
                raise
            last_exc = e
            backoff = YF_RETRY_BACKOFF * (2 ** attempt)
            _YF_LIMITER.penalise(backoff)
            print(f"Yahoo rate limit hit; retrying in {backoff:.2f}s "
                  f"(attempt {attempt + 1}/{YF_MAX_RETRIES})", file=sys.stderr)
            time.sleep(backoff)
    raise last_exc


class _PacedTicker:
    """
    A `yfinance.Ticker` whose network access goes through `call_yahoo`.

    yfinance does its I/O behind lazy properties -- `.info`, `.options`, `.news`
    all fetch on first access -- so pacing the constructor would pace nothing.
    Properties are paced when read; methods (`.history`, `.option_chain`) are
    paced when called, not when looked up, so a bound-method reference costs
    nothing.
    """

    __slots__ = ("_ticker",)

    def __init__(self, ticker):
        object.__setattr__(self, "_ticker", ticker)

    def __getattr__(self, name):
        import functools
        target = self._ticker
        class_attr = getattr(type(target), name, None)
        if callable(class_attr) and not isinstance(class_attr, property):
            return functools.partial(call_yahoo, getattr(target, name))
        return call_yahoo(getattr, target, name)

    def __repr__(self):
        return f"<PacedTicker {getattr(self._ticker, 'ticker', '?')}>"


def yahoo_ticker(symbol: str):
    """Rate-limited replacement for `yfinance.Ticker(symbol)`."""
    import yfinance as yf
    return _PacedTicker(yf.Ticker(symbol))


def _quieten_rotation_errors():
    """
    Stop a locked log file from printing a traceback on every log call.

    The SDK logs through a TimedRotatingFileHandler, which rotates by renaming.
    On Windows a rename fails while another process holds the file — and the
    normal configuration here is exactly that: the MCP server and the dashboard
    both import this module and both open the same conf/webull_sdk.log. At the
    rollover boundary every subsequent log call then printed a full
    PermissionError traceback to stderr, which for a stdio MCP server is noise
    on the same channel a user reads errors from.

    The rotation genuinely cannot happen while the file is held, so there is
    nothing to fix in the logging itself; what is wrong is reporting it once per
    record. Failed rotations are swallowed, everything else still reports.
    """
    import logging.handlers

    for logger_name in ("webull.core", "webull"):
        for handler in logging.getLogger(logger_name).handlers:
            if not isinstance(handler, logging.handlers.BaseRotatingHandler):
                continue
            if getattr(handler, "_fm_quietened", False):
                continue

            original = handler.handleError

            def handle(record, _h=handler, _orig=original):
                exc = sys.exc_info()[1]
                if isinstance(exc, (PermissionError, OSError)):
                    return          # a locked file cannot be rotated; say it once, in code
                _orig(record)

            handler.handleError = handle
            handler._fm_quietened = True


# Header values the SDK prints verbatim when it logs a request.
_SENSITIVE_HEADERS = ("x-app-key", "x-signature", "x-access-token", "x-signature-nonce")


class _RedactSecretsFilter:
    """
    Scrub credentials out of SDK log records.

    Lowering the log level alone is not enough: the SDK dumps the entire signed
    request -- app key, HMAC signature and access token -- at ERROR level
    whenever a call fails, and routine 429s make that a frequent event.
    """

    def __init__(self, secrets=()):
        import re
        self._literals = [s for s in secrets if s and len(s) >= 8]
        self._header_re = re.compile(
            r'("(?:%s)"\s*:\s*")([^"]+)(")' % "|".join(_SENSITIVE_HEADERS),
            re.IGNORECASE,
        )

    def _redact(self, text: str) -> str:
        for secret in self._literals:
            text = text.replace(secret, "***REDACTED***")
        return self._header_re.sub(r"\1***REDACTED***\3", text)

    def filter(self, record):
        try:
            record.msg = self._redact(record.getMessage())
            record.args = ()
        except Exception:
            # Never let redaction failure drop a log record silently.
            record.args = ()
        return True


def get_api_client():
    """Build (once) and return the shared Webull ApiClient."""
    global _API_CLIENT
    if _API_CLIENT is not None:
        return _API_CLIENT

    with _CLIENT_LOCK:
        if _API_CLIENT is not None:
            return _API_CLIENT

        from webull.core.client import ApiClient
        import logging

        # The sandbox is a separate Webull deployment with its own registry, so
        # production credentials authenticate against it as 401 UNAUTHORIZED.
        # Verified live: submitting into paper with the prod key returns
        # "Invalid credentials ... ensure you are connecting to the correct
        # environment". Paper therefore takes its own pair when one is set, and
        # says exactly this when it is not, rather than leaving someone to read
        # a bare 401 as "my key is wrong".
        app_key, app_secret = resolved_credentials()

        if not app_key or not app_secret:
            raise ValueError("Webull App Key and App Secret must be set in .env")

        api_client = ApiClient(
            app_key,
            app_secret,
            WEBULL_REGION_ID.lower(),
            token_check_duration_seconds=10,
            token_check_interval_seconds=2,
        )
        token_dir = WEBULL_TOKEN_DIR or os.path.join(BASE_DIR, "conf")
        # `conf/` holds the token and the SDK log, so it is gitignored -- which
        # means it does not exist on a fresh clone or a fresh install, and the
        # file logger below opens its path without creating the directory. The
        # first Webull call on a new machine therefore died with
        # FileNotFoundError before any of the real work started. Nothing else
        # creates it: not the installer, not the SDK's token manager.
        os.makedirs(token_dir, exist_ok=True)
        api_client.set_token_dir(token_dir)

        # WARNING, not the SDK default of DEBUG: DEBUG logs credentials.
        api_client.set_stream_logger(log_level=logging.WARNING, stream=sys.stderr)
        log_file = os.path.join(token_dir, "webull_sdk.log")
        api_client.set_file_logger(path=log_file, log_level=logging.WARNING)
        _quieten_rotation_errors()

        # ...and redact, because the SDK logs the signed request at ERROR too.
        redactor = _RedactSecretsFilter(
            (WEBULL_APP_KEY, WEBULL_APP_SECRET, app_key, app_secret))
        sdk_logger = logging.getLogger("webull.core")
        if not any(isinstance(f, _RedactSecretsFilter) for f in sdk_logger.filters):
            sdk_logger.addFilter(redactor)
        for handler in sdk_logger.handlers:
            if not any(isinstance(f, _RedactSecretsFilter) for f in handler.filters):
                handler.addFilter(redactor)

        # Point the client at the sandbox when asked. The SDK only ships
        # production endpoints, so the hosts have to be injected by hand.
        if is_paper_environment():
            region = WEBULL_REGION_ID.lower()
            region_cfg = SANDBOX_ENDPOINTS.get(region)
            if not region_cfg:
                raise RuntimeError(
                    f"WEBULL_ENVIRONMENT={WEBULL_ENVIRONMENT} requests the sandbox, but "
                    f"no sandbox endpoint is published for region '{region}'. "
                    f"Known: {', '.join(sorted(SANDBOX_ENDPOINTS))}. "
                    "Refusing to fall through to production."
                )
            for api_type, endpoint in region_cfg.items():
                api_client.add_endpoint(region, endpoint, api_type)
            print(f"Webull SANDBOX environment active (region {region}): "
                  f"{region_cfg['api']} — orders are simulated.", file=sys.stderr)

        _API_CLIENT = api_client
        return _API_CLIENT


def get_data_client():
    """Shared DataClient. Constructing one per request re-runs client init and burns rate budget."""
    global _DATA_CLIENT
    if _DATA_CLIENT is None:
        with _CLIENT_LOCK:
            if _DATA_CLIENT is None:
                from webull.data.data_client import DataClient
                _DATA_CLIENT = DataClient(get_api_client())
    return _DATA_CLIENT


def get_webull_data(symbol: str, interval: str = "D", count: int = 200) -> pd.DataFrame:
    """
    Fetch historical data from Webull OpenAPI using SDK.

    Returns bars sorted OLDEST FIRST. The API itself returns them newest-first;
    normalising here is load-bearing, because every downstream consumer reads
    .iloc[-1] / .tail(n) as the most recent bar.
    """
    data_client = get_data_client()

    sym_upper = symbol.upper()
    if sym_upper.endswith(".HK"):
        category = "HK_STOCK"
    elif sym_upper.endswith(".SS") or sym_upper.endswith(".SZ"):
        category = "CN_STOCK"
    else:
        category = "US_STOCK"

    timespan = WEBULL_TIMESPAN.get(interval)
    if timespan is None:
        # Refuse locally rather than spend a request learning the same thing
        # from a 417, which the fallback then papers over.
        raise ValueError(
            f"Webull has no timespan for interval '{interval}'. "
            f"Known: {', '.join(sorted(WEBULL_TIMESPAN))}.")

    kwargs = {
        "symbol": sym_upper.replace(".HK", "").replace(".SS", "").replace(".SZ", ""),
        "category": category,
        "timespan": timespan,
        "count": str(count)
    }

    res = call_webull(data_client.market_data.get_history_bar, **kwargs)

    # Extract data from SDK response wrapper or requests.Response
    bars = None
    if hasattr(res, "json"):
        try:
            bars = res.json()
        except Exception:
            pass

    if bars is None:
        if hasattr(res, "data") and res.data:
            bars = res.data
        elif isinstance(res, dict) and "data" in res:
            bars = res["data"]
        elif isinstance(res, list):
            bars = res

    if isinstance(bars, dict) and "data" in bars:
        bars = bars["data"]

    if not bars or not isinstance(bars, list):
        raise ValueError(f"Failed to fetch K-Line data from Webull API: {res}")

    # Format to DataFrame
    df = pd.DataFrame(bars)

    # Standardize columns to lowercase
    df.columns = [col.lower() for col in df.columns]

    # Convert numerical columns
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    required_cols = ["time", "open", "high", "low", "close", "volume"]
    for col in required_cols:
        if col not in df.columns:
            raise KeyError(f"Missing required K-Line column: {col}")

    # ---- The fix -------------------------------------------------------
    # Webull returns newest-first. Sort ascending on parsed datetimes (not on
    # the formatted string) so that .iloc[-1] genuinely is the latest bar.
    df["_ts"] = _parse_bar_times(df["time"])
    df = df.sort_values("_ts", kind="mergesort").reset_index(drop=True)
    df["time"] = df["_ts"].dt.strftime("%Y-%m-%d %H:%M:%S")

    return df[required_cols]


# Local Cache in-memory structure: { (symbol, interval, count): (timestamp, dataframe, source_name) }
DATA_CACHE = {}
CACHE_EXPIRATION_SECONDS = 60  # Cache lasts for 60 seconds


def _price_sources(symbol: str, interval: str, count: int) -> list:
    """
    (loader, label) pairs to try in order: the configured broker, then Yahoo.

    Webull keeps its dedicated path because it is the one exercised for real and
    carries the .BK regional retry. Any other broker declaring `history_bars`
    goes through the protocol. Yahoo is always last and always announces itself
    -- a fallback that does not say it is one is how a divergence between two
    feeds went unnoticed for months.

    A broker with no credentials is not tried at all. It is not a fallback if
    the first source could never have worked: every price lookup paid for a
    round trip that was going to fail, and printed a scary line about the feed
    failing to a user whose only mistake was not having an account.
    """
    sources = []
    broker_name = os.getenv("FINANCE_BROKER", "webull").strip().lower()

    if broker_name == "webull":
        if have_credentials():
            sources.append((lambda: get_webull_data(symbol, interval, count),
                            "Webull OpenAPI"))
    else:
        def broker_bars():
            from dashboard import brokers, capabilities
            adapter = brokers.get()
            if capabilities.HISTORY_BARS not in capabilities.effective(adapter):
                raise RuntimeError(
                    f"{adapter.name} does not serve historical bars here")
            return adapter.history_bars(symbol, interval, count)

        try:
            from dashboard import brokers, capabilities
            configured = not capabilities.missing_credentials(brokers.get())
        except Exception:
            configured = True            # unknowable fails open, as everywhere else
        if configured:
            sources.append((broker_bars, f"{broker_name.upper()} broker feed"))

    # Not labelled a fallback when it is the only source. Someone running
    # without a broker is on Yahoo by design, and calling their normal feed a
    # fallback reads as a degradation they should fix.
    sources.append((lambda: get_yfinance_data(symbol, interval, count),
                    "Yahoo Finance (Fallback)" if sources else "Yahoo Finance"))
    return sources


def fetch_data(symbol: str, interval: str = "D", count: int = 200) -> tuple[pd.DataFrame, str]:
    """
    Main function to retrieve K-Line data.
    Implements a local caching layer to avoid duplicate/frequent API pings.
    Tries Webull OpenAPI first; if it fails (due to lack of subscription or auth),
    automatically falls back to Yahoo Finance.

    Every frame returned is guaranteed oldest-first, fresh, and numerically sane
    (see _validate_frame). Integrity failures raise instead of returning.

    Returns:
        tuple: (DataFrame containing time/open/high/low/close/volume, data_source_name)
    """
    import time

    cache_key = (symbol.upper(), interval.upper(), count)
    current_time = time.time()

    if cache_key in DATA_CACHE:
        cached_time, cached_df, cached_source = DATA_CACHE[cache_key]
        if current_time - cached_time < CACHE_EXPIRATION_SECONDS:
            return cached_df.copy(), cached_source + " (Cached)"

    # Then the shared on-disk layer. The MCP server and the dashboard are two
    # processes asking for the same bars, and each used to pay full price for
    # what the other had just fetched. A hit still goes through _validate_frame
    # below, so this skips the download, never the integrity gate -- a frame
    # that has gone stale on disk is rejected exactly as a stale live one is.
    disk = barcache.load(symbol, interval, count)
    if disk is not None:
        frame, disk_source, age = disk
        try:
            validated = _validate_frame(frame, symbol.upper(), interval, disk_source)
        except DataIntegrityError:
            validated = None          # aged out or malformed; fetch it again
        if validated is not None:
            DATA_CACHE[cache_key] = (current_time, validated, disk_source)
            return validated, f"{disk_source} (Disk cache, {age:.0f}s old)"

    # Each source is validated on its own before being accepted, so a stale or
    # malformed Webull response can still fall back to Yahoo rather than failing
    # the whole request. We only raise once every source has been exhausted --
    # and if staleness is what killed them, we raise StaleDataError specifically.
    errors = []

    def _try(loader, source_label):
        frame = loader()
        label = source_label
        resolved = frame.attrs.get("resolved_symbol", symbol.upper())
        if resolved != symbol.upper():
            label += f" [resolved as {resolved}]"
        return _validate_frame(frame, symbol.upper(), interval, label), label

    # Sources in order: the configured broker, then Yahoo. A Saxo or IBKR user
    # supplied credentials to a broker that serves bars; leaving them on the
    # public fallback for every price in the server was the largest thing the
    # broker sweep found, and it was invisible because the tools still worked.
    df = source = None
    for loader, label in _price_sources(symbol, interval, count):
        try:
            df, source = _try(loader, label)
            break
        except Exception as e:
            errors.append((label, e))
            print(f"{label} failed for {symbol}: {e}", file=sys.stderr)

    if df is None:
        detail = "; ".join(f"{name}: {err}" for name, err in errors)
        tried = ", ".join(name for name, _ in errors)
        if any(isinstance(err, StaleDataError) for _, err in errors):
            raise StaleDataError(
                f"No source returned fresh {interval} data for {symbol.upper()}. {detail}")
        raise RuntimeError(
            f"Every price source failed for {symbol} ({tried}). {detail}")

    # Only frames that passed validation are cached, in memory and on disk.
    DATA_CACHE[cache_key] = (current_time, df, source)
    barcache.store(symbol, interval, count, df, base_source(source))
    return df, source


def unwrap(res):
    """Return the decoded payload from an SDK response wrapper."""
    if hasattr(res, "json"):
        try:
            return res.json()
        except Exception:
            pass
    if hasattr(res, "data"):
        return res.data
    return res


# Accounts, buying power, positions and the order lifecycle now live in
# dashboard/broker.py. This module is the market-data client plus the shared
# signed-request plumbing; it deliberately no longer exposes a way to trade.


def get_provenance(symbol: str, interval: str = "D") -> dict:
    """
    Where a price came from and how fresh it was, captured at the moment of use.

    Anything that records a price for later reading -- a journal thesis, an
    alert, a triggered notification -- should store this alongside it. Had these
    fields existed, the reversed-bar bug would have been obvious from the
    journal alone: entries would have shown a `bar_time` ten months in the past
    sitting next to a timestamp from today.
    """
    try:
        df, source = fetch_data(symbol, interval, 5)
        latest = df.iloc[-1]
        return {
            "source": base_source(source),
            "bar_time": str(latest["time"]),
            "bar_close": round(float(latest["close"]), 4),
            "captured_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "interval": interval.upper(),
        }
    except Exception as e:
        # Provenance is metadata; failing to capture it must never block the
        # write it annotates. Record the failure honestly instead.
        return {
            "source": "UNAVAILABLE",
            "error": str(e)[:200],
            "captured_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "interval": interval.upper(),
        }


def freshness_line(df: pd.DataFrame, source: str, interval: str = "D") -> str:
    """
    A one-line provenance stamp for any response carrying prices.

    The staleness gate already refuses to serve old bars, but refusing is only
    half the job: a reader still cannot tell a live number from a merely
    acceptable one, and will quote both with the same confidence. So every
    price-bearing answer states which bar it is quoting and how old that bar is.
    """
    try:
        newest = pd.to_datetime(df["time"].iloc[-1])
    except Exception:
        return f"`Source: {base_source(source)} · bar timestamp unavailable`\n\n"

    iv = interval.upper()
    if iv in STALENESS_TOLERANCE_SESSIONS:
        behind = market_calendar.sessions_stale(newest.date())
        age = ("current session" if behind == 0
               else f"{behind} trading session{'s' if behind != 1 else ''} old")
    else:
        hours = (datetime.datetime.utcnow() - newest.to_pydatetime()).total_seconds() / 3600
        age = f"{hours:.1f}h old" if hours < 48 else f"{hours / 24:.1f} days old"

    cached = " · from 60s cache" if "(Cached)" in source else ""
    return (f"`Latest {iv} bar: {newest:%Y-%m-%d %H:%M} ({age}) · "
            f"source: {base_source(source)}{cached} · "
            f"retrieved {datetime.datetime.now():%H:%M:%S}`\n\n")


def bar_age(df: pd.DataFrame, interval: str = "D") -> dict:
    """
    How old the newest bar is, in a form small enough for a table cell.

    `freshness_line` answers this for a single-symbol response. A sweep needs it
    per row: one stale name among twenty fresh ones is invisible in a summary
    line, and a watchlist scan or sector heatmap is exactly where that hides.

    Returns {"bar", "as_of", "age", "behind", "current"} — `behind` is trading
    sessions for daily and slower, hours otherwise, so callers can sort or flag
    on it rather than parsing the label.
    """
    try:
        newest = pd.to_datetime(df["time"].iloc[-1])
    except Exception:
        return {"bar": None, "as_of": "unknown", "age": "unknown",
                "behind": float("inf"), "current": False}

    iv = (interval or "D").upper()
    if iv in STALENESS_TOLERANCE_SESSIONS:
        behind = market_calendar.sessions_stale(newest.date())
        age = "current" if behind == 0 else f"{behind} session{'s' if behind != 1 else ''}"
        return {"bar": newest, "as_of": f"{newest:%Y-%m-%d}", "age": age,
                "behind": float(behind), "current": behind == 0}

    hours = (datetime.datetime.utcnow() - newest.to_pydatetime()).total_seconds() / 3600
    age = f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"
    return {"bar": newest, "as_of": f"{newest:%Y-%m-%d %H:%M}", "age": age,
            "behind": hours, "current": hours < 24}


def freshness_summary(ages, interval: str = "D", label: str = "series") -> str:
    """
    One as-of line for a multi-symbol sweep, stated as the WORST case.

    A sweep is only as current as its stalest member, so the header quotes that
    rather than an average or the first row — an average would let one
    three-session-old name disappear into nineteen fresh ones. Per-row ages
    still appear in the table; this is the line a reader sees without scanning.
    """
    usable = [a for a in ages if a and a.get("bar") is not None]
    if not usable:
        return "`As of: no dated bars in this result`\n\n"

    stalest = max(usable, key=lambda a: a["behind"])
    freshest = min(usable, key=lambda a: a["behind"])
    n = len(usable)

    if stalest["behind"] == freshest["behind"]:
        return (f"`As of {stalest['as_of']} ({stalest['age']}) — all {n} {label} · "
                f"retrieved {datetime.datetime.now():%H:%M:%S}`\n\n")
    return (f"`As of {stalest['as_of']} ({stalest['age']}) at the stalest, "
            f"{freshest['as_of']} ({freshest['age']}) at the freshest, over {n} {label} · "
            f"retrieved {datetime.datetime.now():%H:%M:%S}`\n\n")


def base_source(source: str) -> str:
    """Source label without the cache marker, so banners dedupe across cached/live hits."""
    return source.replace(" (Cached)", "")


def fallback_warning(source: str) -> str:
    """
    Banner for results not served by the primary feed.

    Callers that drop the source string are how a silent source substitution
    goes unnoticed, so make it impossible to render the data without it.

    A substitution and a configuration are not the same event, and the wording
    used to conflate them. Someone who never connected a broker was told on
    every single price that "the primary Webull feed did not serve this
    request" -- an alarm about the absence of something they had not asked for,
    on what is their normal and correct feed. `_price_sources` only omits the
    "(Fallback)" suffix when no broker feed was in the running at all, so the
    label is enough to tell the two apart.
    """
    if source.startswith("Webull OpenAPI") or source.endswith(" broker feed"):
        return ""
    if "(Fallback)" not in source:
        return (
            f"> Data source: {source}. No broker is configured, so prices come "
            "from the public feed — which is what the broker-free setup is meant "
            "to do. Connect a broker if you want its own quotes.\n\n"
        )
    return (
        f"> **Warning — data source: {source}.** The primary Webull feed did not serve this "
        "request. Values may differ from the broker's own quotes.\n\n"
    )
