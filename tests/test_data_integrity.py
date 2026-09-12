"""
Regression tests for the market-data integrity contract.

The bug these exist to prevent: the Webull OpenAPI returns K-line bars
newest-first, `get_webull_data` did not sort, and every consumer reads
`.iloc[-1]` / `.tail(n)` as "the most recent bar". The tool therefore reported
the *oldest* bar of the requested window as the current price -- MU at $118.70
when it was trading at $881.47 -- and computed every technical indicator on a
time-reversed series.

The fixtures in tests/fixtures/ are real recorded API responses, still in the
descending order the API delivered them in.
"""
import datetime
import json
import re
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import webull_client as wc
from dashboard import broker
from dashboard.webull_client import DataIntegrityError, StaleDataError

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def mu_bars():
    """250 real MU daily bars, newest-first, exactly as Webull delivered them."""
    return load_fixture("webull_mu_daily_descending.json")


def make_frame(bars):
    df = pd.DataFrame(bars)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c])
    return df


def synthetic_frame(n=30, start_price=100.0, step=1.0, end=None, freq="D"):
    """Oldest-first frame rising by `step` each bar, ending `end` (default: now)."""
    end = end or datetime.datetime.utcnow()
    times = [end - datetime.timedelta(days=(n - 1 - i)) for i in range(n)]
    closes = [start_price + step * i for i in range(n)]
    return pd.DataFrame({
        "time": [t.strftime("%Y-%m-%d %H:%M:%S") for t in times],
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })


# =====================================================================
# The regression test for the reported bug
# =====================================================================

def test_fixture_really_is_descending(mu_bars):
    """Guard the guard: if this fixture ever gets pre-sorted, the tests below stop testing anything."""
    times = pd.to_datetime([b["time"] for b in mu_bars])
    assert not times.is_monotonic_increasing
    assert times[0] > times[-1], "fixture should be newest-first, as the API delivers it"


def test_webull_data_is_sorted_oldest_first(monkeypatch, mu_bars):
    df = _fetch_with_fake_sdk(monkeypatch, mu_bars)
    times = pd.to_datetime(df["time"])
    assert times.is_monotonic_increasing


def test_latest_bar_is_the_newest_not_the_oldest(monkeypatch, mu_bars):
    """
    The exact defect: .iloc[-1] must be 2026-08-06 at $881.47, not the window's
    oldest bar at $118.70.
    """
    df = _fetch_with_fake_sdk(monkeypatch, mu_bars)

    newest = max(mu_bars, key=lambda b: b["time"])
    oldest = min(mu_bars, key=lambda b: b["time"])

    assert df["time"].iloc[-1].startswith(newest["time"][:10])
    assert df["close"].iloc[-1] == pytest.approx(float(newest["close"]))
    assert df["close"].iloc[-1] != pytest.approx(float(oldest["close"]))
    # And the oldest bar is where it belongs.
    assert df["close"].iloc[0] == pytest.approx(float(oldest["close"]))


def _fetch_with_fake_sdk(monkeypatch, bars):
    """Run get_webull_data against a stubbed SDK that replays a recorded response."""
    import webull.data.data_client as ddc

    class FakeMarketData:
        def get_history_bar(self, **kwargs):
            return list(bars)

    class FakeDataClient:
        def __init__(self, api_client):
            self.market_data = FakeMarketData()

    monkeypatch.setattr(wc, "get_api_client", lambda: object())
    monkeypatch.setattr(ddc, "DataClient", FakeDataClient)
    return wc.get_webull_data("MU", "D", 250)


# =====================================================================
# _validate_frame: the invariant every source passes through
# =====================================================================

def test_validate_rejects_descending_frame():
    df = synthetic_frame(n=30).iloc[::-1].reset_index(drop=True)
    with pytest.raises(DataIntegrityError, match="ascending time order"):
        wc._validate_frame(df, "TEST", "D", "unit-test")


def test_validate_accepts_fresh_ascending_frame():
    df = synthetic_frame(n=30)
    out = wc._validate_frame(df, "TEST", "D", "unit-test")
    assert len(out) == 30
    assert list(out.columns) == ["time", "open", "high", "low", "close", "volume"]


def test_validate_rejects_stale_frame():
    """A window ending 10 months ago -- the symptom originally reported."""
    end = datetime.datetime.utcnow() - datetime.timedelta(days=300)
    df = synthetic_frame(n=30, end=end)
    with pytest.raises(StaleDataError, match="Refusing to return stale prices"):
        wc._validate_frame(df, "TEST", "D", "unit-test")


def test_validate_tolerates_a_weekend_or_holiday_gap():
    """
    A bar from the most recent *session* is fresh, however many calendar days
    ago that was. Checked on a Monday after a 3-day holiday weekend, Friday's
    bar is 0 sessions stale and must not fire.
    """
    from dashboard import market_calendar as mc
    latest_session = mc.previous_trading_day(datetime.date.today() + datetime.timedelta(days=1))
    end = datetime.datetime.combine(latest_session, datetime.time(21, 0))
    df = synthetic_frame(n=30, end=end)
    wc._validate_frame(df, "TEST", "D", "unit-test")  # must not raise


