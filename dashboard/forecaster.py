"""
The AR projection the chart draws, and the two numbers on it that are claims.

The median path is a projection: it says what the fitted model extrapolates, and
nobody mistakes it for a promise. The bands are different. The chart labels them
"68% CI" and "95% CI", and a person reads that as coverage -- so unlike the line,
they are falsifiable, and for four releases they were false.

They were computed as ``sqrt(k) * sigma``, the spread of a random walk. That
formula ignores the AR coefficients the model has just fitted, which is the
entire content of the model. A shock at step one is still propagating at step
fifteen, so the cumulative spread grows faster than ``sqrt(k)`` whenever the
coefficients are not zero -- measured at 2.8x too narrow on an AR(1) with
phi 0.69 at fifteen steps. The bands agreed with reality exactly when the model
was pointless and understated it the rest of the time, in the direction that
makes a position look safer than it is.

What the band now includes: uncertainty from future shocks, propagated through
the fitted recursion. What it still excludes: uncertainty in the fitted
coefficients themselves, which on a few hundred bars is not negligible. So the
coverage is conditional on the fit being right, and the true interval is wider
than this one -- never narrower. That is said here rather than implied, because
the last version of this file implied something that was not true.
"""
import numpy as np
import pandas as pd

try:
    from dashboard import market_calendar
except ImportError:  # imported as a top-level module from dashboard/
    import market_calendar

#: Standard normal quantiles for the two labelled coverages. Named, because a
#: bare 1.96 in a price calculation reads like a fudge factor.
Z_68 = 1.0
Z_95 = 1.959963984540054


def _fit(log_returns: np.ndarray, ar_order: int):
    """
    OLS on the lagged returns, returning the coefficients and the residual
    standard error.

    The scale is the standard *error*, divided by the degrees of freedom left
    after estimating ``ar_order + 1`` parameters -- not the population deviation
    numpy's ``std()`` returns by default. The difference is a factor of
    ``sqrt(n / (n - p - 1))``: negligible on ten years of dailies, around 8% on
    the eighty-bar window the chart will happily hand over, and in the direction
    that draws every band too tight.
    """
    n = len(log_returns)
    y = log_returns[ar_order:]
    design = np.ones((n - ar_order, ar_order + 1))
    for i in range(ar_order):
        design[:, i + 1] = log_returns[ar_order - 1 - i: n - 1 - i]

    try:
        beta = np.linalg.solve(design.T @ design, design.T @ y)
    except np.linalg.LinAlgError:
        # Rank-deficient, which a constant series produces. Fall back to the
        # mean: every AR coefficient is then zero, the recursion below collapses
        # to a random walk, and the band is the random walk's -- which is the
        # honest answer when there is no estimable dynamic.
        beta = np.zeros(ar_order + 1)
        beta[0] = log_returns.mean()

    residuals = y - design @ beta
    dof = len(y) - (ar_order + 1)
    sigma = float(np.sqrt(residuals @ residuals / dof))
    # A constant series gives exactly zero and every band collapses onto the
    # line. Floor it so the chart still renders a width.
    return beta, max(sigma, 1e-12)


def _cumulative_sd(beta: np.ndarray, sigma: float, steps: int) -> list:
    """
    Standard deviation of the cumulative log return at each horizon.

    The AR(p) recursion is an infinite moving average in its own shocks: a unit
    shock at time t moves the return at t+j by psi[j], where psi[0] = 1 and
    psi[j] = sum(phi[i] * psi[j-i]). What the band needs is the spread of the
    *sum* of the next k returns, so the weight carried by the shock at step i is
    the partial sum of psi up to k-i, and the variance is sigma squared times
    the sum of those weights squared.

    With every phi at zero, psi is 1 followed by zeros, each partial sum is 1,
    and this returns ``sigma * sqrt(k)`` -- the old formula, recovered exactly in
    the one case where it was correct.
    """
    phi = beta[1:]
    order = len(phi)

    psi = np.zeros(steps)
    psi[0] = 1.0
    for j in range(1, steps):
        psi[j] = sum(phi[i] * psi[j - i - 1] for i in range(min(order, j)))

    weights = np.cumsum(psi)          # weights[m] = sum(psi[0..m])
    squared = weights ** 2
    # Var(S_k) = sigma^2 * sum of the first k squared weights.
    return [sigma * float(np.sqrt(total)) for total in np.cumsum(squared)]


