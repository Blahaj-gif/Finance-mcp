"""
Indicator correctness tests.

Every check here is against a value that is knowable independently — a textbook
edge case, a hand-rolled reference implementation, or an invariant that must
hold regardless of how much history was requested.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import indicators as ind
from dashboard import ta_core


def frame(closes, intraday=False, volume=None):
    c = pd.Series(closes, dtype=float)
    freq = "15min" if intraday else "D"
    start = "2026-08-05 09:30" if intraday else "2026-01-01"
    t = pd.date_range(start, periods=len(c), freq=freq)
    return pd.DataFrame({
        "time": t.strftime("%Y-%m-%d %H:%M:%S"),
        "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "volume": volume if volume is not None else [1000] * len(c),
    })


def noisy(n=120, seed=3):
    return frame(100 + np.random.RandomState(seed).randn(n).cumsum())


# =====================================================================
# RSI / MFI boundary values
# =====================================================================

def test_rsi_is_100_when_there_are_no_losses():
    """
    An unbroken advance has RSI 100. Dividing by a zero average loss produced
    NaN, which a blanket .fillna(50) turned into "neutral" — the single most
    load-bearing signal in the verdict, reporting the opposite of the truth.
    """
    assert ind.calculate_rsi(frame([100 + i for i in range(30)])).iloc[-1] == pytest.approx(100.0)


def test_rsi_is_0_when_there_are_no_gains():
    assert ind.calculate_rsi(frame([200 - i for i in range(30)])).iloc[-1] == pytest.approx(0.0)


def test_rsi_is_undefined_not_neutral_on_a_flat_series():
    """
    This used to return 50.0. That reads as a measured neutral reading, and the
    alert daemon would evaluate it as one — an RSI_BELOW 30 alert on a halted or
    untraded symbol would report "condition not met" as though it had looked.
    NaN is the honest answer, and `_finite()` in alert_watcher already raises on
    it rather than treating it as a no.
    """
    import math
    assert math.isnan(ind.calculate_rsi(frame([100] * 30)).iloc[-1])


def test_rsi_is_100_on_an_uninterrupted_uptrend():
    """
    Preserved across the swap to the pinned core. A blanket "undefined -> 50"
    once reported neutral for an asset that had never had a down day, and RSI
    feeds both the market-analysis verdict and the consensus score.
    """
    assert ind.calculate_rsi(frame([100 + i for i in range(40)])).iloc[-1] == pytest.approx(100.0)


def test_rsi_uses_wilder_seeding_not_a_bar_zero_seed():
    """
    The correction that came with the pinned core. `ewm(com=period-1,
    adjust=False)` seeds from bar 0; Wilder seeds from SMA(first period) at
    index period-1, which is what every charting platform reports. Measured
    18.8 RSI points apart at bar 14 on a 400-bar series, converging to zero
    past bar 200 — right where the chart looks, wrong across the warm-up that
    short windows and the backtester's early trades live in.
    """
    d = noisy(400)
    close = d["close"]
    old = (lambda g, l: 100 - 100 / (1 + g / l))(
        close.diff().clip(lower=0).ewm(com=13, adjust=False).mean(),
        (-close.diff().clip(upper=0)).ewm(com=13, adjust=False).mean())
    new = ind.calculate_rsi(d)

    assert abs(new.iloc[14] - old.iloc[14]) > 1.0, "seeding should differ during warm-up"
    assert new.iloc[-1] == pytest.approx(old.iloc[-1], abs=1e-6), "and converge afterwards"


def test_rsi_stays_within_bounds():
    r = ind.calculate_rsi(noisy()).dropna()
    assert r.between(0, 100).all()


def test_mfi_is_100_when_there_is_no_negative_flow():
    assert ind.calculate_mfi(frame([100 + i for i in range(30)])).iloc[-1] == pytest.approx(100.0)


# =====================================================================
# Convention conformance
# =====================================================================

def test_bollinger_uses_population_std():
    """Charting platforms use ddof=0; pandas defaults to ddof=1 and widens the bands."""
    d = noisy(60, seed=0)
    bb = ind.calculate_bollinger_bands(d)
    expected = (d["close"].rolling(20).mean() + 2 * d["close"].rolling(20).std(ddof=0)).iloc[-1]
    assert bb["bb_upper"].iloc[-1] == pytest.approx(expected)


def test_adx_delegates_to_the_conformance_pinned_core():
    """
    This used to hand-roll a Wilder reference with pandas' `ewm`, which seeds
    from bar 0 — so once ATR was corrected the reference was the thing that was
    wrong. Rewriting it by hand missed twice more: ADX is smoothed three times
    over (+DM/-DM, TR, then DX), and DX cannot exist until both DI lines do, so
    its average seeds from DX's own first valid bar rather than at period-1.

    A fourth guess would be no more trustworthy than the third. `adx` is one of
    the 96 callables pinned by conformance/ta golden vectors, which a second
    independent runtime must also satisfy — that fixture is the authority on the
    arithmetic now, checked by test_ta_conformance.py. What is worth asserting
    here is that this wrapper actually routes to it and preserves index
    alignment.
    """
    d = noisy(150)
    got = ind.calculate_adx(d, 14)
    reference = ta_core.REGISTRY["adx"](
        d["high"].to_numpy(dtype=float), d["low"].to_numpy(dtype=float),
        d["close"].to_numpy(dtype=float), period=14)

    for key in ("adx", "plus_di", "minus_di"):
        assert got[key].to_numpy() == pytest.approx(reference[key], nan_ok=True), key
    assert list(got.index) == list(d.index), "index alignment broken"


def test_adx_and_its_directional_lines_stay_in_range():
    """Properties the golden vector does not state: ADX and DI are percentages."""
    got = ind.calculate_adx(noisy(150), 14).dropna()
    for key in ("adx", "plus_di", "minus_di"):
        assert got[key].between(0, 100).all(), f"{key} left 0..100"


def test_atr_uses_wilder_seeding():
    """
    True range is unchanged; the smoothing seed is what moved. Wilder seeds the
    average from SMA(first period) at index period-1, not from bar 0 — measured
    0.198 apart at bar 13 (10% relative) before the swap. ATR sizes every stop
    in calculate_position_size, so the warm-up region is not cosmetic.
    """
    d = noisy(60)
    n = 14
    h, l, c = d["high"], d["low"], d["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)

    got = ind.calculate_atr(d, n)
    # Wilder: seed at index n-1 is the mean of the first n true ranges. TR[0] is
    # NaN (no previous close), so the core's own TR seeding governs index n-1;
    # assert the recursion from there, which is the part that was wrong.
    for i in range(n + 1, len(d)):
        expected = (1.0 / n) * tr.iloc[i] + (1 - 1.0 / n) * got.iloc[i - 1]
        assert got.iloc[i] == pytest.approx(expected, rel=1e-9)

    # And it must NOT match the old bar-zero-seeded form during warm-up.
    old = tr.ewm(alpha=1 / n, adjust=False).mean()
    assert abs(got.iloc[n] - old.iloc[n]) > 1e-6


# =====================================================================
# VWAP anchoring
# =====================================================================

def test_daily_vwap_does_not_depend_on_how_much_history_was_fetched():
    """
    The same bar used to return VWAP 149.90 / 153.43 / 155.26 for count
    50 / 100 / 250, because the accumulation was anchored to the first bar of
    whatever window happened to be requested.
    """
    long = frame(100 + np.random.RandomState(1).randn(250).cumsum() + 50)
    values = [ind.calculate_vwap(long.tail(n).reset_index(drop=True)).iloc[-1]
              for n in (50, 100, 250)]
    assert max(values) - min(values) < 1e-9


def test_daily_vwap_tracks_price_rather_than_drifting_away():
    d = frame(100 + np.random.RandomState(5).randn(250).cumsum() + 200)
    vwap = ind.calculate_vwap(d).iloc[-1]
    close = d["close"].iloc[-1]
    assert abs(vwap - close) / close < 0.25, "a 20-bar VWAP should sit near price"


def test_intraday_vwap_resets_each_session():
    d = frame(100 + np.random.RandomState(2).randn(120).cumsum() * 0.1, intraday=True)
    vwap = ind.calculate_vwap(d)
    session = pd.to_datetime(d["time"]).dt.date
    tp = (d["high"] + d["low"] + d["close"]) / 3
    for s in sorted(set(session)):
        mask = session == s
        # The first bar of a session has only itself in the average.
        assert vwap[mask].iloc[0] == pytest.approx(tp[mask].iloc[0])


def test_vwap_bands_straddle_the_vwap():
    d = noisy(80)
    b = ind.calculate_vwap_bands(d)
    assert (b["vwap_lower"].dropna() <= b["vwap"].dropna()).all()
    assert (b["vwap_upper"].dropna() >= b["vwap"].dropna()).all()


# =====================================================================
# Warm-up honesty
# =====================================================================

def test_consensus_is_undefined_during_warmup():
    """
    EMA(50) and MACD are *defined* from bar 0 but meaningless until they have
    seen their period. Scoring them anyway opened the series at a confident
    -3.75 (strong sell) that the backtester then traded on.
    """
    c = ind.calculate_adaptive_consensus(noisy(120))
    assert c.iloc[:ind.CONSENSUS_WARMUP_BARS].isna().all()
    assert c.iloc[ind.CONSENSUS_WARMUP_BARS:].notna().any()


def test_consensus_stays_in_range():
    c = ind.calculate_adaptive_consensus(noisy(200)).dropna()
    assert c.between(-5.0, 5.0).all()


def test_consensus_is_bullish_on_a_sustained_advance():
    c = ind.calculate_adaptive_consensus(frame([100 * (1.01 ** i) for i in range(150)]))
    assert c.iloc[-1] > 0, "an unbroken uptrend must not score bearish"


def test_supertrend_first_bar_is_not_a_fabricated_price():
    st = ind.calculate_supertrend(noisy(60))
    assert pd.isna(st["supertrend"].iloc[0]), "bar 0 had no band; 0.0 plotted as a real level"
    assert pd.isna(st["supertrend_dir"].iloc[0])


def test_supertrend_direction_is_only_ever_plus_or_minus_one():
    st = ind.calculate_supertrend(noisy(80))["supertrend_dir"].dropna()
    assert set(st.unique()) <= {1, -1}


# =====================================================================
# The full pipeline
# =====================================================================

def test_calculate_all_indicators_is_column_complete():
    res = ind.calculate_all_indicators(noisy(250))
    for col in ("rsi_14", "macd", "macd_signal", "bb_upper", "bb_lower", "vwap",
                "supertrend", "supertrend_dir", "adx", "atr_14", "regime",
                "consensus_score", "stoch_k", "mfi_14", "obv", "ichimoku_base"):
        assert col in res.columns, f"missing {col}"


def test_calculate_all_indicators_does_not_recompute_expensive_parts(monkeypatch):
    """
    Regime and consensus used to rebuild their inputs from scratch, so a single
    call ran EMA 17x, Bollinger 4x, ADX 3x, and SuperTrend — a per-bar Python
    loop — twice.
    """
    counts = {}
    for name in ("calculate_supertrend", "calculate_adx", "calculate_bollinger_bands", "calculate_macd"):
        original = getattr(ind, name)

        def make(orig, key):
            def wrapper(*a, **k):
                counts[key] = counts.get(key, 0) + 1
                return orig(*a, **k)
            return wrapper

        monkeypatch.setattr(ind, name, make(original, name))

    ind.calculate_all_indicators(noisy(120))

    assert counts["calculate_supertrend"] == 1
    assert counts["calculate_adx"] == 1
    assert counts["calculate_bollinger_bands"] == 1
    assert counts["calculate_macd"] == 1


def test_indicators_survive_a_short_series():
    """Fewer bars than the longest period must not raise."""
    res = ind.calculate_all_indicators(noisy(12))
    assert len(res) == 12
    assert res["consensus_score"].isna().all()
