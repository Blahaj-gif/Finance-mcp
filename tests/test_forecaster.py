"""
The AR forecast overlay, which the chart labels "95% CI".

This was the only logic module in the project with no tests, and it shipped a
confidence band computed as sqrt(k) * sigma -- the random-walk envelope, which
ignores the AR coefficients the model just fitted. On a genuinely autocorrelated
series that understated the spread by 2.8x at fifteen steps, in the direction
that makes a position look safer than it is.

The reference here is deliberately not a second copy of the implementation's
arithmetic. The headline test simulates forward from the fitted parameters and
compares the band against the empirical spread of those paths, so an error
reproduced in both places cannot pass.
"""
import datetime
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import forecaster
from dashboard import market_calendar

Z95 = 1.959963984540054
Z68 = 1.0


def _ar1_frame(phi, n=600, sigma=0.01, seed=7, start="2024-01-01", freq="D"):
    """A price series whose log returns are AR(1) with a known coefficient."""
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, sigma, n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + eps[i]
    close = 100.0 * np.exp(np.cumsum(r))
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq=freq),
        "close": close,
    })


def _implied_sd(out, k, z=Z95, side="upper"):
    """
    Back out the standard deviation the band was drawn with.

    The bands are quantiles of a lognormal around the median path, so
    upper = median * exp(z * sd) and the sd is recoverable without the module
    having to expose it.
    """
    key = f"{side}_{'95' if z == Z95 else '68'}"
    ratio = out[key][k - 1] / out["forecast_price"][k - 1]
    return abs(np.log(ratio)) / z


def _fit_ar(close, order):
    """The same OLS the module runs, used only to seed the simulation."""
    lr = np.log(close[1:] / close[:-1])
    n = len(lr)
    Y = lr[order:]
    X = np.ones((n - order, order + 1))
    for i in range(order):
        X[:, i + 1] = lr[order - 1 - i: n - 1 - i]
    beta = np.linalg.solve(X.T @ X, X.T @ Y)
    resid = Y - X @ beta
    # Residual standard error, not the plain deviation: order + 1 parameters
    # were estimated from these observations.
    sigma = float(np.sqrt(resid @ resid / (len(Y) - (order + 1))))
    return beta, sigma, lr


def _simulated_sd(close, order, steps, paths=120_000, seed=11):
    """
    Empirical standard deviation of the cumulative log return, by simulation.

    Independent of how the module computes its envelope: it draws shocks,
    propagates them through the fitted recursion, and measures the spread of
    where the paths land.
    """
    beta, sigma, lr = _fit_ar(close, order)
    rng = np.random.default_rng(seed)
    lags = np.tile(lr[-order:][::-1], (paths, 1))      # most recent first
    total = np.zeros(paths)
    out = []
    for _ in range(steps):
        pred = beta[0] + lags @ beta[1:]
        draw = pred + rng.normal(0.0, sigma, paths)
        total = total + draw
        out.append(total.std(ddof=1))
        lags = np.column_stack([draw, lags[:, :-1]])
    return out, sigma


# =====================================================================
# The band itself
# =====================================================================

def test_the_band_matches_the_spread_of_simulated_paths():
    """
    The headline. With log returns that are genuinely autocorrelated, the shock
    at step one is still moving the series at step fifteen, and the cumulative
    spread grows faster than sqrt(k).

    sqrt(k) * sigma is the spread only when the fitted coefficients are zero.
    """
    steps, order = 15, 1
    df = _ar1_frame(0.7)
    out = forecaster.run_ar_forecast(df, forecast_steps=steps, ar_order=order)

    simulated, sigma = _simulated_sd(df["close"].values, order, steps)

    for k in (1, 3, 5, 10, 15):
        got = _implied_sd(out, k)
        want = simulated[k - 1]
        assert got == pytest.approx(want, rel=0.03), (
            f"step {k}: band drawn with sd {got:.5f}, simulated paths spread "
            f"{want:.5f}")

    # And state the bug this replaces, so a regression to it is named rather
    # than merely failing a tolerance.
    naive = np.sqrt(steps) * sigma
    assert simulated[steps - 1] > 2 * naive, (
        "fixture is too weakly autocorrelated to distinguish the two formulas")
    assert _implied_sd(out, steps) > 2 * naive, (
        "the band is back to the random-walk envelope sqrt(k)*sigma, which "
        "ignores the fitted AR coefficients")