def test_validate_catches_a_short_outage_that_calendar_days_would_miss():
    """
    The point of session-counting: a 3-calendar-day gap used to pass under the
    old 5-day tolerance, so a genuine multi-session outage was invisible.
    """
    from dashboard import market_calendar as mc
    latest = mc.previous_trading_day(datetime.date.today() + datetime.timedelta(days=1))
    three_sessions_back = latest
    for _ in range(3):
        three_sessions_back = mc.previous_trading_day(three_sessions_back)

    df = synthetic_frame(n=30, end=datetime.datetime.combine(three_sessions_back, datetime.time(21, 0)))
    with pytest.raises(StaleDataError, match="trading session"):
        wc._validate_frame(df, "TEST", "D", "unit-test")


def test_validate_rejects_nan_in_newest_bar():
    df = synthetic_frame(n=30)
    df.loc[df.index[-1], "close"] = float("nan")
    with pytest.raises(DataIntegrityError, match="NaN"):
        wc._validate_frame(df, "TEST", "D", "unit-test")


def test_validate_rejects_impossible_ohlc():
    df = synthetic_frame(n=30)
    df.loc[df.index[-1], "high"] = df.loc[df.index[-1], "close"] - 10
    with pytest.raises(DataIntegrityError, match="low<=close<=high"):
        wc._validate_frame(df, "TEST", "D", "unit-test")


def test_validate_rejects_empty_frame():
    df = synthetic_frame(n=0)
    with pytest.raises(DataIntegrityError, match="zero bars"):
        wc._validate_frame(df, "TEST", "D", "unit-test")


def test_validate_rejects_unparseable_timestamps():
    df = synthetic_frame(n=5)
    df.loc[2, "time"] = "not-a-date"
    with pytest.raises(DataIntegrityError, match="Could not parse"):
        wc._validate_frame(df, "TEST", "D", "unit-test")


def test_parse_bar_times_handles_epoch_millis():
    ms = pd.Series([1754452800000, 1754539200000])
    out = wc._parse_bar_times(ms)
    assert out.is_monotonic_increasing
    assert out.dt.year.iloc[0] == 2025


# =====================================================================
# Fallback must never be silent
# =====================================================================

def test_fallback_warning_silent_for_primary_source():
    assert wc.fallback_warning("Webull OpenAPI") == ""
    assert wc.fallback_warning("Webull OpenAPI (Cached)") == ""


def test_fallback_warning_loud_for_yahoo():
    banner = wc.fallback_warning("Yahoo Finance (Fallback)")
    assert "Yahoo Finance (Fallback)" in banner
    # The word, not a pictograph: the banner has to survive any renderer, and a
    # warning glyph that fails to load leaves the sentence reading as normal.
    assert "Warning" in banner


# =====================================================================
# fetch_data: each source validated independently
# =====================================================================

def _clear_cache():
    wc.DATA_CACHE.clear()


def test_stale_primary_falls_back_to_fresh_secondary(monkeypatch):
    """A stale Webull response should not fail the request outright."""
    _clear_cache()
    stale_end = datetime.datetime.utcnow() - datetime.timedelta(days=300)
    monkeypatch.setattr(wc, "get_webull_data",
                        lambda s, i, c: synthetic_frame(n=30, end=stale_end))
    monkeypatch.setattr(wc, "get_yfinance_data",
                        lambda s, i, c: synthetic_frame(n=30))

    df, source = wc.fetch_data("AAA", "D", 30)

    assert source.startswith("Yahoo Finance (Fallback)")
    assert wc.fallback_warning(source) != "", "the substitution must be announced"
    assert pd.to_datetime(df["time"]).is_monotonic_increasing


def test_stale_everywhere_raises_stale_data_error(monkeypatch):
    _clear_cache()
    stale_end = datetime.datetime.utcnow() - datetime.timedelta(days=300)
    monkeypatch.setattr(wc, "get_webull_data",
                        lambda s, i, c: synthetic_frame(n=30, end=stale_end))
    monkeypatch.setattr(wc, "get_yfinance_data",
                        lambda s, i, c: synthetic_frame(n=30, end=stale_end))

    with pytest.raises(StaleDataError, match="No source returned fresh"):
        wc.fetch_data("AAA", "D", 30)


def test_stale_frames_are_never_cached(monkeypatch):
    _clear_cache()
    stale_end = datetime.datetime.utcnow() - datetime.timedelta(days=300)
    monkeypatch.setattr(wc, "get_webull_data",
                        lambda s, i, c: synthetic_frame(n=30, end=stale_end))
    monkeypatch.setattr(wc, "get_yfinance_data",
                        lambda s, i, c: synthetic_frame(n=30, end=stale_end))

    with pytest.raises(StaleDataError):
        wc.fetch_data("AAA", "D", 30)
    assert ("AAA", "D", 30) not in wc.DATA_CACHE


def test_descending_primary_falls_back_rather_than_serving_reversed_bars(monkeypatch):
    """Belt and braces: even if the sort regressed, reversed bars must not ship."""
    _clear_cache()
    monkeypatch.setattr(wc, "get_webull_data",
                        lambda s, i, c: synthetic_frame(n=30).iloc[::-1].reset_index(drop=True))
    monkeypatch.setattr(wc, "get_yfinance_data",
                        lambda s, i, c: synthetic_frame(n=30))

    df, source = wc.fetch_data("AAA", "D", 30)
    assert source.startswith("Yahoo Finance (Fallback)")
    assert pd.to_datetime(df["time"]).is_monotonic_increasing


