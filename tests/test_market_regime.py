"""
What the regime label actually means.

"Mixed Trend" used to be returned for three unrelated situations: the warm-up
before ADX and Bollinger width exist, a real trend whose direction the EMA stack
disputed, and the 20-23 ADX gap between the trending and ranging thresholds. On
250 bars of real MU data that was 28% of the series under one word, and one of
the three was not a reading at all.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import indicators as ind


def _series(closes):
    close = pd.Series([float(c) for c in closes])
    return pd.DataFrame({"open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close,
                         "volume": pd.Series([1e6] * len(close))})


def test_the_warmup_is_not_reported_as_a_reading():
    """
    Absence must never look like a measurement. The opening bars of any series
    have no ADX and no Bollinger width, and the old classifier called them
    "Mixed Trend" — indistinguishable in the output from a genuine finding.
    """
    df = _series(np.linspace(100, 200, 120))
    regimes = ind.classify_market_regime(df)["regime"]
    assert regimes.iloc[0] == ind.REGIME_UNKNOWN
    assert "not enough bars" in ind.describe_regime(ind.REGIME_UNKNOWN)
    # And it ends: a series this long must resolve to something.
    assert regimes.iloc[-1] != ind.REGIME_UNKNOWN


def test_a_trend_the_emas_dispute_is_not_the_same_as_no_trend():
    """
    ADX above the threshold says a trend exists. Price sitting the wrong side
    of an EMA stack says the direction is unclear. That is a different state
    from "ADX says there is no trend", and they were the same word.
    """
    assert ind.REGIME_CONFLICTED != ind.REGIME_TRANSITIONAL
    assert ind.REGIME_CONFLICTED != ind.REGIME_RANGING
    assert "disagree" in ind.describe_regime(ind.REGIME_CONFLICTED)
    assert str(ind.ADX_TRENDING) in ind.describe_regime(ind.REGIME_CONFLICTED)


def test_the_gap_between_the_thresholds_is_named():
    """
    ADX between 20 and 23 is neither trending nor ranging by this tree's own
    thresholds. That is a real state and it deserves its own word rather than
    being folded into a residual bucket.
    """
    assert ind.ADX_RANGING < ind.ADX_TRENDING, "the gap is what Transitional covers"
    basis = ind.describe_regime(ind.REGIME_TRANSITIONAL)
    assert str(ind.ADX_RANGING) in basis and str(ind.ADX_TRENDING) in basis


def test_every_label_can_say_which_test_produced_it():
    """
    A label a reader cannot check is a label they have to trust. Each one
    reports the comparison the classifier actually made.
    """
    labels = [ind.REGIME_BULLISH, ind.REGIME_BEARISH, ind.REGIME_EXPANSION,
              ind.REGIME_RANGING, ind.REGIME_CONFLICTED, ind.REGIME_TRANSITIONAL,
              ind.REGIME_UNKNOWN]
    assert len(set(labels)) == len(labels), "two labels share a string"
    for label in labels:
        basis = ind.describe_regime(label)
        assert basis and basis != "unrecognised regime label", label
    assert ind.describe_regime("Mixed Trend") == "unrecognised regime label", (
        "the old catch-all must not quietly resolve to something")


def test_a_clean_uptrend_is_read_as_one():
    """The tree has to work, not merely be well labelled."""
    df = _series(np.linspace(100, 300, 200))
    regimes = ind.classify_market_regime(df)["regime"]
    resolved = regimes[regimes != ind.REGIME_UNKNOWN]
    assert (resolved == ind.REGIME_BULLISH).mean() > 0.7, resolved.value_counts()


def test_a_flat_series_is_read_as_range_bound():
    rng = np.random.RandomState(7)
    df = _series(100 + rng.normal(0, 0.05, 200))
    regimes = ind.classify_market_regime(df)["regime"]
    resolved = regimes[regimes != ind.REGIME_UNKNOWN]
    assert (resolved == ind.REGIME_RANGING).mean() > 0.5, resolved.value_counts()


def test_the_consensus_is_absent_where_the_regime_is_unknown():
    """
    A 50/50 blend of two undefined scores is still a number, and a number here
    reads as a signal. The backtester trades the whole series, not just the
    last bar.
    """
    df = _series(np.linspace(100, 200, 120))
    regimes = ind.classify_market_regime(df)["regime"]
    consensus = ind.calculate_adaptive_consensus(df)
    unknown = regimes == ind.REGIME_UNKNOWN
    assert unknown.any()
    assert consensus[unknown].isna().all(), (
        "scored a bar whose regime could not be determined")


def test_the_expansion_test_waits_for_its_own_warmup():
    """
    The expansion check compares Bollinger width against a rolling mean of
    itself — a window over a window. Before that second window fills, the
    comparison is against NaN, which is False, so those bars fell through to
    the trend branch and were labelled from ADX alone.
    """
    df = _series(np.linspace(100, 200, 60))
    regimes = ind.classify_market_regime(df)["regime"]
    bb = ind.calculate_bollinger_bands(df)["bb_width"]
    unresolvable = bb.rolling(20).std().isna()
    assert (regimes[unresolvable] == ind.REGIME_UNKNOWN).all()
