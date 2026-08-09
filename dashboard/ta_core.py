"""
Canonical technical-analysis core — vendored from Powerstation (vivlos.ta.core).

Adopted rather than rewritten because it is pinned by a shared golden-vector
suite that a second, independent runtime (the JS twin in Powerstation's chart
engine) must also satisfy. Our own indicators.py has no such cross-runtime
check: if its RSI drifts, nothing notices. These 96 callables are held to 112
vectors in tests/fixtures/ta_golden_vectors.json, run by
tests/test_ta_conformance.py on every commit.

Vendored verbatim on 2026-08-09 from extract-chart-dsl/vivlos/ta/core.py.
Do not edit in place -- a local change would silently break the parity that is
the entire reason for taking it. Re-vendor from upstream instead, and let the
conformance suite prove the new copy still agrees.

Upstream contract, preserved: ndarray in -> ndarray out, index-aligned, NaN
warmup, never raises on short or empty input, never mutates its inputs.
"""

from __future__ import annotations

import math

import numpy as np

# fn name -> callable. Populated as indicators are implemented.
REGISTRY: dict[str, object] = {}


def _asf(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def _valid_period(period, n) -> bool:
    return isinstance(period, (int, np.integer)) and int(period) >= 1 and n > 0


def sma(close, period: int):
    """Simple moving average: mean of the last ``period`` values; NaN before index period-1.
    NaN-tolerant (ignores NaN in the window; all-NaN window -> NaN)."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    for i in range(p - 1, n):
        w = x[i - p + 1 : i + 1]
        m = w[~np.isnan(w)]
        out[i] = m.mean() if m.size else np.nan
    return out


def ema(close, period: int):
    """Exponential MA: alpha=2/(period+1), ema[0]=x[0], ema[i]=alpha*x[i]+(1-alpha)*ema[i-1].
    Defined from bar 0 (no NaN warmup) — matches pandas ewm(span=period, adjust=False)."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    alpha = 2.0 / (int(period) + 1.0)
    prev = x[0]
    out[0] = prev
    for i in range(1, n):
        prev = alpha * x[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def wma(close, period: int):
    """Linear weighted MA, weights 1..period (newest heaviest); NaN before index period-1."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    w = np.arange(1, p + 1, dtype=float)
    sw = w.sum()
    for i in range(p - 1, n):
        out[i] = float(np.dot(x[i - p + 1 : i + 1], w) / sw)
    return out


def rma(close, period: int):
    """Wilder smoothing (RSI/ATR basis): alpha=1/period; seed rma[period-1]=SMA(first period);
    rma[i]=alpha*x[i]+(1-alpha)*rma[i-1] for i>=period; NaN before period-1."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n) or n < p:
        return out
    a = 1.0 / p
    out[p - 1] = x[:p].mean()
    for i in range(p, n):
        out[i] = a * x[i] + (1.0 - a) * out[i - 1]
    return out


def stdev(close, period: int):
    """Rolling population standard deviation (ddof=0); NaN before index period-1."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    for i in range(p - 1, n):
        out[i] = float(np.std(x[i - p + 1 : i + 1]))
    return out


def change(close, length: int = 1):
    """x[i]-x[i-length]; NaN before index length."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    ln = int(length)
    if ln < 1:
        return out
    for i in range(ln, n):
        out[i] = x[i] - x[i - ln]
    return out


def roc(close, period: int):
    """Rate of change: 100*(x[i]-x[i-period])/x[i-period]; NaN before period; NaN if divisor 0."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    for i in range(p, n):
        d = x[i - p]
        out[i] = 100.0 * (x[i] - d) / d if d != 0 else np.nan
    return out


def rsi(close, period: int):
    """Wilder RSI in [0,100]. Simple-mean seed of the first ``period`` diffs, then Wilder smoothing.
    rsi=100-100/(1+avgGain/avgLoss); avgLoss==0 & avgGain>0 -> 100; both 0 -> NaN. First value at
    index ``period``; NaN for indices 0..period."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n) or n <= p:
        return out
    d = np.nan_to_num(np.diff(x), nan=0.0)  # d[k]=x[k+1]-x[k]; a NaN diff is 0 movement (Pine/JS na-skip)
    gains = np.clip(d, 0.0, None)
    losses = np.clip(-d, 0.0, None)

    def _val(ag, al):
        if al == 0.0 and ag == 0.0:
            return np.nan
        if al == 0.0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    ag = gains[:p].mean()
    al = losses[:p].mean()
    out[p] = _val(ag, al)
    a = 1.0 / p
    for i in range(p + 1, n):
        ag = a * gains[i - 1] + (1.0 - a) * ag
        al = a * losses[i - 1] + (1.0 - a) * al
        out[i] = _val(ag, al)
    return out


def highest(close, period: int):
    """Rolling max over ``period``; NaN before index period-1. NaN values in the window are skipped
    (Pine/JS na-tolerant), never propagated; an all-NaN window yields -inf (JS `m` stays -Infinity)."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    for i in range(p - 1, n):
        w = x[i - p + 1 : i + 1]
        finite = w[~np.isnan(w)]  # NaN skipped (JS `x[j] > m` is false for NaN), not propagated
        out[i] = float(finite.max()) if finite.size else -np.inf
    return out


def lowest(close, period: int):
    """Rolling min over ``period``; NaN before index period-1. NaN values in the window are skipped
    (Pine/JS na-tolerant), never propagated; an all-NaN window yields +inf (JS `m` stays +Infinity)."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    for i in range(p - 1, n):
        w = x[i - p + 1 : i + 1]
        finite = w[~np.isnan(w)]  # NaN skipped (JS `x[j] < m` is false for NaN), not propagated
        out[i] = float(finite.min()) if finite.size else np.inf
    return out


def _cross(a, b, over: bool):
    a = _asf(a)
    b = _asf(b)
    n = a.size
    out = np.zeros(n)
    for i in range(1, n):
        if np.isnan(a[i]) or np.isnan(b[i]) or np.isnan(a[i - 1]) or np.isnan(b[i - 1]):
            continue
        if over and a[i] > b[i] and a[i - 1] <= b[i - 1]:
            out[i] = 1.0
        elif (not over) and a[i] < b[i] and a[i - 1] >= b[i - 1]:
            out[i] = 1.0
    return out


def crossover(a, b):
    """1 iff a[i]>b[i] and a[i-1]<=b[i-1]; out[0]=0; NaN in either at i or i-1 -> 0."""
    return _cross(a, b, True)


def crossunder(a, b):
    """1 iff a[i]<b[i] and a[i-1]>=b[i-1]; out[0]=0; NaN in either at i or i-1 -> 0."""
    return _cross(a, b, False)


def atr(high, low, close, period: int):
    """Average True Range (Wilder). TR[0]=high-low; TR[i]=max(h-l,|h-prevClose|,|l-prevClose|);
    atr=rma(TR, period). NaN before index period-1."""
    h, low_, c = _asf(high), _asf(low), _asf(close)
    n = h.size
    if not (n == low_.size == c.size):
        raise ValueError("high/low/close must be the same length")
    if n == 0:
        return np.full(0, np.nan)
    tr = np.empty(n)
    tr[0] = h[0] - low_[0]
    for i in range(1, n):
        tr[i] = max(h[i] - low_[i], abs(h[i] - c[i - 1]), abs(low_[i] - c[i - 1]))
    return rma(tr, period)


def vwap(high, low, close, volume):
    """Cumulative (session-anchored) VWAP of the typical price (h+l+c)/3:
    cumsum(tp*vol)/cumsum(vol); NaN where cumulative volume is 0."""
    h, low_, c, v = _asf(high), _asf(low), _asf(close), _asf(volume)
    tp = (h + low_ + c) / 3.0
    cpv = np.cumsum(tp * v)
    cv = np.cumsum(v)
    return np.where(cv != 0.0, cpv / cv, np.nan)


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD: macd=ema(fast)-ema(slow); signal=ema(macd,signal); histogram=macd-signal. Uses the
    canonical EMA (defined from bar 0), so all three series are defined from bar 0."""
    x = _asf(close)
    macd_line = ema(x, fast) - ema(x, slow)
    sig = ema(macd_line, signal)
    return {"macd": macd_line, "signal": sig, "histogram": macd_line - sig}


def bollinger(close, period: int = 20, mult: float = 2.0):
    """Bollinger bands: mid=SMA(period); sd=stdev(period) (ddof=0); upper/lower=mid±mult*sd."""
    x = _asf(close)
    mid = sma(x, period)
    sd = stdev(x, period)
    return {"mid": mid, "upper": mid + mult * sd, "lower": mid - mult * sd}


# ── P5-expand wave 1: breadth toward the TradingView-class canon ──


def _safe_div(num, den):
    """Elementwise num/den, NaN where den==0 (no inf, no warnings) — matches the golden oracle."""
    num = _asf(num)
    den = _asf(den)
    return np.divide(num, den, out=np.full_like(num, np.nan), where=den != 0)


def tr(high, low, close):
    """True Range: TR[0]=high-low; TR[i]=max(h-l, |h-prevClose|, |l-prevClose|)."""
    h, low_, c = _asf(high), _asf(low), _asf(close)
    n = h.size
    if n == 0:
        return np.full(0, np.nan)
    out = np.empty(n)
    out[0] = h[0] - low_[0]
    for i in range(1, n):
        out[i] = max(h[i] - low_[i], abs(h[i] - c[i - 1]), abs(low_[i] - c[i - 1]))
    return out


def dema(close, period: int):
    """Double EMA: 2*ema - ema(ema). Defined from bar 0 (EMA has no warmup)."""
    e1 = ema(close, period)
    return 2.0 * e1 - ema(e1, period)


def tema(close, period: int):
    """Triple EMA: 3*e1 - 3*e2 + e3 where e2=ema(e1), e3=ema(e2)."""
    e1 = ema(close, period)
    e2 = ema(e1, period)
    e3 = ema(e2, period)
    return 3.0 * e1 - 3.0 * e2 + e3


def hma(close, period: int):
    """Hull MA: wma(2*wma(x, floor(p/2)) - wma(x, p), floor(sqrt(p)))."""
    p = int(period)
    raw = 2.0 * wma(close, p // 2) - wma(close, p)
    return wma(raw, int(math.isqrt(p)))


def vwma(close, volume, period: int):
    """Volume-weighted MA: sma(close*volume, p) / sma(volume, p); NaN where the volume sum is 0."""
    c = _asf(close)
    v = _asf(volume)
    return _safe_div(sma(c * v, period), sma(v, period))


def mom(close, period: int):
    """Momentum: close[i] - close[i-period]; NaN before index period."""
    return change(close, period)


def cmo(close, period: int):
    """Chande Momentum Oscillator: 100*(sumGain-sumLoss)/(sumGain+sumLoss) over the last ``period``
    diffs; NaN before ``period``; NaN when total movement is 0."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n) or n <= p:
        return out
    d = np.nan_to_num(np.diff(x), nan=0.0)  # a NaN diff is 0 movement (Pine/JS na-skip), not propagated
    g = np.clip(d, 0.0, None)
    ls = np.clip(-d, 0.0, None)
    for i in range(p, n):
        sg = g[i - p : i].sum()
        sl = ls[i - p : i].sum()
        out[i] = 100.0 * (sg - sl) / (sg + sl) if (sg + sl) != 0.0 else np.nan
    return out


def williams_r(high, low, close, period: int):
    """Williams %R: -100*(highest(high,p) - close)/(highest(high,p) - lowest(low,p)); NaN on a 0 range."""
    c = _asf(close)
    hh = highest(high, period)
    ll = lowest(low, period)
    return _safe_div(-100.0 * (hh - c), hh - ll)


def cci(high, low, close, period: int):
    """Commodity Channel Index: tp=(h+l+c)/3; (tp - sma(tp,p))/(0.015*mean|tp-sma|); NaN on 0 dev."""
    h, low_, c = _asf(high), _asf(low), _asf(close)
    tp = (h + low_ + c) / 3.0
    smt = sma(tp, period)
    n = tp.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n):
        return out
    for i in range(p - 1, n):
        md = float(np.mean(np.abs(tp[i - p + 1 : i + 1] - smt[i])))
        out[i] = (tp[i] - smt[i]) / (0.015 * md) if md != 0.0 else np.nan
    return out


def stochastic(high, low, close, k_period: int = 14, d_period: int = 3):
    """Stochastic oscillator: %K = 100*(close - lowest(low,k))/(highest(high,k) - lowest(low,k));
    %D = sma(%K, d_period). Returns ``{"k", "d"}``."""
    c = _asf(close)
    hh = highest(high, k_period)
    ll = lowest(low, k_period)
    k = _safe_div(100.0 * (c - ll), hh - ll)
    # %D is a STRICT SMA of %K — NaN if any value in the window is NaN (so %D stays undefined through
    # %K's warmup, the standard convention), unlike the NaN-tolerant core `sma`.
    n = k.size
    d = np.full(n, np.nan)
    dp = int(d_period)
    if dp >= 1:
        for i in range(dp - 1, n):
            w = k[i - dp + 1 : i + 1]
            d[i] = float(w.mean()) if not np.isnan(w).any() else np.nan
    return {"k": k, "d": d}


def obv(close, volume):
    """On-Balance Volume: cumulative sign(change)*volume; obv[0]=0."""
    c = _asf(close)
    v = _asf(volume)
    n = c.size
    out = np.zeros(n)
    for i in range(1, n):
        vi = v[i] if i < v.size else np.nan  # tolerate short volume (JS reads out-of-range as NaN)
        if c[i] > c[i - 1]:
            out[i] = out[i - 1] + vi
        elif c[i] < c[i - 1]:
            out[i] = out[i - 1] - vi
        else:
            out[i] = out[i - 1]
    return out


def donchian(high, low, period: int):
    """Donchian channel: upper=highest(high,p), lower=lowest(low,p), basis=(upper+lower)/2.
    Returns ``{"upper", "lower", "basis"}``."""
    up = highest(high, period)
    lw = lowest(low, period)
    return {"upper": up, "lower": lw, "basis": (up + lw) / 2.0}


# ── P5-expand wave 2 ──


def _rma_from_first_valid(x, period: int):
    """Wilder average that seeds at the first ``period`` NON-NaN values (for ADX = rma of DX, where DX
    itself has a NaN warmup). NaN until the seed index."""
    x = _asf(x)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    valid = np.flatnonzero(~np.isnan(x))
    if valid.size < p or valid[0] + p > n:
        return out
    seed = valid[0] + p - 1
    out[seed] = x[valid[0] : valid[0] + p].mean()
    a = 1.0 / p
    for i in range(seed + 1, n):
        out[i] = a * x[i] + (1.0 - a) * out[i - 1]
    return out


def adx(high, low, close, period: int = 14):
    """Wilder ADX + directional indicators. Returns ``{"adx","plus_di","minus_di"}``. +DM/-DM smoothed
    by Wilder rma over TR; DX = 100*|+DI - -DI|/(+DI + -DI); ADX = Wilder average of DX from its first
    valid bar (standard 2*period-2 warmup)."""
    h, low_, c = _asf(high), _asf(low), _asf(close)
    n = h.size
    pdm = np.zeros(n)
    mdm = np.zeros(n)
    for i in range(1, n):
        up = h[i] - h[i - 1]
        dn = low_[i - 1] - low_[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    atr_s = rma(tr(h, low_, c), period)
    plus_di = _safe_div(100.0 * rma(pdm, period), atr_s)
    minus_di = _safe_div(100.0 * rma(mdm, period), atr_s)
    dx = _safe_div(100.0 * np.abs(plus_di - minus_di), plus_di + minus_di)
    return {"adx": _rma_from_first_valid(dx, period), "plus_di": plus_di, "minus_di": minus_di}


def aroon(high, low, period: int = 14):
    """Aroon up/down/oscillator over a (period+1)-bar window. Returns ``{"up","down","osc"}``."""
    h, low_ = _asf(high), _asf(low)
    n = h.size
    up = np.full(n, np.nan)
    down = np.full(n, np.nan)
    p = int(period)
    for i in range(p, n):
        since_high = p - int(np.argmax(h[i - p : i + 1]))
        since_low = p - int(np.argmin(low_[i - p : i + 1]))
        up[i] = 100.0 * (p - since_high) / p
        down[i] = 100.0 * (p - since_low) / p
    return {"up": up, "down": down, "osc": up - down}


def keltner(high, low, close, period: int = 20, mult: float = 2.0):
    """Keltner channel: mid=ema(close,p); atr=Wilder rma(TR,p); upper/lower=mid±mult*atr.
    Returns ``{"mid","upper","lower"}``."""
    mid = ema(close, period)
    a = rma(tr(high, low, close), period)
    return {"mid": mid, "upper": mid + mult * a, "lower": mid - mult * a}


def mfi(high, low, close, volume, period: int = 14):
    """Money Flow Index: raw money flow tp*volume split by typical-price direction over ``period``;
    100-100/(1+posMF/negMF). NaN before period; negMF==0 & pos>0 -> 100; both 0 -> NaN."""
    h, low_, c, vol = _asf(high), _asf(low), _asf(close), _asf(volume)
    tp = (h + low_ + c) / 3.0
    rmf = tp * vol
    n = tp.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n) or n <= p:
        return out
    for i in range(p, n):
        pos = neg = 0.0
        for k in range(i - p + 1, i + 1):
            if tp[k] > tp[k - 1]:
                pos += rmf[k]
            elif tp[k] < tp[k - 1]:
                neg += rmf[k]
        if neg == 0.0 and pos == 0.0:
            out[i] = np.nan
        elif neg == 0.0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + pos / neg)
    return out