# =====================================================================
# Request pacing / 429 handling
# =====================================================================

class FakeRateLimitError(Exception):
    def __init__(self):
        super().__init__("HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests")
        self.http_status = 429
        self.error_code = "TOO_MANY_REQUESTS"


def test_rate_limiter_spaces_consecutive_calls(monkeypatch):
    import time
    # Driven by a fake clock rather than wall-clock: Windows' sleep granularity
    # is ~15.6 ms, which makes real-time assertions on 50 ms intervals flaky.
    clock = {"t": 1000.0}
    slept = []

    def fake_monotonic():
        return clock["t"]

    def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    interval, calls = 0.05, 4
    limiter = wc._RateLimiter(interval)
    for _ in range(calls):
        limiter.acquire()

    # The first acquire does not wait; every subsequent one waits a full interval.
    assert len(slept) == calls - 1
    assert all(s == pytest.approx(interval) for s in slept)
    assert sum(slept) == pytest.approx((calls - 1) * interval)


def test_call_webull_retries_a_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(wc, "WEBULL_RETRY_BACKOFF", 0.01)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeRateLimitError()
        return "ok"

    assert wc.call_webull(flaky) == "ok"
    assert calls["n"] == 3


def test_call_webull_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(wc, "WEBULL_RETRY_BACKOFF", 0.01)
    monkeypatch.setattr(wc, "WEBULL_MAX_RETRIES", 2)
    calls = {"n": 0}

    def always_limited():
        calls["n"] += 1
        raise FakeRateLimitError()

    with pytest.raises(FakeRateLimitError):
        wc.call_webull(always_limited)
    assert calls["n"] == 2, "must not retry forever -- the caller's fallback still needs to run"


def test_call_webull_does_not_retry_other_errors(monkeypatch):
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("bad symbol")

    with pytest.raises(ValueError):
        wc.call_webull(broken)
    assert calls["n"] == 1, "only rate limiting is retryable"


def test_rate_limit_detection():
    assert wc._is_rate_limited(FakeRateLimitError())
    assert not wc._is_rate_limited(ValueError("no data"))


# =====================================================================
# Client construction must not deadlock
# =====================================================================

def test_client_lock_is_reentrant():
    """
    get_data_client() holds _CLIENT_LOCK and calls get_api_client(), which
    acquires it again on the same thread. With a plain Lock that self-deadlocks
    and hangs the server on its first data request.
    """
    assert wc._CLIENT_LOCK.acquire(timeout=1)
    try:
        assert wc._CLIENT_LOCK.acquire(timeout=1), "_CLIENT_LOCK must be reentrant (RLock)"
        wc._CLIENT_LOCK.release()
    finally:
        wc._CLIENT_LOCK.release()


def test_get_data_client_does_not_hang(monkeypatch):
    import threading

    sentinel = object()
    monkeypatch.setattr(wc, "_API_CLIENT", sentinel)
    monkeypatch.setattr(wc, "_DATA_CLIENT", None)

    import webull.data.data_client as ddc
    monkeypatch.setattr(ddc, "DataClient", lambda api: ("dc", api))

    done = threading.Event()
    result = {}

    def go():
        result["client"] = wc.get_data_client()
        done.set()

    threading.Thread(target=go, daemon=True).start()
    assert done.wait(timeout=5), "get_data_client() deadlocked"
    assert result["client"] == ("dc", sentinel)


# =====================================================================
# Yahoo download sizing
# =====================================================================

def test_yf_period_scales_with_requested_bar_count():
    """A 26-bar heatmap request used to download a full year (~250 bars)."""
    small = wc._yf_period_for("1d", 26)
    large = wc._yf_period_for("1d", 250)
    assert small.endswith("d") and large.endswith("d")
    assert int(small[:-1]) < int(large[:-1])
    assert int(small[:-1]) < 100, f"26 daily bars should not need {small}"


def test_yf_period_respects_yahoo_intraday_caps():
    # Yahoo refuses 1m beyond 7d and 15m beyond 60d.
    assert int(wc._yf_period_for("1m", 10_000)[:-1]) <= 7
    assert int(wc._yf_period_for("15m", 10_000)[:-1]) <= 59
    assert int(wc._yf_period_for("1h", 100_000)[:-1]) <= 729


def test_yf_period_covers_the_bars_requested():
    """Headroom must exist, or indicator warm-up lands a bar short."""
    for count in (5, 26, 100, 250):
        days = int(wc._yf_period_for("1d", count)[:-1])
        trading_days = days * 5 / 7
        assert trading_days >= count, f"period {days}d gives ~{trading_days:.0f} bars, need {count}"


# =====================================================================
# Yahoo pacing
# =====================================================================
# Yahoo is both the fallback for every Webull failure and the primary source
# for the fundamentals tools, so it was the one feed that could be hit a dozen
# times in a single profile sweep -- with no pacing at all.

class FakeYFRateLimitError(Exception):
    def __init__(self):
        super().__init__("Too Many Requests. Rate limited. Try after a while.")


