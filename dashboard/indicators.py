import numpy as np

try:
    from dashboard import ta_core
except ImportError:  # imported as a top-level module from dashboard/
    import ta_core
import pandas as pd

def calculate_sma(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    return df[column].rolling(window=period).mean()

def calculate_ema(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    return df[column].ewm(span=period, adjust=False).mean()

def calculate_wma(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    weights = np.arange(1, period + 1)
    return df[column].rolling(period).apply(lambda prices: np.dot(prices, weights) / weights.sum(), raw=True)

def calculate_hma(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    half_length = int(period / 2)
    sqrt_length = int(np.sqrt(period))
    wma_half = calculate_wma(df, half_length, column)
    wma_full = calculate_wma(df, period, column)
    diff = 2 * wma_half - wma_full
    diff_df = pd.DataFrame({"diff": diff})
    return calculate_wma(diff_df, sqrt_length, "diff")

def calculate_dema(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    ema1 = calculate_ema(df, period, column)
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    return 2 * ema1 - ema2

def calculate_tema(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    ema1 = calculate_ema(df, period, column)
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    return 3 * ema1 - 3 * ema2 + ema3

def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9, column: str = "close") -> pd.DataFrame:
    fast_ema = calculate_ema(df, fast, column)
    slow_ema = calculate_ema(df, slow, column)
    macd_val = fast_ema - slow_ema
    macd_df = pd.DataFrame({"macd": macd_val})
    signal_val = calculate_ema(macd_df, signal, "macd")
    hist = macd_val - signal_val
    return pd.DataFrame({"macd": macd_val, "macd_signal": signal_val, "macd_hist": hist}, index=df.index)

def calculate_rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    """
    Wilder's RSI, delegated to the conformance-pinned core.

    This was `ewm(com=period-1, adjust=False)`, which seeds the average from
    bar 0. Wilder seeds from SMA(first period) at index period-1, which is what
    every charting platform reports. Measured against the pinned core on a
    400-bar series: **18.8 RSI points apart at bar 14**, 1.9 by bar 50, 0.04 by
    bar 100, identical past bar 200. Converged where the chart looks, wrong
    across the warm-up that short windows and the backtester's early trades
    live in -- and 1.9 points is enough to flip a 30/70 threshold at the edge.

    The pinned core also returns NaN on a genuinely flat series where this
    returned 50.0. NaN is the honest answer and the one alert_manager already
    expects: it raises rather than reading an unmeasurable RSI as "condition
    not met". A halted or untraded symbol no longer reports a neutral 50 as
    though it were measured.

    RSI = 100 on an uninterrupted uptrend (no down moves at all) is preserved --
    the pinned core does the same thing, so the fix that behaviour came from
    survives the swap.
    """
    values = ta_core.REGISTRY["rsi"](df[column].to_numpy(dtype=float), period=int(period))
    return pd.Series(values, index=df.index)

def calculate_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    low_min = df["low"].rolling(window=k_period).min()
    high_max = df["high"].rolling(window=k_period).max()
    stoch_k = 100 * ((df["close"] - low_min) / (high_max - low_min).replace(0, np.nan))
    stoch_d = stoch_k.rolling(window=d_period).mean()
    return pd.DataFrame({"stoch_k": stoch_k, "stoch_d": stoch_d}, index=df.index)

def calculate_stoch_rsi(df: pd.DataFrame, period: int = 14, k_period: int = 3, d_period: int = 3, column: str = "close") -> pd.DataFrame:
    rsi = calculate_rsi(df, period, column)
    rsi_min = rsi.rolling(window=period).min()
    rsi_max = rsi.rolling(window=period).max()
    stoch_rsi_k = 100 * ((rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan))
    stoch_rsi_d = stoch_rsi_k.rolling(window=d_period).mean()
    return pd.DataFrame({"stoch_rsi_k": stoch_rsi_k, "stoch_rsi_d": stoch_rsi_d}, index=df.index)

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Wilder's ATR, delegated to the conformance-pinned core.

    Same seeding correction as calculate_rsi: `ewm(alpha=1/period,
    adjust=False)` starts from bar 0 rather than from SMA(first period).
    Measured 0.198 apart at bar 13 (10% relative), converged past bar 200.
    ATR sizes every stop in calculate_position_size and feeds the volatility
    reading in the metric strip, so the warm-up region is not cosmetic.
    """
    values = ta_core.REGISTRY["atr"](
        df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
        period=int(period),
    )
    return pd.Series(values, index=df.index)

def calculate_natr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    atr = calculate_atr(df, period)
    return (atr / df["close"]) * 100

def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20, num_std: float = 2.0, column: str = "close") -> pd.DataFrame:
    sma = calculate_sma(df, period, column)
    # ddof=0: Bollinger bands use the population standard deviation. Pandas
    # defaults to the sample std (ddof=1), which widens the bands and puts this
    # out of step with every charting platform the numbers get compared against.
    std = df[column].rolling(window=period).std(ddof=0)
    upper = sma + (num_std * std)
    lower = sma - (num_std * std)
    bandwidth = (upper - lower) / sma
    return pd.DataFrame({"bb_middle": sma, "bb_upper": upper, "bb_lower": lower, "bb_width": bandwidth}, index=df.index)

def calculate_keltner_channels(df: pd.DataFrame, period: int = 20, multiplier: float = 1.5, ema_period: int = 20) -> pd.DataFrame:
    middle = calculate_ema(df, ema_period, "close")
    atr = calculate_atr(df, period)
    upper = middle + (multiplier * atr)
    lower = middle - (multiplier * atr)
    return pd.DataFrame({"kc_middle": middle, "kc_upper": upper, "kc_lower": lower}, index=df.index)

def calculate_donchian_channels(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    upper = df["high"].rolling(window=period).max()
    lower = df["low"].rolling(window=period).min()
    middle = (upper + lower) / 2
    return pd.DataFrame({"dc_upper": upper, "dc_middle": middle, "dc_lower": lower}, index=df.index)

def calculate_williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_max = df["high"].rolling(window=period).max()
    low_min = df["low"].rolling(window=period).min()
    return -100 * ((high_max - df["close"]) / (high_max - low_min).replace(0, np.nan))

def calculate_cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = tp.rolling(window=period).mean()
    mean_dev = tp.rolling(window=period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma_tp) / (0.015 * mean_dev.replace(0, np.nan))

def calculate_roc(df: pd.DataFrame, period: int = 12, column: str = "close") -> pd.Series:
    shifted = df[column].shift(period)
    return ((df[column] - shifted) / shifted) * 100

def calculate_mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    tp_diff = tp.diff()
    pos_flow = pd.Series(0.0, index=df.index)
    neg_flow = pd.Series(0.0, index=df.index)
    pos_flow[tp_diff > 0] = rmf[tp_diff > 0]
    neg_flow[tp_diff < 0] = rmf[tp_diff < 0]
    pos_mf = pos_flow.rolling(window=period).sum()
    neg_mf = neg_flow.rolling(window=period).sum()
    mfr = pos_mf / neg_mf.replace(0, np.nan)
    mfi = 100 - (100 / (1 + mfr))

    # No negative money flow in the window means MFI is 100, not undefined.
    return mfi.mask(neg_mf.eq(0) & pos_mf.gt(0), 100.0)

def calculate_obv(df: pd.DataFrame) -> pd.Series:
    diff = df["close"].diff()
    direction = pd.Series(np.zeros(len(df)), index=df.index)
    direction[diff > 0] = 1.0
    direction[diff < 0] = -1.0
    return (direction * df["volume"]).cumsum()

def calculate_cmf(df: pd.DataFrame, period: int = 20) -> pd.Series:
    ad_multiplier = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"]).replace(0, np.nan)
    ad_volume = ad_multiplier * df["volume"]
    return ad_volume.rolling(window=period).sum() / df["volume"].rolling(window=period).sum().replace(0, np.nan)

def calculate_pvt(df: pd.DataFrame) -> pd.Series:
    close_pct = df["close"].pct_change()
    return (close_pct * df["volume"]).cumsum().fillna(0)

def _is_intraday(df: pd.DataFrame) -> bool:
    """True when bars are finer than one day, inferred from the time column."""
    if "time" not in df.columns or len(df) < 3:
        return False
    try:
        ts = pd.to_datetime(df["time"])
        return bool(ts.diff().dropna().median() < pd.Timedelta(days=1))
    except Exception:
        return False


def _session_key(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["time"]).dt.date


def calculate_vwap(df: pd.DataFrame, rolling_period: int = 20) -> pd.Series:
    """
    Volume-weighted average price.

    VWAP is a *session* statistic. Accumulating it from the first bar of an
    arbitrary fetch window made the value depend on `count`: the same daily bar
    returned VWAP 149.90, 153.43 or 155.26 for count=50/100/250, and on a long
    daily window it drifted hundreds of dollars below spot while still being
    labelled "VWAP".

    So: intraday bars reset the accumulation each session (the standard
    definition), and daily-or-slower bars use a rolling `rolling_period` VWAP
    (a well-defined measure that does not depend on how much history was asked
    for). Both are stable under a change of `count`.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]

    if _is_intraday(df):
        session = _session_key(df)
        cum_pv = pv.groupby(session).cumsum()
        cum_vol = df["volume"].groupby(session).cumsum()
        return cum_pv / cum_vol.replace(0, np.nan)

    win = min(rolling_period, max(len(df), 1))
    cum_pv = pv.rolling(window=win, min_periods=1).sum()
    cum_vol = df["volume"].rolling(window=win, min_periods=1).sum()
    return cum_pv / cum_vol.replace(0, np.nan)


def calculate_vwap_bands(df: pd.DataFrame, num_std: float = 2.0, rolling_period: int = 20) -> pd.DataFrame:
    """VWAP with volume-weighted standard-deviation bands, on the same anchoring as calculate_vwap."""
    vwap = calculate_vwap(df, rolling_period)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    dev = ((tp - vwap) ** 2) * df["volume"]

    if _is_intraday(df):
        session = _session_key(df)
        cum_dev = dev.groupby(session).cumsum()
        cum_vol = df["volume"].groupby(session).cumsum()
    else:
        win = min(rolling_period, max(len(df), 1))
        cum_dev = dev.rolling(window=win, min_periods=1).sum()
        cum_vol = df["volume"].rolling(window=win, min_periods=1).sum()

    vwap_std = np.sqrt(cum_dev / cum_vol.replace(0, np.nan))
    return pd.DataFrame({
        "vwap": vwap,
        "vwap_upper": vwap + (num_std * vwap_std),
        "vwap_lower": vwap - (num_std * vwap_std),
    }, index=df.index)

def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    ADX with +DI/-DI, delegated to the conformance-pinned core.

    ADX is Wilder-smoothed three times over — the directional movements, the
    true range, and then DX itself — so it compounds the bar-zero seeding error
    rather than diluting it. Delegating keeps all three consistent, and the
    regime classifier that reads ADX no longer depends on which smoothing
    convention happened to be used at each of the three steps.
    """
    out = ta_core.REGISTRY["adx"](
        df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
        period=int(period),
    )
    return pd.DataFrame({"adx": out["adx"], "plus_di": out["plus_di"],
                         "minus_di": out["minus_di"]}, index=df.index)

def calculate_std(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    return df[column].rolling(window=period).std()

def calculate_mom(df: pd.DataFrame, period: int = 10, column: str = "close") -> pd.Series:
    return df[column].diff(period)

def calculate_ultimate_oscillator(df: pd.DataFrame, p1: int = 7, p2: int = 14, p3: int = 28) -> pd.Series:
    prev_close = df["close"].shift(1)
    bp = df["close"] - pd.concat([df["low"], prev_close], axis=1).min(axis=1)
    tr = pd.concat([df["high"], prev_close], axis=1).max(axis=1) - pd.concat([df["low"], prev_close], axis=1).min(axis=1)
    
    avg7 = bp.rolling(p1).sum() / tr.rolling(p1).sum().replace(0, np.nan)
    avg14 = bp.rolling(p2).sum() / tr.rolling(p2).sum().replace(0, np.nan)
    avg28 = bp.rolling(p3).sum() / tr.rolling(p3).sum().replace(0, np.nan)
    
    uo = 100 * (4 * avg7 + 2 * avg14 + avg28) / 7
    return uo

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    atr = calculate_atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    
    basic_ub = hl2 + (multiplier * atr)
    basic_lb = hl2 - (multiplier * atr)
    
    final_ub = pd.Series(0.0, index=df.index)
    final_lb = pd.Series(0.0, index=df.index)
    supertrend = pd.Series(0.0, index=df.index)
    direction = pd.Series(1, index=df.index) # 1 = up, -1 = down

    if len(df) == 0:
        return pd.DataFrame({"supertrend": supertrend, "supertrend_dir": direction}, index=df.index)

    for i in range(1, len(df)):
        # Upper band
        if basic_ub.iloc[i] < final_ub.iloc[i-1] or df["close"].iloc[i-1] > final_ub.iloc[i-1]:
            final_ub.iloc[i] = basic_ub.iloc[i]
        else:
            final_ub.iloc[i] = final_ub.iloc[i-1]
            
        # Lower band
        if basic_lb.iloc[i] > final_lb.iloc[i-1] or df["close"].iloc[i-1] < final_lb.iloc[i-1]:
            final_lb.iloc[i] = basic_lb.iloc[i]
        else:
            final_lb.iloc[i] = final_lb.iloc[i-1]
            
        # Supertrend direction
        if supertrend.iloc[i-1] == final_ub.iloc[i-1]:
            if df["close"].iloc[i] > final_ub.iloc[i]:
                direction.iloc[i] = 1
                supertrend.iloc[i] = final_lb.iloc[i]
            else:
                direction.iloc[i] = -1
                supertrend.iloc[i] = final_ub.iloc[i]
        else:
            if df["close"].iloc[i] < final_lb.iloc[i]:
                direction.iloc[i] = -1
                supertrend.iloc[i] = final_ub.iloc[i]
            else:
                direction.iloc[i] = 1
                supertrend.iloc[i] = final_lb.iloc[i]

    # Bar 0 never had a band to compute; leaving it at 0.0 drew a line to zero
    # on charts and looked like a real price level.
    supertrend.iloc[0] = np.nan
    direction.iloc[0] = np.nan

    return pd.DataFrame({"supertrend": supertrend, "supertrend_dir": direction}, index=df.index)

def calculate_ichimoku(df: pd.DataFrame, conversion_period: int = 9, base_period: int = 26, span_b_period: int = 52, lagging_span_period: int = 26) -> pd.DataFrame:
    tenkan_sen = (df["high"].rolling(conversion_period).max() + df["low"].rolling(conversion_period).min()) / 2
    kijun_sen = (df["high"].rolling(base_period).max() + df["low"].rolling(base_period).min()) / 2
    
    senkou_span_a = ((tenkan_sen + kijun_sen) / 2).shift(base_period)
    senkou_span_b = ((df["high"].rolling(span_b_period).max() + df["low"].rolling(span_b_period).min()) / 2).shift(base_period)
    
    chikou_span = df["close"].shift(-lagging_span_period)
    
    return pd.DataFrame({
        "ichimoku_conversion": tenkan_sen,
        "ichimoku_base": kijun_sen,
        "ichimoku_span_a": senkou_span_a,
        "ichimoku_span_b": senkou_span_b,
        "ichimoku_lagging": chikou_span
    }, index=df.index)

def calculate_tsi(df: pd.DataFrame, long_period: int = 25, short_period: int = 13, signal_period: int = 7) -> pd.DataFrame:
    diff = df["close"].diff()
    abs_diff = diff.abs()
    
    # Smooth momentum
    ema_double = diff.ewm(span=long_period, adjust=False).mean().ewm(span=short_period, adjust=False).mean()
    abs_ema_double = abs_diff.ewm(span=long_period, adjust=False).mean().ewm(span=short_period, adjust=False).mean()
    
    tsi = 100 * (ema_double / abs_ema_double.replace(0, np.nan))
    tsi_signal = tsi.ewm(span=signal_period, adjust=False).mean()
    
    return pd.DataFrame({"tsi": tsi, "tsi_signal": tsi_signal}, index=df.index)

def calculate_ao(df: pd.DataFrame) -> pd.Series:
    median = (df["high"] + df["low"]) / 2
    return calculate_sma(pd.DataFrame({"close": median}), 5) - calculate_sma(pd.DataFrame({"close": median}), 34)

def calculate_pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    # Classic daily pivots computed from previous period's HLC
    high = df["high"].shift(1)
    low = df["low"].shift(1)
    close = df["close"].shift(1)
    
    pp = (high + low + close) / 3
    r1 = 2 * pp - low
    s1 = 2 * pp - high
    r2 = pp + (high - low)
    s2 = pp - (high - low)
    r3 = high + 2 * (pp - low)
    s3 = low - 2 * (high - pp)
    
    return pd.DataFrame({"pivot_pp": pp, "pivot_r1": r1, "pivot_s1": s1, "pivot_r2": r2, "pivot_s2": s2, "pivot_r3": r3, "pivot_s3": s3}, index=df.index)

def classify_market_regime(df: pd.DataFrame, precomputed: dict = None) -> pd.DataFrame:
    """
    Classifies market regime into one of 4 states:
    1: Bullish Trending
    2: Bearish Trending
    3: High Volatility Expansion
    4: Mean-Reverting Range-Bound

    `precomputed` optionally supplies adx/bb/ema_fast/ema_slow frames to avoid
    recalculating them.
    """
    p = precomputed or {}
    adx_df = p.get("adx") if p.get("adx") is not None else calculate_adx(df)
    bb_df = p.get("bb") if p.get("bb") is not None else calculate_bollinger_bands(df)
    ema_fast = p.get("ema_fast") if p.get("ema_fast") is not None else calculate_ema(df, 20)
    ema_slow = p.get("ema_slow") if p.get("ema_slow") is not None else calculate_ema(df, 50)
    
    # BB Width 20-period SMA & Std
    bb_width = bb_df["bb_width"]
    bb_width_sma = bb_width.rolling(window=20).mean()
    bb_width_std = bb_width.rolling(window=20).std()
    
    regimes = []
    adx = adx_df["adx"]
    close = df["close"]
    
    for i in range(len(df)):
        if pd.isna(adx.iloc[i]) or pd.isna(bb_width.iloc[i]):
            regimes.append("Mixed Trend")
            continue
            
        # Volatility expansion check: width is > 1.5 standard deviations above its 20 SMA
        is_expansion = bb_width.iloc[i] > (bb_width_sma.iloc[i] + 1.5 * bb_width_std.iloc[i])
        
        if is_expansion:
            regimes.append("Volatility Expansion")
        elif adx.iloc[i] > 23:
            if close.iloc[i] > ema_fast.iloc[i] and ema_fast.iloc[i] > ema_slow.iloc[i]:
                regimes.append("Bullish Trending")
            elif close.iloc[i] < ema_fast.iloc[i] and ema_fast.iloc[i] < ema_slow.iloc[i]:
                regimes.append("Bearish Trending")
            else:
                regimes.append("Mixed Trend")
        elif adx.iloc[i] < 20:
            regimes.append("Mean-Reverting / Range-Bound")
        else:
            regimes.append("Mixed Trend")
            
    return pd.DataFrame({"regime": regimes}, index=df.index)

# Bars needed before the consensus is built on anything real. EMA(50) and
# MACD(26,9) are *defined* from bar 0 (ewm with adjust=False seeds at close[0])
# but carry no information until they have seen their period. Scoring them
# anyway produced confident-looking readings on the opening bars -- invisible
# when only the latest bar is read, but the backtester trades the whole series.
CONSENSUS_WARMUP_BARS = 50


def calculate_adaptive_consensus(df: pd.DataFrame, precomputed: dict = None) -> pd.Series:
    """
    Calculates a consensus score (-5 to +5) adaptively weighted by the market regime:
      - Trending: Trend indicators (MACD, MA, SuperTrend) have 80% weight.
      - Mean-Reverting: Oscillators (RSI, Stochastic, BB touch) have 80% weight.
      - Mixed/Expansion: Split equally.

    The first CONSENSUS_WARMUP_BARS bars are NaN rather than a number nobody
    should act on.

    `precomputed` optionally supplies already-calculated component frames
    (rsi/macd/bb/supertrend/ema_fast/ema_slow/regime) so a caller that has
    already built them does not pay for them twice -- SuperTrend in particular
    is a per-bar Python loop.
    """
    p = precomputed or {}
    rsi = p.get("rsi") if p.get("rsi") is not None else calculate_rsi(df)
    macd_df = p.get("macd") if p.get("macd") is not None else calculate_macd(df)
    bb_df = p.get("bb") if p.get("bb") is not None else calculate_bollinger_bands(df)
    st_df = p.get("supertrend") if p.get("supertrend") is not None else calculate_supertrend(df)
    ema_fast = p.get("ema_fast") if p.get("ema_fast") is not None else calculate_ema(df, 20)
    ema_slow = p.get("ema_slow") if p.get("ema_slow") is not None else calculate_ema(df, 50)
    regime_df = p.get("regime") if p.get("regime") is not None else classify_market_regime(df)

    consensus = []
    
    for i in range(len(df)):
        r = regime_df["regime"].iloc[i]
        
        # 1. Oscillator Signals (-1 to +1)
        # RSI
        rsi_val = rsi.iloc[i]
        rsi_sig = 0.0
        if rsi_val < 30: rsi_sig = 1.0
        elif rsi_val > 70: rsi_sig = -1.0
        
        # BB Touch
        close_val = df["close"].iloc[i]
        bb_sig = 0.0
        if close_val <= bb_df["bb_lower"].iloc[i]: bb_sig = 1.0
        elif close_val >= bb_df["bb_upper"].iloc[i]: bb_sig = -1.0
        
        osc_score = (rsi_sig + bb_sig) / 2.0
        
        # 2. Trend Signals (-1 to +1)
        # Each of these is neutral when it has no opinion yet. Previously they
        # were binary with no neutral state, and `NaN > NaN` is False, so during
        # indicator warm-up all three read -1 and the consensus opened at a
        # spurious -3.75 (strong sell). Harmless for the latest bar, but the
        # backtester runs the whole series and traded on that phantom signal.
        def _cmp(a, b):
            if pd.isna(a) or pd.isna(b) or a == b:
                return 0.0
            return 1.0 if a > b else -1.0

        macd_sig_val = _cmp(macd_df["macd"].iloc[i], macd_df["macd_signal"].iloc[i])

        st_dir = st_df["supertrend_dir"].iloc[i]
        st_sig = 0.0 if pd.isna(st_dir) else (1.0 if st_dir == 1 else -1.0)

        ema_sig = _cmp(ema_fast.iloc[i], ema_slow.iloc[i])

        trend_score = (macd_sig_val + st_sig + ema_sig) / 3.0
        
        # 3. Apply Regime weighting
        if r in ["Bullish Trending", "Bearish Trending"]:
            # Focus on Trend
            score = 0.8 * trend_score + 0.2 * osc_score
        elif r == "Mean-Reverting / Range-Bound":
            # Focus on Oscillators
            score = 0.8 * osc_score + 0.2 * trend_score
        else:
            # Equal weight
            score = 0.5 * trend_score + 0.5 * osc_score
            
        # Scale to -5.0 to +5.0 range
        consensus.append(score * 5.0)

    series = pd.Series(consensus, index=df.index, dtype=float)
    if len(series) > 0:
        series.iloc[:min(CONSENSUS_WARMUP_BARS, len(series))] = np.nan
    return series

def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates all 50+ indicator columns and joins them to the dataframe."""
    res = df.copy()
    
    # Trend
    res["sma_20"] = calculate_sma(df, 20)
    res["sma_50"] = calculate_sma(df, 50)
    res["sma_200"] = calculate_sma(df, 200)
    res["ema_9"] = calculate_ema(df, 9)
    res["ema_21"] = calculate_ema(df, 21)
    res["wma_14"] = calculate_wma(df, 14)
    res["hma_14"] = calculate_hma(df, 14)
    res["dema_14"] = calculate_dema(df, 14)
    res["tema_14"] = calculate_tema(df, 14)
    
    macd_df = calculate_macd(df)
    res = res.join(macd_df)
    
    # Momentum
    res["rsi_14"] = calculate_rsi(df, 14)
    stoch_df = calculate_stochastic(df)
    res = res.join(stoch_df)
    stoch_rsi_df = calculate_stoch_rsi(df)
    res = res.join(stoch_rsi_df)
    res["williams_r"] = calculate_williams_r(df)
    res["cci_20"] = calculate_cci(df)
    res["roc_12"] = calculate_roc(df)
    res["mfi_14"] = calculate_mfi(df)
    res["mom_10"] = calculate_mom(df)
    res["ultimate_osc"] = calculate_ultimate_oscillator(df)
    res["ao"] = calculate_ao(df)
    
    # Volatility
    bb_df = calculate_bollinger_bands(df)
    res = res.join(bb_df)
    kc_df = calculate_keltner_channels(df)
    res = res.join(kc_df)
    dc_df = calculate_donchian_channels(df)
    res = res.join(dc_df)
    res["atr_14"] = calculate_atr(df)
    res["natr_14"] = calculate_natr(df)
    res["std_14"] = calculate_std(df)
    
    # Volume
    res["obv"] = calculate_obv(df)
    res["cmf_20"] = calculate_cmf(df)
    res["pvt"] = calculate_pvt(df)
    
    vwap_df = calculate_vwap_bands(df)
    res = res.join(vwap_df)
    
    # Complex / Multi-column
    adx_df = calculate_adx(df)
    res = res.join(adx_df)

    st_df = calculate_supertrend(df)
    res = res.join(st_df)

    ich_df = calculate_ichimoku(df)
    res = res.join(ich_df)

    tsi_df = calculate_tsi(df)
    res = res.join(tsi_df)

    pivots = calculate_pivot_points(df)
    res = res.join(pivots)

    # Regime and consensus reuse what has already been built. Recomputing them
    # meant EMA ran 17x, Bollinger 4x, ADX 3x and SuperTrend -- a per-bar Python
    # loop -- twice per call.
    ema_fast = calculate_ema(df, 20)
    ema_slow = calculate_ema(df, 50)
    shared = {
        "adx": adx_df, "bb": bb_df, "rsi": res["rsi_14"], "macd": macd_df,
        "supertrend": st_df, "ema_fast": ema_fast, "ema_slow": ema_slow,
    }

    regime_df = classify_market_regime(df, precomputed=shared)
    res = res.join(regime_df)

    shared["regime"] = regime_df
    res["consensus_score"] = calculate_adaptive_consensus(df, precomputed=shared)

    return res
