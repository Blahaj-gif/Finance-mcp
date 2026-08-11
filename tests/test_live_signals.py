"""
The three readings the indicator strip could not make.

Each one exists because a number was displayed without the reference needed to
read it: a raw share count with no average beside it, a regime word with no ADX
under it, and a volume profile computed for its own tab and never consulted by
the live panel.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import indicators, live_signals


def _frame(closes, volumes=None):
    close = pd.Series([float(c) for c in closes])
    if volumes is None:
        volumes = [1e6] * len(close)
    return pd.DataFrame({"open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close,
                         "volume": pd.Series([float(v) for v in volumes])})


# =====================================================================
# Volume confirmation
# =====================================================================

def test_the_average_excludes_the_bar_being_compared_against_it():
    """
    Including the latest bar drags the mean toward the value under test, which
    understates exactly the outliers this reading exists to catch. A 10x bar in
    a 20-bar window would report ~7x if it were counted in its own average.
    """
    volumes = [1_000_000] * 20 + [10_000_000]
    result = live_signals.volume_confirmation(_frame(range(100, 121), volumes))
    assert result["average"] == pytest.approx(1_000_000)
    assert result["ratio"] == pytest.approx(10.0)
    assert result["verdict"] == "heavy"


def test_a_quiet_bar_is_called_light_and_a_normal_one_typical():
    base = [1_000_000] * 20
    light = live_signals.volume_confirmation(_frame(range(100, 121), base + [400_000]))
    typical = live_signals.volume_confirmation(_frame(range(100, 121), base + [1_050_000]))
    assert light["verdict"] == "light"
    assert typical["verdict"] == "typical"


def test_volume_says_it_cannot_compare_rather_than_guessing():
    """Too few bars is not 'typical volume'."""
    result = live_signals.volume_confirmation(_frame(range(100, 105)))
    assert result["verdict"] == "unknown"
    assert result["ratio"] is None
    assert "needs" in result["basis"]


def test_an_untraded_symbol_does_not_divide_by_zero():
    result = live_signals.volume_confirmation(_frame(range(100, 130), [0] * 30))
    assert result["verdict"] == "unknown"
    assert "no traded volume" in result["basis"]


# =====================================================================
# Trend strength
# =====================================================================

def test_the_adx_behind_the_regime_label_is_reported():
    """
    The strip showed a regime word with "classifier" underneath. The number
    that decided it was invisible, which is how one label covered the warm-up,
    a disputed trend and the threshold gap without anyone noticing.
    """
    result = live_signals.trend_strength(_frame(np.linspace(100, 300, 200)))
    assert result["adx"] is not None
    assert f"{result['adx']:.1f}" in result["basis"]
    assert result["regime"] in indicators.REGIME_BASIS


def test_trend_strength_verdicts_match_the_classifier_thresholds():
    """
    Two places deciding what "trending" means is two places to disagree. These
    read the same constants the regime tree turns on.
    """
    frame = _frame(np.linspace(100, 300, 200))
    result = live_signals.trend_strength(frame)
    adx = result["adx"]
    if adx > indicators.ADX_TRENDING:
        assert result["verdict"] == "trending"
    elif adx < indicators.ADX_RANGING:
        assert result["verdict"] == "ranging"
    else:
        assert result["verdict"] == "transitional"


def test_trend_strength_is_unknown_before_adx_exists():
    result = live_signals.trend_strength(_frame(range(100, 110)))
    assert result["adx"] is None
    assert result["verdict"] == "unknown"


# =====================================================================
# Auction position
# =====================================================================

def test_the_profile_window_is_clamped_to_the_bars_available():
    """
    Asking for a 100-bar profile of a 60-bar frame returned no nodes, so the
    panel reported "unknown" on a window that had a perfectly good profile in
    it -- an answer withheld for a reason that was not the reader's.
    """
    rng = np.random.RandomState(3)
    frame = _frame(100 + rng.normal(0, 2, 60))
    result = live_signals.auction_position(frame, lookback=100)
    assert result["node"] is not None, result["basis"]
    assert result["window"] == 60
    assert "60 bars" in result["basis"]


def test_price_at_a_node_is_distinguished_from_price_near_one():
    """
    A node is a price region the auction spent time in, not a line, so "at" has
    to be a band rather than an equality.
    """
    rng = np.random.RandomState(11)
    frame = _frame(100 + rng.normal(0, 1, 150))
    result = live_signals.auction_position(frame)
    assert result["verdict"] in ("at a node", "above the nearest node",
                                 "below the nearest node")
    if abs(result["distance_pct"]) <= live_signals.NODE_AT_PCT:
        assert result["verdict"] == "at a node"


def test_auction_reports_the_distance_it_measured():
    rng = np.random.RandomState(5)
    frame = _frame(100 + rng.normal(0, 3, 150))
    result = live_signals.auction_position(frame)
    assert result["distance_pct"] is not None
    assert f"{abs(result['distance_pct']):.2f}%" in result["basis"]


# =====================================================================
# The panel
# =====================================================================

def test_every_line_carries_the_basis_for_its_verdict():
    """
    "Volume heavy" with no reference could be 1.6x or 16x, and the difference
    changes what the sentence means. A reading a person cannot check is one
    they have to trust.
    """
    rng = np.random.RandomState(2)
    frame = _frame(100 + rng.normal(0, 2, 200))
    lines = live_signals.signal_lines(frame)
    assert len(lines) == 3
    for line in lines:
        assert line.startswith("- **")
        assert "—" in line or "-" in line
        assert len(line) > 40, f"line carries no basis: {line}"


def test_the_panel_never_raises_on_a_frame_too_short_to_read():
    """It is called on whatever the user asked for, including three bars."""
    for length in (1, 2, 5, 25):
        lines = live_signals.signal_lines(_frame(range(100, 100 + length)))
        assert len(lines) == 3


def test_the_dashboard_and_the_mcp_tool_read_from_the_same_helper():
    """
    Two surfaces computing the same panel separately is how they come to
    disagree about one bar.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = open(os.path.join(root, "dashboard", "app.py"), encoding="utf-8").read()
    server = open(os.path.join(root, "finance_mcp.py"), encoding="utf-8").read()
    assert "live_signals.live_signals(" in app
    assert "live_signals.signal_lines(" in server