def test_yahoo_rate_limit_is_recognised():
    from yfinance.exceptions import YFRateLimitError
    assert wc._is_yahoo_rate_limited(YFRateLimitError()) is True
    assert wc._is_yahoo_rate_limited(FakeYFRateLimitError()) is True
    assert wc._is_yahoo_rate_limited(Exception("HTTP 429")) is True
    assert wc._is_yahoo_rate_limited(ValueError("No data found for this ticker")) is False


def test_call_yahoo_retries_a_throttled_request_then_succeeds(monkeypatch):
    monkeypatch.setattr(wc, "YF_RETRY_BACKOFF", 0.01)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeYFRateLimitError()
        return "ok"

    assert wc.call_yahoo(flaky) == "ok"
    assert calls["n"] == 3


def test_call_yahoo_re_raises_once_retries_are_spent(monkeypatch):
    """Throttling must stay visible; a swallowed 429 becomes a phantom 'no data'."""
    monkeypatch.setattr(wc, "YF_RETRY_BACKOFF", 0.01)
    monkeypatch.setattr(wc, "YF_MAX_RETRIES", 2)

    def always():
        raise FakeYFRateLimitError()

    with pytest.raises(FakeYFRateLimitError):
        wc.call_yahoo(always)


def test_call_yahoo_does_not_retry_other_errors(monkeypatch):
    calls = {"n": 0}

    def missing():
        calls["n"] += 1
        raise ValueError("No data returned from Yahoo Finance for ticker: NOPE")

    with pytest.raises(ValueError):
        wc.call_yahoo(missing)
    assert calls["n"] == 1, "a delisted ticker must fail immediately, not after 3 waits"


class _StubTicker:
    """Stands in for yfinance.Ticker: `.info` fetches on read, `.history` on call."""
    def __init__(self):
        self.reads = 0
        self.calls = 0

    @property
    def info(self):
        self.reads += 1
        return {"sector": "Technology"}

    def history(self, period=None):
        self.calls += 1
        return f"bars:{period}"


def test_paced_ticker_paces_properties_on_read(monkeypatch):
    seen = []
    monkeypatch.setattr(wc._YF_LIMITER, "acquire", lambda: seen.append(1))
    stub = _StubTicker()

    assert wc._PacedTicker(stub).info == {"sector": "Technology"}
    assert stub.reads == 1
    assert len(seen) == 1


def test_paced_ticker_paces_methods_at_call_not_at_lookup(monkeypatch):
    """
    yfinance does its I/O behind lazy properties and methods. Looking a method
    up must cost nothing; invoking it must go through the budget.
    """
    seen = []
    monkeypatch.setattr(wc._YF_LIMITER, "acquire", lambda: seen.append(1))
    stub = _StubTicker()
    paced = wc._PacedTicker(stub)

    fn = paced.history                     # lookup only
    assert seen == [] and stub.calls == 0

    assert fn(period="5d") == "bars:5d"     # invocation
    assert stub.calls == 1 and len(seen) == 1


def test_paced_ticker_retries_a_throttled_property(monkeypatch):
    monkeypatch.setattr(wc, "YF_RETRY_BACKOFF", 0.01)

    class Throttled:
        def __init__(self):
            self.n = 0

        @property
        def info(self):
            self.n += 1
            if self.n < 2:
                raise FakeYFRateLimitError()
            return {"ok": True}

    assert wc._PacedTicker(Throttled()).info == {"ok": True}


def test_no_yfinance_call_site_bypasses_the_pacing_layer():
    """
    Ten tools built their own `yf.Ticker(...)`. A new one must not quietly
    reintroduce an unpaced path.
    """
    import glob
    offenders = []
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in [os.path.join(root, "finance_mcp.py")] + glob.glob(os.path.join(root, "dashboard", "*.py")):
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            # The factory itself is the one place allowed to build a raw Ticker.
            if "yf.Ticker(" in line and "_PacedTicker(" not in line:
                offenders.append(f"{os.path.basename(path)}:{i}")
    assert not offenders, ("construct tickers via webull_client.yahoo_ticker(); "
                           f"unpaced at {offenders}")


# =====================================================================
# Credentials must never reach the log file
# =====================================================================

def test_log_redaction_scrubs_credentials():
    """
    Lowering the SDK log level is not sufficient: it dumps the full signed
    request at ERROR whenever a call fails, and 429s are routine.

    The fixtures below are synthetic and must stay that way. This test was
    originally written by pasting a real failing request out of the SDK log --
    which put the live app key into a committed file, in every commit, and
    silently undid the rotation that the same incident had prompted. A test
    that proves secrets are scrubbed is the last place a real secret belongs.
    """
    import logging

    key = "deadbeefdeadbeefdeadbeefdeadbeef"
    secret = "cafebabecafebabecafebabecafebabe"
    signature = "AAAAsyntheticSignatureForTestingOnly000000000="
    token = "0123456789abcdef0123456789abcdef"

    raw = (
        'ServerException occurred. Request:{ "x-app-key": "%s", '
        '"x-signature": "%s", "x-access-token": "%s", '
        '"User-Agent": "WebullApiSDK" } app_secret=%s'
    ) % (key, signature, token, secret)

    record = logging.LogRecord("webull.core", logging.ERROR, "x", 1, raw, (), None)
    wc._RedactSecretsFilter((key, secret)).filter(record)

    for leaked in (key, secret, signature, token):
        assert leaked not in record.msg, f"credential leaked into log: {leaked[:8]}..."
    assert "WebullApiSDK" in record.msg, "redaction must not destroy benign context"


