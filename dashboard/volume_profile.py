"""
Volume-by-price: profile, value area, and high/low volume nodes.

Ported from Powerstation's JS implementation
(apex-terminal/src/indicators/registry/builtin/{volumeProfile,hvn,lvn}.js) rather
than invented, so a second runtime already agrees on the arithmetic and the
edge-case handling was arrived at once rather than twice.

Why this and not another oscillator: it changes what can be *said*. "RSI is 62"
is a number about the past. "Price is advancing through a low-volume node toward
the high-volume shelf at 318" is a statement about where the market previously
agreed on value and where it did not — which is the kind of thing an analyst
says and a purely oscillator-driven tool cannot.

Reference: Steidlmayer's Market Profile (1985) for value area and POC;
Dalton (2007) for the volume-weighted variant.

Known approximation, carried over deliberately: volume is spread uniformly
across each bar's high-low range. Real intrabar volume clusters at the open and
close. Fixing it needs tick data, which no free feed provides; the estimate is
stated as an estimate wherever it surfaces.
"""
import math

DEFAULT_LOOKBACK = 100
DEFAULT_BUCKETS = 20
DEFAULT_VALUE_AREA = 0.70
HVN_QUANTILE = 0.80
LVN_QUANTILE = 0.20


def _finite(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def build_profile(highs, lows, volumes, lookback=DEFAULT_LOOKBACK, buckets=DEFAULT_BUCKETS):
    """
    Volume-by-price histogram over the last `lookback` bars in `buckets` bins.

    Returns None — not an empty profile — on any input that cannot produce a
    meaningful one: too few bars, a degenerate price range, no volume. A caller
    that gets None declines to draw; one that gets a zero-filled profile would
    happily report a POC at an arbitrary price.
    """
    n = len(highs)
    if n == 0 or len(lows) != n or len(volumes) != n:
        return None
    if not isinstance(lookback, int) or lookback < 1:
        return None
    if not isinstance(buckets, int) or buckets < 2:
        return None
    if n < lookback:
        return None

    start = n - lookback
    price_min, price_max = math.inf, -math.inf
    for i in range(start, n):
        h, l = _finite(highs[i]), _finite(lows[i])
        if h is not None and h > price_max:
            price_max = h
        if l is not None and l < price_min:
            price_min = l
    if not math.isfinite(price_min) or not math.isfinite(price_max) or price_max <= price_min:
        return None

    width = (price_max - price_min) / buckets
    if not width > 0:
        return None

    hist = [0.0] * buckets
    total = 0.0
    for i in range(start, n):
        h, l, v = _finite(highs[i]), _finite(lows[i]), _finite(volumes[i])
        if h is None or l is None or v is None or v <= 0:
            continue
        # Clamp the LOW bucket to buckets-1 as well as to 0. A flat bar sitting
        # exactly at price_max yields floor((l - price_min) / width) == buckets,
        # which without the upper clamp exceeds b_hi and silently drops that
        # bar's volume -- total would no longer equal the volume summed.
        b_lo = min(buckets - 1, max(0, int((l - price_min) // width)))
        b_hi = min(buckets - 1, int((h - price_min) // width))
        span = max(1, b_hi - b_lo + 1)
        per_bucket = v / span
        for b in range(b_lo, b_hi + 1):
            hist[b] += per_bucket
            total += per_bucket
    if not total > 0:
        return None

    poc_idx = 0
    for b in range(1, buckets):
        if hist[b] > hist[poc_idx]:
            poc_idx = b

    return {"buckets": hist, "price_min": price_min, "price_max": price_max,
            "bucket_width": width, "total": total, "poc_idx": poc_idx, "k": buckets}


def expand_value_area(hist, poc_idx, total, fraction, method="single"):
    """
    Grow a contiguous band outward from the POC until it holds `fraction` of volume.

    "single" annexes whichever one adjacent bucket holds more (ties expand
    upward — a stated convention, so the result is deterministic rather than
    dependent on float comparison order). "dalton" is the classic two-row rule:
    compare the sum of the next two above against the next two below and take
    both of the winning side.
    """
    k = len(hist)
    target = fraction * total
    lo = hi = poc_idx
    acc = hist[poc_idx]

    if method == "dalton":
        while acc < target and (lo > 0 or hi < k - 1):
            up1 = hist[hi + 1] if hi + 1 <= k - 1 else None
            up2 = hist[hi + 2] if hi + 2 <= k - 1 else 0.0
            dn1 = hist[lo - 1] if lo - 1 >= 0 else None
            dn2 = hist[lo - 2] if lo - 2 >= 0 else 0.0
            up_sum = -math.inf if up1 is None else up1 + up2
            dn_sum = -math.inf if dn1 is None else dn1 + dn2
            if up_sum == -math.inf and dn_sum == -math.inf:
                break
            if up_sum >= dn_sum:
                hi += 1
                acc += hist[hi]
                if hi + 1 <= k - 1:
                    hi += 1
                    acc += hist[hi]
            else:
                lo -= 1
                acc += hist[lo]
                if lo - 1 >= 0:
                    lo -= 1
                    acc += hist[lo]
    else:
        while acc < target and (lo > 0 or hi < k - 1):
            up = hist[hi + 1] if hi + 1 <= k - 1 else -math.inf
            down = hist[lo - 1] if lo - 1 >= 0 else -math.inf
            if up >= down:
                hi += 1
                acc += hist[hi]
            else:
                lo -= 1
                acc += hist[lo]

    return {"lo": lo, "hi": hi, "coverage": acc / total if total else 0.0}


def value_area(highs, lows, volumes, lookback=DEFAULT_LOOKBACK, buckets=DEFAULT_BUCKETS,
               fraction=DEFAULT_VALUE_AREA, method="single"):
    """POC, VAH and VAL. VAH/VAL are the price *edges* of the band; POC is a bucket centre."""
    if not (0 < fraction <= 1):
        fraction = DEFAULT_VALUE_AREA
    prof = build_profile(highs, lows, volumes, lookback, buckets)
    if prof is None:
        return None

    band = expand_value_area(prof["buckets"], prof["poc_idx"], prof["total"], fraction, method)
    lo, hi, width, pmin = band["lo"], band["hi"], prof["bucket_width"], prof["price_min"]
    return {
        "poc": pmin + (prof["poc_idx"] + 0.5) * width,
        "vah": pmin + (hi + 1) * width,
        "val": pmin + lo * width,
        "coverage": band["coverage"],
        "profile": prof,
    }


def _quantile_threshold(hist, q):
    """The same index rule both runtimes use: floor(q * (len-1)) of the sorted histogram."""
    ordered = sorted(hist)
    return ordered[int(q * (len(ordered) - 1))]


def high_volume_nodes(highs, lows, volumes, lookback=DEFAULT_LOOKBACK, buckets=DEFAULT_BUCKETS):
    """
    Price shelves the market accepted: buckets at or above the 80th percentile
    of volume, plus the POC itself regardless of where it falls.
    """
    prof = build_profile(highs, lows, volumes, lookback, buckets)
    if prof is None:
        return []
    hist, total = prof["buckets"], prof["total"]
    threshold = _quantile_threshold(hist, HVN_QUANTILE)

    out = []
    for b in range(prof["k"]):
        is_poc = b == prof["poc_idx"]
        if not is_poc and hist[b] < threshold:
            continue
        if hist[b] == 0:
            continue
        out.append({
            "type": "hvn",
            "price": prof["price_min"] + (b + 0.5) * prof["bucket_width"],
            "volume_share": hist[b] / total,
            "is_poc": is_poc,
        })
    return out


def low_volume_nodes(highs, lows, volumes, lookback=DEFAULT_LOOKBACK, buckets=DEFAULT_BUCKETS):
    """
    Thin prices the market rejected: at or below the 20th percentile AND a strict
    local minimum, so a long flat low shelf does not report every bucket in it.
    A missing edge neighbour counts as +infinity, so the first and last buckets
    can still qualify.

    These matter because price tends to move fast through them — they act as
    breakout triggers and as support/resistance flips.

    Known limitation of the reference rule, kept rather than fixed: a *fully
    untraded* gap returns nothing. A run of empty buckets is a run of equal
    zeros, so every interior bucket ties with its neighbours and fails the
    strict `<` test — even though an empty price band is the most textbook low
    volume node there is. The rule fires on dips within a continuously traded
    profile, not on holes in it. Diverging here would mean the JS and Python
    runtimes silently disagree, which costs more than the extra signal; gap
    detection belongs in a separate function that is clearly not a port.
    """
    prof = build_profile(highs, lows, volumes, lookback, buckets)
    if prof is None:
        return []
    hist, total, k = prof["buckets"], prof["total"], prof["k"]
    threshold = _quantile_threshold(hist, LVN_QUANTILE)

    out = []
    for b in range(k):
        left = hist[b - 1] if b > 0 else math.inf
        right = hist[b + 1] if b < k - 1 else math.inf
        if not (hist[b] <= threshold and hist[b] < left and hist[b] < right):
            continue
        out.append({
            "type": "lvn",
            "price": round(prof["price_min"] + (b + 0.5) * prof["bucket_width"], 8),
            "volume_share": round(hist[b] / total, 8) if total > 0 else 0.0,
            "is_valley": True,
        })
    return out


def describe_position(price, nodes_high, nodes_low, value):
    """
    Where the current price sits in the auction, in words.

    This is the point of the module: it turns the profile into the sentence an
    analyst would say, so a model reading the tool output does not have to infer
    it from a list of numbers and risk inventing the interpretation.
    """
    if value is None:
        return "No volume profile could be built for this window."

    poc, vah, val = value["poc"], value["vah"], value["val"]
    parts = []

    if price > vah:
        parts.append(f"trading above the value area high ({vah:,.2f}) — acceptance "
                     "above prior value, or an unaccepted excursion")
    elif price < val:
        parts.append(f"trading below the value area low ({val:,.2f}) — acceptance "
                     "below prior value, or an unaccepted excursion")
    else:
        where = "above" if price > poc else "below" if price < poc else "at"
        parts.append(f"inside the value area ({val:,.2f}–{vah:,.2f}), {where} "
                     f"the point of control ({poc:,.2f})")

    near_lvn = _nearest(price, nodes_low)
    if near_lvn is not None:
        parts.append(f"the nearest low-volume node is {near_lvn['price']:,.2f} — thin "
                     "price the market previously rejected, so moves through it tend "
                     "to be fast")

    above = [n for n in nodes_high if n["price"] > price]
    below = [n for n in nodes_high if n["price"] < price]
    if above:
        nxt = min(above, key=lambda n: n["price"])
        parts.append(f"the next high-volume shelf above is {nxt['price']:,.2f} "
                     f"({nxt['volume_share'] * 100:.1f}% of window volume)")
    if below:
        nxt = max(below, key=lambda n: n["price"])
        parts.append(f"the nearest shelf below is {nxt['price']:,.2f} "
                     f"({nxt['volume_share'] * 100:.1f}%)")

    return "Price is " + "; ".join(parts) + "."


def _nearest(price, nodes):
    if not nodes:
        return None
    return min(nodes, key=lambda n: abs(n["price"] - price))