def test_the_band_reduces_to_the_random_walk_when_nothing_is_autocorrelated():
    """
    The other side of the same claim: with no AR structure the correct envelope
    *is* sqrt(k) * sigma, and the fix must not widen bands that were right.
    """
    steps, order = 12, 1
    df = _ar1_frame(0.0, seed=23)
    out = forecaster.run_ar_forecast(df, forecast_steps=steps, ar_order=order)

    _, sigma, _ = _fit_ar(df["close"].values, order)
    for k in (1, 6, 12):
        assert _implied_sd(out, k) == pytest.approx(np.sqrt(k) * sigma, rel=0.08)


def test_the_first_step_is_drawn_with_the_residual_standard_error():
    """
    At one step ahead the envelope collapses to sigma exactly, which makes this
    the place the scale estimate is observable.

    It must be the residual standard *error* -- divided by the degrees of
    freedom left after fitting order + 1 parameters -- not numpy's default
    population deviation, which is biased low and so draws every band on the
    chart slightly too tight.
    """
    order = 3
    df = _ar1_frame(0.5, n=80, seed=5)
    out = forecaster.run_ar_forecast(df, forecast_steps=4, ar_order=order)

    _, sigma, lr = _fit_ar(df["close"].values, order)
    assert _implied_sd(out, 1) == pytest.approx(sigma, rel=1e-9)

    # The biased estimator differs by enough to see on this sample size, so the
    # assertion above is not satisfied by both.
    n_obs = len(lr) - order
    biased = sigma * np.sqrt((n_obs - (order + 1)) / n_obs)
    assert abs(biased - sigma) / sigma > 0.01, "sample too large to tell apart"


def test_the_bands_nest_and_widen():
    df = _ar1_frame(0.4, seed=31)
    out = forecaster.run_ar_forecast(df, forecast_steps=10, ar_order=2)

    for k in range(10):
        assert out["lower_95"][k] < out["lower_68"][k] < out["forecast_price"][k]
        assert out["forecast_price"][k] < out["upper_68"][k] < out["upper_95"][k]

    widths = [out["upper_95"][k] / out["lower_95"][k] for k in range(10)]
    assert widths == sorted(widths), f"bands must not narrow with horizon: {widths}"


# =====================================================================
# The timeline
# =====================================================================

def test_future_daily_timestamps_land_only_on_trading_sessions():
    """
    The projection used to skip Saturdays by hand -- and only Saturdays, so a
    Sunday reachable from a Saturday bar survived, and every market holiday
    survived too. A fifteen-session projection from late November crosses
    Thanksgiving, so the chart drew a forecast for a day the exchange is shut.

    market_calendar already owns this question for the rest of the project.
    """
    df = _ar1_frame(0.3, n=300, seed=3, start="2025-01-02")
    # Land the last bar just before Thanksgiving 2025 (Thursday 27 November).
    df = df.copy()
    df["time"] = pd.bdate_range("2025-01-02", periods=len(df), freq="C")
    cut = df[df["time"] <= "2025-11-21"]
    out = forecaster.run_ar_forecast(cut, forecast_steps=15, ar_order=2)

    dates = [pd.to_datetime(t).date() for t in out["time"]]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    offenders = [d for d in dates if not market_calendar.is_trading_day(d)]
    assert not offenders, f"forecast lands on non-trading days: {offenders}"
    assert datetime.date(2025, 11, 27) not in dates, "Thanksgiving is not a session"


def test_intraday_spacing_is_preserved_rather_than_snapped_to_days():
    """
    The trading-day walk is a daily-bar notion. On an hourly series it would
    collapse fifteen bars onto fifteen separate days, so it must not run.
    """
    df = _ar1_frame(0.2, n=400, seed=9, freq="h")
    out = forecaster.run_ar_forecast(df, forecast_steps=5, ar_order=1)

    stamps = [pd.to_datetime(t) for t in out["time"]]
    gaps = {(b - a) for a, b in zip(stamps, stamps[1:])}
    assert gaps == {pd.Timedelta(hours=1)}, f"hourly spacing not preserved: {gaps}"


# =====================================================================
# Refusals
# =====================================================================

def test_too_little_data_is_refused_rather_than_fitted():
    df = _ar1_frame(0.3, n=8)
    with pytest.raises(ValueError):
        forecaster.run_ar_forecast(df, forecast_steps=5, ar_order=3)


def test_an_order_that_leaves_no_degrees_of_freedom_is_refused():
    """
    The old length check was `len(close) < ar_order + 10`, which admits any
    order up to eight observations' worth of parameters and then divides by a
    degrees-of-freedom count of zero or less.
    """
    df = _ar1_frame(0.3, n=18)
    with pytest.raises(ValueError):
        forecaster.run_ar_forecast(df, forecast_steps=3, ar_order=12)