# =====================================================================
# Interval names: ours vs the broker's
# =====================================================================

def test_hourly_maps_to_the_timespan_webull_actually_has():
    """
    Webull's vocabulary is [M1, M5, M15, M30, M60, M120, M240, D, W, M, Y].
    There is no H1. Passing our canonical name straight through returned HTTP
    417 on every hourly request and fell back to Yahoo -- so the dashboard's
    1H and 4H views, and get_multi_timeframe's hourly leg, were never served by
    the primary feed while the daily leg was.
    """
    assert wc.WEBULL_TIMESPAN["H1"] == "M60"
    assert "H1" not in set(wc.WEBULL_TIMESPAN.values())


def test_every_timespan_we_send_is_one_webull_publishes():
    published = {"M1", "M5", "M15", "M30", "M60", "M120", "M240", "D", "W", "M", "Y"}
    assert set(wc.WEBULL_TIMESPAN.values()) <= published


def test_an_unknown_interval_is_refused_before_a_request_is_spent():
    """A 417 round trip only to be papered over by the fallback helps nobody."""
    with pytest.raises(ValueError, match="no timespan"):
        wc.get_webull_data("AAPL", interval="H4")


def test_an_unknown_interval_does_not_silently_become_daily_on_yahoo():
    """
    The Yahoo map defaulted to "1d", so a typo'd interval returned daily bars
    wearing the requested interval's name -- indicators computed on the wrong
    timeframe with nothing in the output to show it.
    """
    with pytest.raises(ValueError, match="No Yahoo interval"):
        wc.get_yfinance_data("AAPL", interval="H4")


def test_both_interval_maps_agree_on_what_exists():
    """A name one side knows and the other does not is a silent fallback."""
    assert set(wc.WEBULL_TIMESPAN) <= set(wc.INTERVAL_WEBULL_TO_YF)


# =====================================================================
# The shared on-disk bar cache
# =====================================================================

def test_a_stored_frame_comes_back_intact(tmp_path, monkeypatch):
    from dashboard import barcache
    monkeypatch.setattr(barcache, "CACHE_DIR", str(tmp_path))
    frame = rising_frame(20) if "rising_frame" in dir() else None
    frame = pd.DataFrame({
        "time": ["2026-08-06 04:00:00", "2026-08-07 04:00:00"],
        "open": [1.0, 2.0], "high": [2.0, 3.0], "low": [0.5, 1.5],
        "close": [1.5, 2.5], "volume": [10, 20],
    })
    assert barcache.store("AAPL", "D", 2, frame, "Webull OpenAPI")
    got = barcache.load("AAPL", "D", 2)
    assert got is not None
    back, source, age = got
    assert list(back["close"]) == [1.5, 2.5]
    assert source == "Webull OpenAPI"
    assert age < 5


def test_the_time_column_comes_back_as_strings(tmp_path, monkeypatch):
    """
    Everything downstream reads `time` as the "%Y-%m-%d %H:%M:%S" string form.
    A CSV round trip would otherwise hand back whatever pandas inferred.
    """
    from dashboard import barcache
    monkeypatch.setattr(barcache, "CACHE_DIR", str(tmp_path))
    frame = pd.DataFrame({"time": ["2026-08-07 04:00:00"], "open": [1.0], "high": [1.0],
                          "low": [1.0], "close": [1.0], "volume": [1]})
    barcache.store("X", "D", 1, frame, "src")
    back, _, _ = barcache.load("X", "D", 1)
    assert isinstance(back["time"].iloc[0], str)


def test_an_expired_entry_is_not_served(tmp_path, monkeypatch):
    from dashboard import barcache
    monkeypatch.setattr(barcache, "CACHE_DIR", str(tmp_path))
    frame = pd.DataFrame({"time": ["2026-08-07 04:00:00"], "open": [1.0], "high": [1.0],
                          "low": [1.0], "close": [1.0], "volume": [1]})
    barcache.store("X", "D", 1, frame, "src")
    assert barcache.load("X", "D", 1, ttl=0) is None


def test_a_corrupt_entry_means_refetch_not_failure(tmp_path, monkeypatch):
    """A cache is an optimisation; it must never be able to fail a request."""
    from dashboard import barcache
    monkeypatch.setattr(barcache, "CACHE_DIR", str(tmp_path))
    frame = pd.DataFrame({"time": ["2026-08-07 04:00:00"], "open": [1.0], "high": [1.0],
                          "low": [1.0], "close": [1.0], "volume": [1]})
    barcache.store("X", "D", 1, frame, "src")
    data_path, meta_path = barcache._paths("X", "D", 1)
    open(data_path, "wb").write(b"not a dataframe")
    assert barcache.load("X", "D", 1) is None