def test_the_new_readings_are_not_scored_into_the_heuristic_verdict():
    """
    They are context, not another vote in a score that already underperformed
    buy-and-hold in backtest. Adding weight to it would be a change of claim.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server = open(os.path.join(root, "finance_mcp.py"), encoding="utf-8").read()
    start = server.index("signals.extend(live_signals.signal_lines(res))")
    # Up to where the verdict is *computed* from the score -- reading it there
    # is the point; adding to it on the way is what must not happen.
    window = server[start:server.index("# Determine Verdict Text", start)]
    assert not re.search(r"verdict_score\s*[-+]=", window), (
        "the new readings must not feed the BUY/SELL score")


def test_a_stale_indicators_module_fails_at_import_not_mid_render():
    """
    A Streamlit app left open across an upgrade keeps `dashboard.indicators` in
    sys.modules from before these constants existed. Reading them off the module
    at call time then failed with

        AttributeError: module 'dashboard.indicators' has no attribute 'ADX_TRENDING'

    three hundred lines into a page render, pointing at a line that was not
    wrong. Binding at import turns that into one legible error before anything
    is drawn.
    """
    import importlib
    import sys
    import types

    stale = types.ModuleType("dashboard.indicators")
    stale.calculate_adx = lambda *a, **k: None
    stale.classify_market_regime = lambda *a, **k: None
    # No ADX_TRENDING, ADX_RANGING or describe_regime -- the version that shipped
    # before the regime split.

    import dashboard as dashboard_pkg

    real_indicators = sys.modules.get("dashboard.indicators")
    real_signals = sys.modules.pop("dashboard.live_signals", None)
    sys.modules["dashboard.indicators"] = stale
    # `from dashboard import indicators` resolves the attribute on the package
    # object before it consults sys.modules, so patching only sys.modules leaves
    # the real module reachable and the test proves nothing.
    dashboard_pkg.indicators = stale
    try:
        with pytest.raises(ImportError) as raised:
            importlib.import_module("dashboard.live_signals")
        message = str(raised.value)
        assert "ADX_TRENDING" in message
        assert "Restart" in message, "the error must say what to do about it"
    finally:
        if real_indicators is not None:
            sys.modules["dashboard.indicators"] = real_indicators
            dashboard_pkg.indicators = real_indicators
        else:
            sys.modules.pop("dashboard.indicators", None)
        sys.modules.pop("dashboard.live_signals", None)
        if real_signals is not None:
            sys.modules["dashboard.live_signals"] = real_signals
        importlib.import_module("dashboard.live_signals")


def test_the_thresholds_are_not_duplicated_from_the_classifier():
    """
    Bound by name from indicators rather than restated. Two copies of the number
    that decides "trending" is two numbers to disagree.
    """
    assert live_signals.ADX_TRENDING is indicators.ADX_TRENDING
    assert live_signals.ADX_RANGING is indicators.ADX_RANGING
