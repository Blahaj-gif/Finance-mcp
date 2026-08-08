"""
Backtester correctness tests.

A backtest reports numbers people size positions from, so these pin the
arithmetic against series whose answer is known in advance.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import backtester as bt

N = 100
COMPOUNDING = 100 * (1.01 ** np.arange(N))   # exactly +1% per bar


def series(scores, close=None):
    close = COMPOUNDING if close is None else close
    return pd.DataFrame({
        "time": [f"d{i}" for i in range(len(close))],
        "close": close,
        "consensus_score": scores,
    })


# =====================================================================
# Trade accounting
# =====================================================================

def test_a_strategy_that_never_trades_reports_zero_trades():
    """`max(1, total_trades)` reported a trade for a strategy that never traded."""
    m = bt.run_backtest(series([0.0] * N))["metrics"]
    assert m["total_trades"] == 0
    assert m["closed_trades"] == 0
    assert m["exposure_pct"] == 0.0
    assert m["total_strategy_return"] == pytest.approx(0.0)


def test_an_unclosed_final_trade_is_still_counted():
    """
    Enter, exit, enter again and end holding: two trades, one closed. The old
    `int(changes / 2)` truncated the open one away while the win-rate loop
    still counted it, so the two disagreed.
    """
    m = bt.run_backtest(series([5.0] * 20 + [-5.0] * 20 + [5.0] * 60))["metrics"]
    assert m["total_trades"] == 2
    assert m["closed_trades"] == 1
    assert m["open_trade"] is True


def test_trade_count_and_win_rate_share_a_denominator():
    r = bt.run_backtest(series([5.0] * 30 + [-5.0] * 10 + [5.0] * 40 + [-5.0] * 20))
    m = r["metrics"]
    assert len(r["trades"]) == m["total_trades"]
    wins = sum(1 for t in r["trades"] if t["ret"] > 0)
    assert m["win_rate"] == pytest.approx(wins / m["total_trades"] * 100)


# =====================================================================
# Returns
# =====================================================================

def test_always_long_equals_buy_and_hold_without_fees():
    m = bt.run_backtest(series([5.0] * N), transaction_fee=0.0)["metrics"]
    assert m["total_strategy_return"] == pytest.approx(m["total_asset_return"])
    assert m["exposure_pct"] == pytest.approx(99.0)   # bar 0 is unavoidably flat


def test_fees_reduce_the_return():
    free = bt.run_backtest(series([5.0] * 30 + [-5.0] * 30 + [5.0] * 40), transaction_fee=0.0)
    paid = bt.run_backtest(series([5.0] * 30 + [-5.0] * 30 + [5.0] * 40), transaction_fee=0.01)
    assert paid["metrics"]["total_strategy_return"] < free["metrics"]["total_strategy_return"]


def test_staying_flat_through_a_crash_beats_holding():
    crash = np.concatenate([100 * (1.01 ** np.arange(50)), 100 * (0.95 ** np.arange(50))])
    m = bt.run_backtest(series([5.0] * 50 + [-5.0] * 50, close=crash))["metrics"]
    assert m["total_strategy_return"] > m["total_asset_return"]
    assert m["max_drawdown"] > m["asset_max_drawdown"], "flat through the crash = shallower drawdown"


def test_no_lookahead_a_signal_cannot_use_its_own_bar_return():
    """
    Give a perfect oracle signal only on the single bar that jumps. If the
    engine peeked, it would capture the jump; with a one-bar execution lag it
    cannot.
    """
    close = np.concatenate([np.full(50, 100.0), np.full(50, 200.0)])
    scores = [0.0] * 100
    scores[50] = 5.0      # signal fires on the bar that already jumped
    m = bt.run_backtest(series(scores, close=close), transaction_fee=0.0)["metrics"]
    assert m["total_strategy_return"] == pytest.approx(0.0), "captured a move it could not have traded"


# =====================================================================
# Sharpe annualisation
# =====================================================================

def test_sharpe_scales_with_the_bar_interval():
    """A fixed 252 understated an H1 Sharpe by ~2.5x and an M15 Sharpe by ~5x."""
    rng = np.random.RandomState(1)
    px = 100 * (1 + pd.Series(rng.randn(500) * 0.01 + 0.002)).cumprod()
    d = series([5.0] * 500, close=px.to_numpy())

    daily = bt.run_backtest(d, interval="D")["metrics"]["sharpe_ratio"]
    hourly = bt.run_backtest(d, interval="H1")["metrics"]["sharpe_ratio"]
    m15 = bt.run_backtest(d, interval="M15")["metrics"]["sharpe_ratio"]

    assert hourly > daily and m15 > hourly
    assert hourly / daily == pytest.approx(np.sqrt(6.5), rel=0.01)
    assert m15 / daily == pytest.approx(np.sqrt(26), rel=0.01)


def test_unknown_interval_falls_back_to_daily():
    d = series([5.0] * N)
    assert (bt.run_backtest(d, interval="WEIRD")["metrics"]["sharpe_ratio"]
            == pytest.approx(bt.run_backtest(d, interval="D")["metrics"]["sharpe_ratio"]))


def test_zero_variance_returns_do_not_divide_by_zero():
    """Flat price and no fees means every return is exactly 0 — std is 0."""
    flat = np.full(N, 100.0)
    m = bt.run_backtest(series([5.0] * N, close=flat), transaction_fee=0.0)["metrics"]
    assert m["sharpe_ratio"] == 0.0


def test_a_fee_on_a_flat_market_shows_up_as_a_loss():
    flat = np.full(N, 100.0)
    m = bt.run_backtest(series([5.0] * N, close=flat), transaction_fee=0.01)["metrics"]
    assert m["total_strategy_return"] < 0, "paying to trade a flat market must lose money"


# =====================================================================
# Warm-up and guards
# =====================================================================

def test_nan_scores_are_not_signals():
    """During indicator warm-up the score is NaN; the strategy must stay flat."""
    m = bt.run_backtest(series([float("nan")] * 50 + [5.0] * 50))["metrics"]
    assert m["total_trades"] == 1
    assert m["exposure_pct"] < 55


def test_rejects_a_missing_score_column():
    with pytest.raises(KeyError, match="consensus_score"):
        bt.run_backtest(pd.DataFrame({"time": ["a", "b"], "close": [1.0, 2.0]}))


def test_rejects_too_few_bars():
    with pytest.raises(ValueError, match="at least 2 bars"):
        bt.run_backtest(pd.DataFrame({"time": ["a"], "close": [1.0], "consensus_score": [0.0]}))


def test_hysteresis_holds_between_thresholds():
    """A score between the two triggers must not change the position."""
    scores = [5.0] * 10 + [0.0] * 80 + [-5.0] * 10
    r = bt.run_backtest(series(scores))
    held = r["df"]["position"].iloc[10:88]
    assert (held == 1).all(), "position should persist while the score sits between triggers"