def test_different_requests_do_not_collide(tmp_path, monkeypatch):
    from dashboard import barcache
    monkeypatch.setattr(barcache, "CACHE_DIR", str(tmp_path))
    a = barcache._paths("AAPL", "D", 300)[0]
    for other in (("AAPL", "D", 200), ("AAPL", "H1", 300), ("NVDA", "D", 300)):
        assert barcache._paths(*other)[0] != a


def test_a_disk_hit_still_passes_through_the_integrity_gate():
    """
    The cache skips the download, never the validation. A frame that has aged
    past its staleness tolerance on disk must be rejected exactly as a stale
    live one is -- otherwise the cache becomes a way to serve stale prices.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "dashboard", "webull_client.py"), encoding="utf-8").read()
    body = src[src.index("disk = barcache.load("):]
    body = body[:body.index("errors = []")]
    assert "_validate_frame(" in body, "a disk hit must be revalidated before it is returned"


# =====================================================================
# stdio transport purity
# =====================================================================

def test_nothing_the_server_imports_writes_to_stdout():
    """
    The MCP server speaks JSON-RPC over stdio. One stray print to stdout
    corrupts the stream for every client connected to it -- and the failure
    looks like a broken tool, not like a log line in the wrong place.
    """
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for name in ("webull_client.py", "broker.py", "alert_manager.py", "econ_calendar.py",
                 "edgar_forms.py", "earnings.py", "barcache.py", "indicators.py",
                 "options_math.py", "volume_profile.py", "central_banks.py",
                 "normalization.py", "market_calendar.py", "live_consent.py"):
        path = os.path.join(root, "dashboard", name)
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print"):
                continue
            targets = [k.arg for k in node.keywords]
            if "file" not in targets:
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, (
        "print() without file=sys.stderr writes to stdout and breaks the "
        f"JSON-RPC stream: {offenders}")


# =====================================================================
# Bars are stored in UTC and shown in exchange time
# =====================================================================

def _frame_at(stamp):
    return pd.DataFrame({"time": [stamp], "open": [1.0], "high": [1.0],
                         "low": [1.0], "close": [1.0], "volume": [1]})


def _bar_stamp(line):
    """The bar timestamp out of a freshness line, without the age or source."""
    return re.search(r"Latest \w+ bar: (.+?) \(", line).group(1)


def test_an_intraday_bar_is_shown_in_exchange_local_time():
    """
    Bars are stored naive-UTC. Friday's 15:45 ET closing bar was printed as
    "19:45" with no timezone marker, which reads as an after-hours print -- from
    a server that has no extended-hours data at all.
    """
    line = wc.freshness_line(_frame_at("2026-09-04 19:45:00"), "Webull OpenAPI", "M15")
    assert _bar_stamp(line) == "2026-09-04 15:45 EDT"


def test_the_display_clock_follows_dst_rather_than_a_fixed_offset():
    winter = wc.freshness_line(_frame_at("2026-01-15 21:00:00"), "Webull OpenAPI", "M15")
    assert _bar_stamp(winter) == "2026-01-15 16:00 EST"


def test_a_session_bar_is_shown_as_a_date_with_no_clock():
    """
    A daily bar is a session, not an instant. It is stamped 04:00 UTC (midnight
    ET) and printing a clock on it invites reading it as a 4 a.m. print.
    """
    for iv in ("D", "W", "M"):
        line = wc.freshness_line(_frame_at("2026-09-04 04:00:00"), "Webull OpenAPI", iv)
        assert _bar_stamp(line) == "2026-09-04", f"{iv} should carry no clock"


def test_an_unrecognised_interval_is_never_given_a_shifted_date():
    """
    whats_changed passes raw interval strings like "60". Converting an unknown
    interval can move the calendar day backward -- a bar stamped 00:00 UTC
    becomes the previous day at 20:00 ET -- so anything not known to be intraday
    renders as the bare stored date.
    """
    line = wc.freshness_line(_frame_at("2026-08-07 00:00:00"), "Webull OpenAPI", "60")
    assert _bar_stamp(line).startswith("2026-08-07"), \
        "an unknown interval must not shift the calendar day"


def test_a_bad_display_timezone_degrades_the_label_and_never_the_import(monkeypatch):
    """
    webull_client is imported by finance_mcp, the dashboard, the alert manager
    and both brokers. A display preference must never be able to take price
    fetching down with it.
    """
    monkeypatch.setenv("MARKET_DISPLAY_TZ", "Not/AZone")
    tz = wc._resolve_display_tz()
    assert tz is not None
    line = wc.freshness_line(_frame_at("2026-09-04 19:45:00"), "Webull OpenAPI", "M15")
    assert "2026-09-04" in line


def test_the_stored_time_column_stays_naive_utc():
    """
    The display change must not reach storage: staleness, the disk cache and the
    indicator maths all compare these strings as UTC.
    """
    # Built relative to the clock. A fixed date here passes on the day it is
    # written and then fails forever after, because the intraday staleness gate
    # measures in wall-clock hours -- the frame ages out and the test starts
    # reporting a storage bug that is really a calendar.
    newest = datetime.datetime.utcnow().replace(second=0, microsecond=0)
    earlier = newest - datetime.timedelta(minutes=15)
    eastern = datetime.timezone(datetime.timedelta(hours=-4))
    frame = pd.DataFrame({
        "time": pd.to_datetime([
            earlier.replace(tzinfo=datetime.timezone.utc).astimezone(eastern),
            newest.replace(tzinfo=datetime.timezone.utc).astimezone(eastern)]),
        "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
        "close": [1.0, 1.0], "volume": [1, 1]})

    out = wc._validate_frame(frame, "SPY", "M15", "test")

    assert str(out["time"].iloc[-1]) == newest.strftime("%Y-%m-%d %H:%M:%S"), \
        "storage must remain naive UTC even when the input carries a zone"


def test_an_intraday_bar_range_is_not_labelled_the_days_range():
    """
    The label was "Day's Range" unconditionally, so on an M15 request it
    reported a fifteen-minute high and low as the day's -- indexing one bar,
    with no aggregation over the session.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "finance_mcp.py"), encoding="utf-8").read()
    block = src[src.index("### Technical Analysis for"):][:600]
    assert "Day's Range" not in block, (
        "a single bar's high/low must not be labelled the day's range on an "
        "interval that is not a day")