def _mfv(high, low, close, volume):
    """Money-flow volume: ((c-l)-(h-c))/(h-l)*volume; 0 where high==low."""
    h, low_, c, vol = _asf(high), _asf(low), _asf(close), _asf(volume)
    rng = h - low_
    mult = np.divide((c - low_) - (h - c), rng, out=np.zeros_like(c), where=rng != 0)
    return mult * vol


def cmf(high, low, close, volume, period: int = 20):
    """Chaikin Money Flow: sum(mfv,p)/sum(volume,p); NaN before period-1 or when the volume sum is 0.
    (The rolling sums are ``sma*p``; the ``p`` cancels in the ratio.)"""
    mfv = _mfv(high, low, close, volume)
    return _safe_div(sma(mfv, period), sma(_asf(volume), period))


def ad(high, low, close, volume):
    """Accumulation/Distribution line: cumulative money-flow volume."""
    return np.cumsum(_mfv(high, low, close, volume))


def ao(high, low):
    """Awesome Oscillator: sma(hl2,5) - sma(hl2,34), hl2=(high+low)/2."""
    hl2 = (_asf(high) + _asf(low)) / 2.0
    return sma(hl2, 5) - sma(hl2, 34)


def ppo(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """Percentage Price Oscillator: 100*(ema(fast)-ema(slow))/ema(slow); signal=ema(ppo,signal);
    histogram=ppo-signal. Returns ``{"ppo","signal","histogram"}``."""
    x = _asf(close)
    slow_ema = ema(x, slow)
    line = _safe_div(100.0 * (ema(x, fast) - slow_ema), slow_ema)
    sig = ema(line, signal)
    return {"ppo": line, "signal": sig, "histogram": line - sig}


def natr(high, low, close, period: int = 14):
    """Normalized ATR: 100 * atr(period) / close."""
    return _safe_div(100.0 * atr(high, low, close, period), _asf(close))


def linreg(close, period: int = 14):
    """Linear-regression value: the least-squares line over the last ``period`` bars, evaluated at the
    current bar. NaN before index period-1."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n):
        return out
    xs = np.arange(p, dtype=float)
    for i in range(p - 1, n):
        b, a0 = np.polyfit(xs, x[i - p + 1 : i + 1], 1)
        out[i] = a0 + b * (p - 1)
    return out


def trix(close, period: int = 15):
    """TRIX: 10000 * the 1-bar rate of change of a triple-smoothed EMA."""
    t = ema(ema(ema(close, period), period), period)
    n = t.size
    out = np.full(n, np.nan)
    for i in range(1, n):
        out[i] = 10000.0 * (t[i] - t[i - 1]) / t[i - 1] if t[i - 1] != 0.0 else np.nan
    return out


def tsi(close, long: int = 25, short: int = 13):
    """True Strength Index: double-smoothed momentum. pc = 1-bar change (pc[0]=0);
    100 * ema(ema(pc,long),short) / ema(ema(|pc|,long),short)."""
    x = _asf(close)
    n = x.size
    pc = np.zeros(n)
    for i in range(1, n):
        pc[i] = x[i] - x[i - 1]
    ds1 = ema(ema(pc, long), short)
    ds2 = ema(ema(np.abs(pc), long), short)
    return _safe_div(100.0 * ds1, ds2)


# ── P5-expand wave 3 ──


def variance(close, period: int):
    """Rolling population variance (ddof=0); NaN before index period-1."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    for i in range(p - 1, n):
        out[i] = float(np.var(x[i - p + 1 : i + 1]))
    return out


def median(close, period: int):
    """Rolling median over ``period``; NaN before index period-1. NaN-tolerant: non-finite values are
    filtered out of the window and the median is taken over the finite remainder (the same honest-NaN
    behavior as ``sma``/``variance``); a window with no finite value yields NaN. This does NOT rely on
    any sort side-effect for NaN ordering — the previous implementation emulated a "NaN sorts to the
    front" V8 assumption that only holds for a leading-NaN prefix, so an INTERIOR NaN diverged from the
    JS twin. Filtering explicitly makes both runtimes byte-identical for every NaN arrangement."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    for i in range(p - 1, n):
        w = x[i - p + 1 : i + 1]
        finite = np.sort(w[np.isfinite(w)])
        if finite.size == 0:
            continue
        m = finite.size // 2
        out[i] = float(finite[m]) if finite.size % 2 else float((finite[m - 1] + finite[m]) / 2.0)
    return out


def percentrank(close, period: int):
    """Percent rank (Pine ta.percentrank): 100 * count of the prior ``period`` values <= the current
    value / period; NaN before index ``period``."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    for i in range(p, n):
        out[i] = 100.0 * float(np.count_nonzero(x[i - p : i] <= x[i])) / p
    return out


def dpo(close, period: int):
    """Detrended Price Oscillator: close[i - (period//2 + 1)] - sma(close, period)[i]; NaN before
    period-1 (or where the shifted index is negative)."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    barsback = p // 2 + 1
    s = sma(x, p)
    for i in range(p - 1, n):
        j = i - barsback
        if j >= 0:
            out[i] = x[j] - s[i]
    return out


def uo(high, low, close):
    """Ultimate Oscillator (Larry Williams), periods 7/14/28, weights 4/2/1. BP=close-min(low,prevClose);
    TR=max(high,prevClose)-min(low,prevClose); avgN=sum(BP,N)/sum(TR,N); UO=100*(4*a7+2*a14+a28)/7.
    NaN before index 28."""
    h, low_, c = _asf(high), _asf(low), _asf(close)
    n = c.size
    bp = np.full(n, np.nan)
    trng = np.full(n, np.nan)
    for i in range(1, n):
        minlc = min(low_[i], c[i - 1])
        bp[i] = c[i] - minlc
        trng[i] = max(h[i], c[i - 1]) - minlc
    out = np.full(n, np.nan)
    for i in range(28, n):
        a7 = bp[i - 6 : i + 1].sum() / trng[i - 6 : i + 1].sum()
        a14 = bp[i - 13 : i + 1].sum() / trng[i - 13 : i + 1].sum()
        a28 = bp[i - 27 : i + 1].sum() / trng[i - 27 : i + 1].sum()
        out[i] = 100.0 * (4.0 * a7 + 2.0 * a14 + a28) / 7.0
    return out


def chop(high, low, close, period: int = 14):
    """Choppiness Index: 100*log10(sum(TR,p)/(highest(high,p)-lowest(low,p)))/log10(p); NaN before
    period-1 or on a zero range."""
    h, low_, c = _asf(high), _asf(low), _asf(close)
    n = c.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n) or p < 2:
        return out
    trng = tr(h, low_, c)
    lp = math.log10(p)
    for i in range(p - 1, n):
        rng = h[i - p + 1 : i + 1].max() - low_[i - p + 1 : i + 1].min()
        if rng > 0:
            out[i] = 100.0 * math.log10(trng[i - p + 1 : i + 1].sum() / rng) / lp
    return out


def eom(high, low, volume, period: int = 14):
    """Ease of Movement: strict SMA over ``period`` of ((hl2 change)*(high-low)/volume); NaN on zero
    volume; NaN before ``period`` (strict window — any NaN in the window -> NaN)."""
    h, low_, v = _asf(high), _asf(low), _asf(volume)
    n = h.size
    hl2 = (h + low_) / 2.0
    emv = np.full(n, np.nan)
    for i in range(1, n):
        emv[i] = (hl2[i] - hl2[i - 1]) * (h[i] - low_[i]) / v[i] if v[i] != 0.0 else np.nan
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n):
        return out
    for i in range(p - 1, n):
        w = emv[i - p + 1 : i + 1]
        out[i] = float(w.mean()) if not np.isnan(w).any() else np.nan
    return out


def pvt(close, volume):
    """Price Volume Trend: cumulative pvt[i]=pvt[i-1]+volume[i]*(close[i]-close[i-1])/close[i-1];
    pvt[0]=0; a zero prior close contributes 0."""
    c = _asf(close)
    v = _asf(volume)
    n = c.size
    out = np.zeros(n)
    for i in range(1, n):
        vi = v[i] if i < v.size else np.nan  # tolerate short volume (JS reads out-of-range as NaN)
        out[i] = out[i - 1] + (vi * (c[i] - c[i - 1]) / c[i - 1] if c[i - 1] != 0.0 else 0.0)
    return out


def cog(close, period: int = 10):
    """Ehlers Center of Gravity: -sum((k+1)*close[i-k]) / sum(close[i-k]) for k=0..period-1;
    NaN before index period-1 or when the price sum is 0."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    if not _valid_period(period, n):
        return out
    p = int(period)
    w = np.arange(1, p + 1, dtype=float)  # weight (k+1) for the newest-first window reversed below
    for i in range(p - 1, n):
        win = x[i - p + 1 : i + 1][::-1]  # win[k] = close[i-k]
        den = win.sum()
        out[i] = -float(np.dot(w, win)) / den if den != 0.0 else np.nan
    return out


def vortex(high, low, close, period: int = 14):
    """Vortex Indicator: VI+ = sum(|high-low[1]|, p)/sum(TR, p); VI- = sum(|low-high[1]|, p)/sum(TR, p).
    Returns ``{"plus","minus"}``. NaN before index ``period`` or on a zero TR sum."""
    h, low_, c = _asf(high), _asf(low), _asf(close)
    n = c.size
    trng = tr(h, low_, c)
    vmp = np.full(n, np.nan)
    vmm = np.full(n, np.nan)
    for i in range(1, n):
        vmp[i] = abs(h[i] - low_[i - 1])
        vmm[i] = abs(low_[i] - h[i - 1])
    plus = np.full(n, np.nan)
    minus = np.full(n, np.nan)
    p = int(period)
    if _valid_period(period, n):
        for i in range(p, n):
            strr = trng[i - p + 1 : i + 1].sum()
            if strr != 0.0:
                plus[i] = vmp[i - p + 1 : i + 1].sum() / strr
                minus[i] = vmm[i - p + 1 : i + 1].sum() / strr
    return {"plus": plus, "minus": minus}


def _strict_sma(x, period: int):
    """Rolling mean that is NaN if ANY value in the window is NaN (unlike the NaN-tolerant core sma)."""
    x = _asf(x)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    if p < 1:
        return out
    for i in range(p - 1, n):
        w = x[i - p + 1 : i + 1]
        out[i] = float(w.mean()) if not np.isnan(w).any() else np.nan
    return out


def stochrsi(close, rsi_length: int = 14, stoch_length: int = 14, k: int = 3, d: int = 3):
    """Stochastic RSI: stochastic of the Wilder RSI. raw = 100*(rsi - lowest(rsi, stoch_length)) /
    (highest(rsi, stoch_length) - lowest(rsi, stoch_length)); %K = strict SMA(raw, k); %D = strict
    SMA(%K, d). Returns ``{"k","d"}``. Strict smoothing -> NaN through the RSI/stoch warmup."""
    r = rsi(close, rsi_length)
    n = r.size
    raw = np.full(n, np.nan)
    sl = int(stoch_length)
    if sl >= 1:
        for i in range(sl - 1, n):
            w = r[i - sl + 1 : i + 1]
            if not np.isnan(w).any():
                lo, hi = float(w.min()), float(w.max())
                raw[i] = 100.0 * (r[i] - lo) / (hi - lo) if hi > lo else np.nan
    kk = _strict_sma(raw, k)
    return {"k": kk, "d": _strict_sma(kk, d)}


def supertrend(high, low, close, period: int = 10, mult: float = 3.0):
    """Supertrend: Wilder-ATR volatility bands carried forward. basicUpper/Lower = hl2 ± mult*atr;
    finalUpper/Lower ratchet; the trend line follows whichever band price respects, with direction
    +1 (uptrend, line=lower) / -1 (downtrend, line=upper). Seeds at index period-1 (direction -1).
    Returns ``{"trend","direction"}``; NaN before the seed."""
    h, low_, c = _asf(high), _asf(low), _asf(close)
    n = c.size
    trend = np.full(n, np.nan)
    direction = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n) or n < p:
        return {"trend": trend, "direction": direction}
    a = atr(h, low_, c, p)
    hl2 = (h + low_) / 2.0
    bu = hl2 + mult * a
    bl = hl2 - mult * a
    fu = np.full(n, np.nan)
    fl = np.full(n, np.nan)
    s = p - 1
    fu[s] = bu[s]
    fl[s] = bl[s]
    trend[s] = fu[s]
    direction[s] = -1.0
    for i in range(s + 1, n):
        fu[i] = bu[i] if (bu[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = bl[i] if (bl[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if trend[i - 1] == fu[i - 1]:  # was downtrend (line = upper band)
            if c[i] <= fu[i]:
                trend[i], direction[i] = fu[i], -1.0
            else:
                trend[i], direction[i] = fl[i], 1.0
        else:  # uptrend (line = lower band)
            if c[i] >= fl[i]:
                trend[i], direction[i] = fl[i], 1.0
            else:
                trend[i], direction[i] = fu[i], -1.0
    return {"trend": trend, "direction": direction}


# ── P5-expand wave 4 ──


def swma(close):
    """Symmetric weighted MA over 4 bars, weights [1,2,2,1]/6; NaN before index 3."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    for i in range(3, n):
        out[i] = (x[i - 3] + 2.0 * x[i - 2] + 2.0 * x[i - 1] + x[i]) / 6.0
    return out


def alma(close, period: int = 9, offset: float = 0.85, sigma: float = 6.0):
    """Arnaud Legoux MA: gaussian-weighted window, m=offset*(period-1), s=period/sigma,
    w[k]=exp(-(k-m)^2/(2 s^2)); NaN before index period-1."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n):
        return out
    m = offset * (p - 1)
    s = p / sigma
    w = np.exp(-((np.arange(p, dtype=float) - m) ** 2) / (2.0 * s * s))
    sw = w.sum()
    for i in range(p - 1, n):
        out[i] = float(np.dot(w, x[i - p + 1 : i + 1]) / sw)
    return out


def dev(close, period: int):
    """Mean absolute deviation from the SMA over ``period``; NaN before index period-1."""
    x = _asf(close)
    sm = sma(x, period)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n):
        return out
    for i in range(p - 1, n):
        out[i] = float(np.mean(np.abs(x[i - p + 1 : i + 1] - sm[i])))
    return out


def correlation(x, y, period: int):
    """Rolling Pearson correlation (population) of two series over ``period``; NaN before index
    period-1 or when either window has zero standard deviation."""
    a = _asf(x)
    b = _asf(y)
    n = a.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n) or b.size != n:
        return out
    for i in range(p - 1, n):
        aw = a[i - p + 1 : i + 1]
        bw = b[i - p + 1 : i + 1]
        ma, mb = aw.mean(), bw.mean()
        cov = float(np.mean((aw - ma) * (bw - mb)))
        sa = math.sqrt(float(np.mean((aw - ma) ** 2)))
        sb = math.sqrt(float(np.mean((bw - mb) ** 2)))
        out[i] = cov / (sa * sb) if sa > 0.0 and sb > 0.0 else np.nan
    return out


def bbw(close, period: int = 20, mult: float = 2.0):
    """Bollinger Band Width: 2*mult*stdev(period) / sma(period); NaN before period-1 or a zero basis."""
    x = _asf(close)
    return _safe_div(2.0 * mult * stdev(x, period), sma(x, period))


def percentb(close, period: int = 20, mult: float = 2.0):
    """Bollinger %b: (close - lower) / (upper - lower), bands = sma ± mult*stdev; NaN before period-1
    or on a flat band."""
    x = _asf(close)
    mid = sma(x, period)
    sd = stdev(x, period)
    upper = mid + mult * sd
    lower = mid - mult * sd
    return _safe_div(x - lower, upper - lower)


def cum(close):
    """Cumulative sum; a NaN input contributes 0 to the running total (Pine ta.cum)."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    run = 0.0
    for i in range(n):
        if not np.isnan(x[i]):
            run += x[i]
        out[i] = run
    return out


def rising(close, length: int):
    """1.0 if ``close`` is strictly increasing over the last ``length`` steps, else 0.0; 0.0 before
    index ``length``."""
    x = _asf(close)
    n = x.size
    out = np.zeros(n)
    ln = int(length)
    if ln < 1:
        return out
    for i in range(ln, n):
        if all(x[i - k] > x[i - k - 1] for k in range(ln)):
            out[i] = 1.0
    return out


def falling(close, length: int):
    """1.0 if ``close`` is strictly decreasing over the last ``length`` steps, else 0.0; 0.0 before
    index ``length``."""
    x = _asf(close)
    n = x.size
    out = np.zeros(n)
    ln = int(length)
    if ln < 1:
        return out
    for i in range(ln, n):
        if all(x[i - k] < x[i - k - 1] for k in range(ln)):
            out[i] = 1.0
    return out


def coppock(close, roc_long: int = 14, roc_short: int = 11, wma_length: int = 10):
    """Coppock Curve: wma(roc(close, roc_long) + roc(close, roc_short), wma_length)."""
    x = _asf(close)
    return wma(roc(x, roc_long) + roc(x, roc_short), wma_length)


def elderray(high, low, close, period: int = 13):
    """Elder Ray: bull = high - ema(close, period); bear = low - ema(close, period). Both defined from
    bar 0 (EMA has no warmup). Returns ``{"bull","bear"}``."""
    e = ema(close, period)
    return {"bull": _asf(high) - e, "bear": _asf(low) - e}


def fisher(high, low, period: int = 9):
    """Ehlers Fisher Transform of hl2. value = 0.66*(2*normalized-1) + 0.67*prev (clamped ±0.999);
    fisher = 0.5*ln((1+value)/(1-value)) + 0.5*prev_fisher; trigger = previous fisher. Returns
    ``{"fisher","trigger"}``; NaN before index period-1."""
    h, low_ = _asf(high), _asf(low)
    price = (h + low_) / 2.0
    n = price.size
    fish = np.full(n, np.nan)
    trigger = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n) or n < p:
        return {"fisher": fish, "trigger": trigger}
    maxh = highest(price, p)
    minl = lowest(price, p)
    v = 0.0
    fprev = 0.0
    started = False
    for i in range(p - 1, n):
        rng = maxh[i] - minl[i]
        raw = ((price[i] - minl[i]) / rng - 0.5) if rng > 0.0 else 0.0
        v = 0.33 * 2.0 * raw + 0.67 * v
        v = max(-0.999, min(0.999, v))
        f = 0.5 * math.log((1.0 + v) / (1.0 - v)) + 0.5 * fprev
        if started:
            trigger[i] = fprev
        fish[i] = f
        fprev = f
        started = True
    return {"fisher": fish, "trigger": trigger}


# ── P5-expand wave 5 ──


def psar(high, low, af0: float = 0.02, af_step: float = 0.02, af_max: float = 0.2):
    """Wilder Parabolic SAR (high/low only). Seeds at index 1 by the first two bars' midpoint direction
    (uptrend if hl2[1] >= hl2[0]); AF starts at af0, steps by af_step to af_max on each new extreme;
    reverses when price crosses the SAR, which is clamped to the prior two bars' low (up) / high (down).
    NaN at index 0."""
    h, low_ = _asf(high), _asf(low)
    n = h.size
    out = np.full(n, np.nan)
    if n < 2:
        return out
    up = (h[1] + low_[1]) >= (h[0] + low_[0])
    if up:
        sar, ep = low_[0], h[1]
    else:
        sar, ep = h[0], low_[1]
    af = af0
    out[1] = sar
    for i in range(2, n):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, low_[i - 1], low_[i - 2])
            if low_[i] < sar:
                up, sar, ep, af = False, ep, low_[i], af0
            elif h[i] > ep:
                ep, af = h[i], min(af + af_step, af_max)
        else:
            sar = max(sar, h[i - 1], h[i - 2])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], af0
            elif low_[i] < ep:
                ep, af = low_[i], min(af + af_step, af_max)
        out[i] = sar
    return out


def bop(open, high, low, close):  # noqa: A002 - `open` mirrors the OHLC field name
    """Balance of Power: (close - open) / (high - low); NaN on a zero range."""
    o, h, low_, c = _asf(open), _asf(high), _asf(low), _asf(close)
    return _safe_div(c - o, h - low_)


def pvi(close, volume):
    """Positive Volume Index (base 1000): updates by the price return only on bars where volume rose."""
    c, v = _asf(close), _asf(volume)
    n = c.size
    out = np.full(n, np.nan)
    if n == 0:
        return out
    out[0] = 1000.0
    for i in range(1, n):
        vi = v[i] if i < v.size else np.nan  # tolerate short volume (JS: out-of-range compares false)
        vp = v[i - 1] if i - 1 < v.size else np.nan
        if vi > vp and c[i - 1] != 0:
            out[i] = out[i - 1] * (1.0 + (c[i] - c[i - 1]) / c[i - 1])
        else:
            out[i] = out[i - 1]
    return out


def nvi(close, volume):
    """Negative Volume Index (base 1000): updates by the price return only on bars where volume fell."""
    c, v = _asf(close), _asf(volume)
    n = c.size
    out = np.full(n, np.nan)
    if n == 0:
        return out
    out[0] = 1000.0
    for i in range(1, n):
        vi = v[i] if i < v.size else np.nan  # tolerate short volume (JS: out-of-range compares false)
        vp = v[i - 1] if i - 1 < v.size else np.nan
        if vi < vp and c[i - 1] != 0:
            out[i] = out[i - 1] * (1.0 + (c[i] - c[i - 1]) / c[i - 1])
        else:
            out[i] = out[i - 1]
    return out


def massindex(high, low, period: int = 25, ema_period: int = 9):
    """Mass Index: rolling sum over ``period`` of ema(range, ema_period) / ema(ema(range, ema_period),
    ema_period), range=high-low. NaN before index period-1."""
    h, low_ = _asf(high), _asf(low)
    rng = h - low_
    e1 = ema(rng, ema_period)
    ratio = _safe_div(e1, ema(e1, ema_period))
    n = h.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n):
        return out
    for i in range(p - 1, n):
        out[i] = float(ratio[i - p + 1 : i + 1].sum())
    return out


def tsf(close, period: int = 14):
    """Time Series Forecast: the least-squares line over the last ``period`` bars, evaluated ONE bar
    beyond the window end (linreg projected forward by one). NaN before index period-1."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n):
        return out
    xs = np.arange(p, dtype=float)
    for i in range(p - 1, n):
        b, a0 = np.polyfit(xs, x[i - p + 1 : i + 1], 1)
        out[i] = a0 + b * p
    return out


def vhf(close, period: int = 28):
    """Vertical Horizontal Filter: |highest(close,p) - lowest(close,p)| / sum(|close changes|, p);
    NaN before index ``period`` or a zero denominator."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(period)
    if not _valid_period(period, n):
        return out
    for i in range(p, n):
        w = x[i - p + 1 : i + 1]
        num = abs(w.max() - w.min())
        den = float(np.sum(np.abs(np.diff(x[i - p : i + 1]))))
        out[i] = num / den if den != 0.0 else np.nan
    return out


def chaikin(high, low, close, volume, fast: int = 3, slow: int = 10):
    """Chaikin Oscillator: ema(ad, fast) - ema(ad, slow), ad = accumulation/distribution line."""
    line = ad(high, low, close, volume)
    return ema(line, fast) - ema(line, slow)


def midprice(high, low, period: int = 14):
    """Midprice: (highest(high, period) + lowest(low, period)) / 2; NaN before index period-1."""
    return (highest(high, period) + lowest(low, period)) / 2.0


def wad(high, low, close):
    """Williams Accumulation/Distribution (cumulative): on an up close add close-min(low,prevClose);
    on a down close add close-max(high,prevClose); flat adds 0. wad[0]=0."""
    h, low_, c = _asf(high), _asf(low), _asf(close)
    n = c.size
    out = np.zeros(n)
    for i in range(1, n):
        if c[i] > c[i - 1]:
            move = c[i] - min(low_[i], c[i - 1])
        elif c[i] < c[i - 1]:
            move = c[i] - max(h[i], c[i - 1])
        else:
            move = 0.0
        out[i] = out[i - 1] + move
    return out


def alligator(high, low, jaw: int = 13, teeth: int = 8, lips: int = 5):
    """Bill Williams Alligator: Wilder-smoothed (rma) median price at three lengths. Returns
    ``{"jaw","teeth","lips"}`` UNSHIFTED (Pine offsets them forward for display; we plot the raw lines)."""
    median = (_asf(high) + _asf(low)) / 2.0
    return {"jaw": rma(median, jaw), "teeth": rma(median, teeth), "lips": rma(median, lips)}


def kst(
    close,
    roc1: int = 10,
    roc2: int = 15,
    roc3: int = 20,
    roc4: int = 30,
    sma1: int = 10,
    sma2: int = 10,
    sma3: int = 10,
    sma4: int = 15,
    signal: int = 9,
):
    """Know Sure Thing: 1*rcma1 + 2*rcma2 + 3*rcma3 + 4*rcma4 where rcma_i = strict SMA of roc(close,
    roc_i) over sma_i; signal = strict SMA(kst, signal). Returns ``{"kst","signal"}``. (Strict SMA ->
    well-defined warmup, unlike the NaN-tolerant core sma.)"""
    x = _asf(close)
    line = (
        _strict_sma(roc(x, roc1), sma1)
        + 2.0 * _strict_sma(roc(x, roc2), sma2)
        + 3.0 * _strict_sma(roc(x, roc3), sma3)
        + 4.0 * _strict_sma(roc(x, roc4), sma4)
    )
    return {"kst": line, "signal": _strict_sma(line, signal)}


# ── Capability: array reductions (Family A6) — series-in/series-out scans, no bar cursor ──


def rolling_sum(close, length: int):
    """Rolling sum over ``length``; NaN before index length-1 (or if the window holds a NaN)."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(length)
    if not _valid_period(length, n):
        return out
    for i in range(p - 1, n):
        w = x[i - p + 1 : i + 1]
        out[i] = np.nan if np.isnan(w).any() else float(w.sum())
    return out


def barssince(cond):
    """Bars since ``cond`` (nonzero) was last true; NaN before the first true bar."""
    c = _asf(cond)
    n = c.size
    out = np.full(n, np.nan)
    last = None
    for i in range(n):
        if c[i] != 0 and not np.isnan(c[i]):
            last = i
        if last is not None:
            out[i] = i - last
    return out


def valuewhen(cond, src, occurrence: int = 0):
    """Value of ``src`` on the bar where ``cond`` was true, ``occurrence`` truths ago (0 = most recent);
    NaN until that many truths exist."""
    c = _asf(cond)
    s = _asf(src)
    n = c.size
    out = np.full(n, np.nan)
    occ = int(occurrence)
    trues: list[int] = []
    for i in range(n):
        if c[i] != 0 and not np.isnan(c[i]):
            trues.append(i)
        if len(trues) > occ:
            out[i] = s[trues[-1 - occ]]
    return out


def pivothigh(src, left: int = 2, right: int = 2):
    """Pivot high: the value at ``right`` bars back if it is strictly greater than the ``left`` bars
    before it and the ``right`` bars after it; placed at the confirming bar. NaN otherwise."""
    x = _asf(src)
    n = x.size
    out = np.full(n, np.nan)
    lft, rgt = int(left), int(right)
    for i in range(lft + rgt, n):
        c = i - rgt
        v = x[c]
        if all(v > x[j] for j in range(c - lft, c)) and all(
            v > x[j] for j in range(c + 1, c + rgt + 1)
        ):
            out[i] = v
    return out


def pivotlow(src, left: int = 2, right: int = 2):
    """Pivot low: the value at ``right`` bars back if it is strictly less than the ``left`` bars before
    it and the ``right`` bars after it; placed at the confirming bar. NaN otherwise."""
    x = _asf(src)
    n = x.size
    out = np.full(n, np.nan)
    lft, rgt = int(left), int(right)
    for i in range(lft + rgt, n):
        c = i - rgt
        v = x[c]
        if all(v < x[j] for j in range(c - lft, c)) and all(
            v < x[j] for j in range(c + 1, c + rgt + 1)
        ):
            out[i] = v
    return out


def cross(a, b):
    """Generic cross: 1.0 when ``a`` crosses ``b`` in EITHER direction at bar i (crossover or
    crossunder), else 0.0; a NaN at i or i-1 yields 0.0."""
    return np.maximum(crossover(a, b), crossunder(a, b))


def highestbars(close, length: int):
    """Offset (<= 0) to the MOST RECENT highest value in the last ``length`` bars (0 = current bar);
    NaN before index length-1."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(length)
    if not _valid_period(length, n):
        return out
    for i in range(p - 1, n):
        best, besti = -np.inf, i
        for j in range(i - p + 1, i + 1):
            if x[j] >= best:  # >= keeps the most recent on ties
                best, besti = x[j], j
        out[i] = float(-(i - besti))
    return out


def lowestbars(close, length: int):
    """Offset (<= 0) to the MOST RECENT lowest value in the last ``length`` bars (0 = current bar);
    NaN before index length-1."""
    x = _asf(close)
    n = x.size
    out = np.full(n, np.nan)
    p = int(length)
    if not _valid_period(length, n):
        return out
    for i in range(p - 1, n):
        best, besti = np.inf, i
        for j in range(i - p + 1, i + 1):
            if x[j] <= best:  # <= keeps the most recent on ties
                best, besti = x[j], j
        out[i] = float(-(i - besti))
    return out


# ── Capability: elementwise math namespace (parallel to Pine's math.*) ──
# These are NOT windowed indicators — they map a scalar function over a scalar OR a series, broadcasting
# scalars in the binary forms. The round convention is HALF-AWAY-FROM-ZERO in both runtimes (numpy's and
# Python's native round use banker's rounding; JS Math.round rounds half up — neither agrees cross-runtime,
# so both twins implement sign*floor(abs+0.5) explicitly and it is pinned by the golden vectors).


def _is_seq(v) -> bool:
    return isinstance(v, (list, tuple, np.ndarray))


def _map1(x, f):
    if _is_seq(x):
        return f(_asf(x))
    return float(f(x))


def _map2(a, b, f):
    aq, bq = _is_seq(a), _is_seq(b)
    if aq or bq:
        n = len(a) if aq else len(b)
        aa = _asf(a) if aq else np.full(n, float(a))
        bb = _asf(b) if bq else np.full(n, float(b))
        return f(aa, bb)
    return float(f(a, b))


def _round_half_away(x):
    return np.sign(x) * np.floor(np.abs(x) + 0.5)


def math_abs(x):
    """Elementwise absolute value (scalar or series)."""
    return _map1(x, np.abs)


def math_sqrt(x):
    """Elementwise square root."""
    return _map1(x, np.sqrt)


def math_exp(x):
    """Elementwise e**x."""
    return _map1(x, np.exp)


def math_log(x):
    """Elementwise natural logarithm."""
    return _map1(x, np.log)


def math_log10(x):
    """Elementwise base-10 logarithm."""
    return _map1(x, np.log10)


def math_floor(x):
    """Elementwise floor."""
    return _map1(x, np.floor)


def math_ceil(x):
    """Elementwise ceiling."""
    return _map1(x, np.ceil)


def math_round(x):
    """Elementwise round, HALF AWAY FROM ZERO (cross-runtime stable)."""
    return _map1(x, _round_half_away)


def math_sign(x):
    """Elementwise sign: -1 / 0 / +1."""
    return _map1(x, np.sign)


def math_pow(base, exponent):
    """Elementwise power base**exponent (scalar exponent broadcasts)."""
    return _map2(base, exponent, np.power)


def math_max(x, y):
    """Elementwise maximum of two operands (scalar broadcasts)."""
    return _map2(x, y, np.maximum)


def math_min(x, y):
    """Elementwise minimum of two operands (scalar broadcasts)."""
    return _map2(x, y, np.minimum)


REGISTRY.update(
    {
        "sma": sma,
        "ema": ema,
        "wma": wma,
        "rma": rma,
        "stdev": stdev,
        "change": change,
        "roc": roc,
        "rsi": rsi,
        "highest": highest,
        "lowest": lowest,
        "crossover": crossover,
        "crossunder": crossunder,
        "atr": atr,
        "vwap": vwap,
        "macd": macd,
        "bollinger": bollinger,
        # P5-expand wave 1
        "tr": tr,
        "dema": dema,
        "tema": tema,
        "hma": hma,
        "vwma": vwma,
        "mom": mom,
        "cmo": cmo,
        "williams_r": williams_r,
        "cci": cci,
        "stochastic": stochastic,
        "obv": obv,
        "donchian": donchian,
        # P5-expand wave 2
        "adx": adx,
        "aroon": aroon,
        "keltner": keltner,
        "mfi": mfi,
        "cmf": cmf,
        "ad": ad,
        "ao": ao,
        "ppo": ppo,
        "natr": natr,
        "linreg": linreg,
        "trix": trix,
        "tsi": tsi,
        # P5-expand wave 3
        "variance": variance,
        "median": median,
        "percentrank": percentrank,
        "dpo": dpo,
        "uo": uo,
        "chop": chop,
        "eom": eom,
        "pvt": pvt,
        "cog": cog,
        "vortex": vortex,
        "stochrsi": stochrsi,
        "supertrend": supertrend,
        # P5-expand wave 4
        "swma": swma,
        "alma": alma,
        "dev": dev,
        "correlation": correlation,
        "bbw": bbw,
        "percentb": percentb,
        "cum": cum,
        "rising": rising,
        "falling": falling,
        "coppock": coppock,
        "elderray": elderray,
        "fisher": fisher,
        # P5-expand wave 5
        "psar": psar,
        "bop": bop,
        "pvi": pvi,
        "nvi": nvi,
        "massindex": massindex,
        "tsf": tsf,
        "vhf": vhf,
        "chaikin": chaikin,
        "midprice": midprice,
        "wad": wad,
        "alligator": alligator,
        "kst": kst,
        # array reductions (Family A6)
        "sum": rolling_sum,
        "barssince": barssince,
        "valuewhen": valuewhen,
        "pivothigh": pivothigh,
        "pivotlow": pivotlow,
        "cross": cross,
        "highestbars": highestbars,
        "lowestbars": lowestbars,
        # elementwise math namespace (public names map to math_* to avoid shadowing builtins)
        "abs": math_abs,
        "sqrt": math_sqrt,
        "exp": math_exp,
        "log": math_log,
        "log10": math_log10,
        "floor": math_floor,
        "ceil": math_ceil,
        "round": math_round,
        "sign": math_sign,
        "pow": math_pow,
        "max": math_max,
        "min": math_min,
    }
)