def _future_stamps(times, steps: int) -> list:
    """
    Where the projected bars sit on the time axis.

    This used to add a day at a time and step over Saturdays by hand. It caught
    only Saturdays -- a Sunday is reachable from a Saturday bar -- and no market
    holiday at all, so a fifteen-session projection from late November drew a
    forecast bar on Thanksgiving. market_calendar already answers this question
    for the rest of the project; asking it is both correct and one fewer calendar
    to maintain.

    Only for daily-or-coarser bars. On an hourly series a trading-day walk would
    spread fifteen bars across fifteen days, so intraday spacing is preserved as
    it is: session boundaries within a day are a question this does not model and
    should not pretend to.
    """
    last = pd.to_datetime(times.iloc[-1])
    try:
        spacing = last - pd.to_datetime(times.iloc[-2])
    except Exception:
        spacing = pd.Timedelta(days=1)
    if spacing <= pd.Timedelta(0):
        spacing = pd.Timedelta(days=1)

    daily_or_coarser = spacing >= pd.Timedelta(days=1)
    stamps, current = [], last
    for _ in range(steps):
        current = current + spacing
        if daily_or_coarser:
            while not market_calendar.is_trading_day(current.date()):
                current = current + pd.Timedelta(days=1)
        stamps.append(current)
    return [t.strftime("%Y-%m-%d %H:%M:%S") for t in stamps]


def run_ar_forecast(df: pd.DataFrame, forecast_steps: int = 15,
                    ar_order: int = 3) -> dict:
    """
    Fit an AR(p) on log returns and project the price path with 68% and 95%
    bands.

    The bands are quantiles of a lognormal around the median path, which is what
    makes them asymmetric in price and symmetric in log price. ``forecast_price``
    is the median of that distribution, not its mean -- for a lognormal the two
    differ, and the median is the one the recursion produces.
    """
    if forecast_steps < 1:
        raise ValueError("forecast_steps must be at least 1.")
    if ar_order < 1:
        raise ValueError("ar_order must be at least 1.")

    close = np.asarray(df["close"], dtype=float)
    if len(close) < ar_order + 10:
        raise ValueError(
            f"Insufficient data to fit an AR({ar_order}) model: "
            f"{len(close)} bars, need at least {ar_order + 10}.")

    log_returns = np.log(close[1:] / close[:-1])

    # Separate from the length check above, which is a minimum sample size. This
    # is the arithmetic requirement: the residual scale divides by the degrees
    # of freedom, and a high order against a short window drives that to zero or
    # below. The old code had only the length check and would have divided by a
    # non-positive count.
    dof = (len(log_returns) - ar_order) - (ar_order + 1)
    if dof < 1:
        raise ValueError(
            f"An AR({ar_order}) model leaves {dof} degrees of freedom on "
            f"{len(close)} bars; the residual scale is not estimable. Use a "
            "lower order or a longer window.")

    beta, sigma = _fit(log_returns, ar_order)

    # Iterate the fitted recursion forward, feeding each prediction back in as
    # the next lag. This is the conditional mean path, so it carries no shocks;
    # the spread around it comes from _cumulative_sd.
    lags = list(log_returns[-ar_order:])
    forecast_returns = []
    for _ in range(forecast_steps):
        step = beta[0] + sum(beta[i + 1] * lags[-(i + 1)] for i in range(ar_order))
        forecast_returns.append(step)
        lags.append(step)

    sd = _cumulative_sd(beta, sigma, forecast_steps)

    last_price = float(close[-1])
    median, upper_68, lower_68, upper_95, lower_95 = [], [], [], [], []
    cumulative = 0.0
    for k in range(forecast_steps):
        cumulative += forecast_returns[k]
        price = last_price * np.exp(cumulative)
        median.append(price)
        upper_68.append(price * np.exp(Z_68 * sd[k]))
        lower_68.append(price * np.exp(-Z_68 * sd[k]))
        upper_95.append(price * np.exp(Z_95 * sd[k]))
        lower_95.append(price * np.exp(-Z_95 * sd[k]))

    return {
        "time": _future_stamps(df["time"], forecast_steps),
        "forecast_price": median,
        "upper_68": upper_68,
        "lower_68": lower_68,
        "upper_95": upper_95,
        "lower_95": lower_95,
    }