# =====================================================================
# A submit is sent exactly once
# =====================================================================

class _Timeout(RuntimeError):
    """A transport failure whose text happens to carry the throttle token."""


def test_a_submit_is_never_retried(monkeypatch):
    """
    Webull's own SDK ships RetryableMethods: ["GET"] and its documentation says
    "Do no retry when it's not a GET request" -- place_order is a POST. The
    wrapper sat outside the SDK and reinstated retry for the one call the vendor
    had excluded, re-POSTing a byte-identical payload. Webull documents
    client_order_id as a client-side uniqueness obligation, never as an
    idempotency key, so nothing establishes the second POST would be rejected.
    """
    monkeypatch.setattr(wc, "WEBULL_RETRY_BACKOFF", 0.0)
    calls = []

    class Client:
        class order_v3:
            @staticmethod
            def place_order(account_id, orders):
                calls.append(orders)
                raise FakeRateLimitError()

    with pytest.raises(Exception):
        broker.place_order(Client(), "ACC1", {"client_order_id": "DRFT_x"})

    assert len(calls) == 1, f"a submit must be sent once, was sent {len(calls)}x"


def test_a_cancel_is_never_retried(monkeypatch):
    """Cancel is a POST too, and a duplicated cancel is a duplicated instruction."""
    monkeypatch.setattr(wc, "WEBULL_RETRY_BACKOFF", 0.0)
    calls = []

    class Client:
        class order_v3:
            @staticmethod
            def cancel_order(account_id, client_order_id):
                calls.append(client_order_id)
                raise FakeRateLimitError()

    with pytest.raises(Exception):
        broker.cancel_order(Client(), "ACC1", "DRFT_x")

    assert len(calls) == 1


def test_a_preview_still_retries(monkeypatch):
    """
    Preview is non-binding by design, so throttling it should still be ridden
    out. Removing retry from reads would be a different and worse change.
    """
    monkeypatch.setattr(wc, "WEBULL_RETRY_BACKOFF", 0.0)
    calls = []

    class Client:
        class order_v3:
            @staticmethod
            def preview_order(account_id, orders):
                calls.append(orders)
                if len(calls) < 2:
                    raise FakeRateLimitError()
                return {"data": {"ok": True}}

    broker.preview_order(Client(), "ACC1", {"client_order_id": "DRFT_x"})
    assert len(calls) == 2, "a preview should still ride out a throttle"


def test_a_transport_error_is_not_read_as_a_rate_limit():
    """
    The predicate ended in a substring match on the exception text, so a read
    timeout whose message merely carried the token retried -- which is exactly
    the failure where the order may already have reached the matching engine.
    """
    assert not wc._is_rate_limited(
        _Timeout("HTTPSConnectionPool: Read timed out. (TOO_MANY_REQUESTS?)"))


def test_structured_rate_limit_evidence_is_still_honoured():
    """The tightening must not stop reads riding out a real 429."""
    class ByStatus(RuntimeError):
        http_status = 429

    class ByCode(RuntimeError):
        error_code = "TOO_MANY_REQUESTS"

    assert wc._is_rate_limited(ByStatus())
    assert wc._is_rate_limited(ByCode())


# =====================================================================
# An unknown outcome is reported as unknown
# =====================================================================

def _submit_raising(exc):
    class Client:
        class order_v3:
            @staticmethod
            def place_order(account_id, orders):
                raise exc
    return Client()


@pytest.mark.parametrize("status,label", [(None, "transport failure"),
                                          (500, "server error"),
                                          (408, "request timeout")])
def test_an_unproven_failure_is_reported_as_ambiguous(monkeypatch, status, label):
    """
    A timeout, a reset and a 500 all leave the outcome unknown: the order may
    have reached the matching engine. Re-raising them bare let the dashboard
    print "the draft remains PENDING", which reads as "nothing was sent" -- a
    claim this process cannot support.
    """
    exc = RuntimeError(f"{label} while submitting")
    if status is not None:
        exc.http_status = status

    with pytest.raises(broker.AmbiguousSubmission) as caught:
        broker.place_order(_submit_raising(exc), "ACC1",
                           {"client_order_id": "DRFT_abc"})

    assert caught.value.client_order_id == "DRFT_abc", \
        "the id is the only handle for finding out what happened"


