"""
Timeframe picker and OHLCV resampling.

Webull and Yahoo both stop at 1H and 1D. 4H and 1Y are built here, and an
aggregate that averages the wrong field invents a bar that never traded -- so
these pin the aggregation rules and the labelling.
"""
import ast
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = open(os.path.join(ROOT, "dashboard", "app.py"), encoding="utf-8").read()


def _load_from_app(*names):
    """
    Pull definitions out of app.py without importing it -- importing runs the
    whole Streamlit script, which needs a broker session and a network.
    """
    tree = ast.parse(APP)
    ns = {"pd": pd}
    wanted = set(names)
    for node in tree.body:
        target = None
        if isinstance(node, ast.FunctionDef):
            target = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target in wanted:
            exec(compile(ast.Module([node], []), "<app>", "exec"), ns)
    missing = wanted - set(ns)
    assert not missing, f"app.py no longer defines {missing}"
    return [ns[n] for n in names]


TIMEFRAMES, INTERVAL_NAMES, resample = _load_from_app(
    "TIMEFRAMES", "INTERVAL_NAMES", "_resample_ohlcv")


def hourly(n=8, start="2026-08-03 09:00:00"):
    times = pd.date_range(start, periods=n, freq="1h")
    return pd.DataFrame({
        "time": times.strftime("%Y-%m-%d %H:%M:%S"),
        "open":  [10 + i for i in range(n)],
        "high":  [12 + i for i in range(n)],
        "low":   [8 + i for i in range(n)],
        "close": [11 + i for i in range(n)],
        "volume": [100] * n,
    })


# =====================================================================
# The picker itself
# =====================================================================

def test_every_offered_timeframe_maps_to_a_real_feed_interval():
    """Offering a bar size the feed cannot serve returns something else silently."""
    from dashboard import webull_client as wc
    for key, tf in TIMEFRAMES.items():
        assert tf["interval"] in wc.INTERVAL_WEBULL_TO_YF, \
            f"{key} maps to unknown interval {tf['interval']}"
        assert tf["label"] and tf["bars"] > 0


def test_the_frames_the_feed_serves_natively_are_not_resampled():
    for key in ("1m", "5m", "15m", "30m", "1H", "1D", "1W", "1M"):
        assert TIMEFRAMES[key]["resample"] is None, f"{key} should be native"


def test_only_4h_and_1y_are_resampled():
    """Neither Webull nor Yahoo serves a 4-hour or annual bar."""
    resampled = {k for k, v in TIMEFRAMES.items() if v["resample"]}
    assert resampled == {"4H", "1Y"}
    assert TIMEFRAMES["4H"]["interval"] == "H1"
    assert TIMEFRAMES["1Y"]["interval"] == "M"


def test_a_resampled_frame_asks_for_enough_source_bars():
    """1Y from monthly needs decades of months to produce a usable count."""
    assert TIMEFRAMES["1Y"]["bars"] >= 240, "1Y would render only a handful of bars"


def test_every_interval_has_a_human_name():
    for tf in TIMEFRAMES.values():
        assert tf["interval"] in INTERVAL_NAMES


# =====================================================================
# OHLCV aggregation
# =====================================================================

def test_ohlcv_aggregates_by_the_right_rule():
    """
    Open is the first, close the last, high and low the extremes, volume the
    sum. Averaging any of them invents a bar that never traded.
    """
    # Start on a 4h boundary so the buckets are 08:00-11:59 and 12:00-15:59.
    # Pandas aligns bins to the clock, not to the first row -- which is correct,
    # and is why a 09:00 start splits into three groups rather than two.
    df = hourly(8, start="2026-08-03 08:00:00")
    out = resample(df, "4h")
    assert len(out) == 2
    first = out.iloc[0]
    assert first["open"] == 10          # first open of the group
    assert first["close"] == 14         # last close of the group
    assert first["high"] == 15          # max high
    assert first["low"] == 8            # min low
    assert first["volume"] == 400       # sum, not mean


def test_a_resampled_bar_is_labelled_at_its_start():
    df = hourly(8, start="2026-08-03 08:00:00")
    out = resample(df, "4h")
    assert out.iloc[0]["time"] == "2026-08-03 08:00:00"


def test_the_annual_bar_is_not_stamped_with_a_future_date():
    """
    Resampling with 'YE' labels the 2026 bar 2026-12-31 -- a date that has not
    happened. Every other bar in the feed is labelled at its start, and a bar
    dated in the future reads as bad data.
    """
    months = pd.date_range("2025-01-01", periods=20, freq="MS")
    df = pd.DataFrame({
        "time": months.strftime("%Y-%m-%d %H:%M:%S"),
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10,
    })
    out = resample(df, TIMEFRAMES["1Y"]["resample"])
    assert out.iloc[0]["time"].startswith("2025-01-01")
    assert out.iloc[-1]["time"].startswith("2026-01-01")


def test_empty_groups_are_dropped_rather_than_emitted_as_nan_bars():
    """
    Resampling across a weekend spans dozens of empty 4-hour buckets. Emitting
    them would put priceless bars on the chart between Friday and Monday.
    """
    friday = hourly(4, start="2026-08-07 08:00:00")
    monday = hourly(4, start="2026-08-10 08:00:00")
    out = resample(pd.concat([friday, monday], ignore_index=True), "4h")

    assert not out[["open", "high", "low", "close"]].isna().any().any()
    # Two sessions in, two sessions out -- nothing invented in the gap.
    assert len(out) == 2
    assert out.iloc[0]["time"].startswith("2026-08-07")
    assert out.iloc[1]["time"].startswith("2026-08-10")


def test_resampling_preserves_the_frame_contract():
    """Downstream code and the validator both require these exact columns."""
    out = resample(hourly(8), "4h")
    assert list(out.columns) == ["time", "open", "high", "low", "close", "volume"]
    assert out["time"].dtype == object          # formatted strings, as elsewhere
    assert out["time"].is_monotonic_increasing


def test_resampling_never_produces_a_bar_violating_low_close_high():
    out = resample(hourly(24), "4h")
    assert (out["low"] <= out["close"]).all()
    assert (out["close"] <= out["high"]).all()
    assert (out["low"] <= out["open"]).all()


def test_the_resample_is_announced_in_the_source_label():
    """A 57-bar chart built from 200 hourly bars must say so, or the bar count
    in the masthead looks like a failed fetch."""
    assert "resampled from" in APP
    assert 'source +=' in APP


# =====================================================================
# Wiring
# =====================================================================

def test_the_picker_is_bound_to_a_session_key_with_a_valid_default():
    assert 'st.session_state.setdefault("ui_timeframe", "1D")' in APP
    assert 'key="ui_timeframe"' in APP
    assert "1D" in TIMEFRAMES


def test_a_stale_timeframe_in_session_state_cannot_crash_the_page():
    """Session state survives a code change that renames a timeframe."""
    assert 'if st.session_state["ui_timeframe"] not in TIMEFRAMES' in APP