def test_a_broker_refusal_is_not_dressed_up_as_ambiguous():
    """
    A 4xx is the server having received, parsed and refused the order. Calling
    that ambiguous would be the opposite error -- it would send someone hunting
    the order book for an order that was never accepted.
    """
    exc = RuntimeError("BUYING_POWER_INSUFFICIENT")
    exc.http_status = 400

    with pytest.raises(RuntimeError) as caught:
        broker.place_order(_submit_raising(exc), "ACC1",
                           {"client_order_id": "DRFT_abc"})

    assert not isinstance(caught.value, broker.AmbiguousSubmission)


def test_the_dashboard_does_not_claim_nothing_was_sent_when_it_cannot_know():
    """
    The guard above is only worth having if the approval page reads it. The
    error text after an ambiguous submit must not assert the order was not
    placed, and must point at the order book.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = open(os.path.join(root, "dashboard", "app.py"), encoding="utf-8").read()
    assert "AmbiguousSubmission" in app, \
        "the approval page must distinguish an unknown outcome from a refusal"
    start = app.index("except broker.AmbiguousSubmission")
    block = app[start:app.index("except Exception", start)]
    assert "order book" in block.lower()
    assert "remains PENDING" not in block, \
        "an unknown outcome must not be reported as a draft that was never sent"


def test_a_pending_draft_can_be_cancelled_from_the_dashboard():
    """
    There was no way to clear a draft except by editing the JSON by hand. That
    absence is the missing remedy behind several refusals -- an unpriceable
    pending draft, a draft raised against the wrong desk, one whose outcome is
    unknown -- each of which tells the operator to deal with the draft and gave
    them nothing to deal with it with.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = open(os.path.join(root, "dashboard", "app.py"), encoding="utf-8").read()
    execution = app[app.index("PENDING_APPROVAL"):]
    assert "CANCELLED" in execution, "the execution page needs a cancel control"
    assert "cancel_draft" in execution


def test_a_cancelled_draft_stops_counting_against_buying_power():
    """A cancelled draft has to actually free the commitment it was holding."""
    import finance_mcp as srv
    queue = [{"draft_id": "D1", "symbol": "AAA", "action": "BUY", "quantity": 100,
              "limit_price": 2.0, "est_notional": 200.0, "status": "CANCELLED"}]
    cost, sells, unpriced = srv._pending_commitments(queue, lambda s: None)
    assert cost == 0.0 and not sells and not unpriced


def test_bar_age_says_what_unit_it_counted_in():
    """`behind` is sessions for daily and slower, hours otherwise. A number
    whose unit depends on an argument has to carry it."""
    daily = wc.bar_age(_frame_at("2026-09-04 04:00:00"), "D")
    intraday = wc.bar_age(_frame_at("2026-09-04 19:45:00"), "M15")
    broken = wc.bar_age(pd.DataFrame({"time": []}), "D")
    assert daily["unit"] == "sessions"
    assert intraday["unit"] == "hours"
    assert "unit" in broken, "even the unreadable case needs a unit"


def test_a_mixed_unit_sweep_does_not_rank_hours_against_sessions():
    """
    freshness_summary picks the worst by `behind`, which is sessions for a daily
    bar and hours for an intraday one. Mixed, it compared 2 sessions against
    3 hours and called the 3 worse -- so the header quoted the fresher of the
    two as the stalest.
    """
    daily = {"bar": 1, "as_of": "2026-09-04", "age": "2 sessions",
             "behind": 2.0, "current": False, "unit": "sessions"}
    intraday = {"bar": 1, "as_of": "2026-09-08 15:45 EDT", "age": "3.0h",
                "behind": 3.0, "current": False, "unit": "hours"}

    line = wc.freshness_summary([daily, intraday], "D", "series")

    assert "2 sessions" in line and "3.0h" in line, (
        "with two units in play both worsts have to be stated, not ranked "
        f"against each other: {line}")


def test_the_bar_table_dates_a_session_the_same_way_the_header_does():
    """
    The header was fixed to render a daily bar as a bare date; the table beside
    it still printed "2026-09-11 04:00:00" for the same bar. One response, two
    renderings, and the one left behind is the midnight-ET-as-UTC form that was
    removed from the header precisely because it reads as a 4 a.m. print.
    """
    frame = pd.DataFrame({
        "time": ["2026-09-10 04:00:00", "2026-09-11 04:00:00"],
        "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
        "close": [1.0, 1.0], "volume": [1, 1]})

    shown = wc.display_frame(frame, "D")

    assert list(shown["time"]) == ["2026-09-10", "2026-09-11"]


def test_an_intraday_table_keeps_its_clock_and_says_the_zone():
    frame = pd.DataFrame({
        "time": ["2026-09-11 19:45:00"],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]})

    shown = wc.display_frame(frame, "M15")

    assert shown["time"].iloc[0] == "2026-09-11 15:45 EDT"


def test_displaying_a_frame_does_not_mutate_the_stored_one():
    """Storage stays naive UTC; this is a view, not a conversion."""
    frame = pd.DataFrame({
        "time": ["2026-09-11 04:00:00"],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]})

    wc.display_frame(frame, "D")

    assert frame["time"].iloc[0] == "2026-09-11 04:00:00"
