"""
The three readings the indicator strip could not make, computed once.

Both the dashboard and `get_market_analysis` show a live signal panel, and they
used to build it separately from the same frames. That is how two surfaces come
to disagree about the same bar, so the arithmetic lives here and both call it.

Each function returns a `verdict` and the `basis` that produced it. A reading
whose derivation a person cannot check is a reading they have to trust, and the
regime classifier spent months labelling three unrelated situations "Mixed
Trend" precisely because nothing displayed the number underneath.

None of these is a trade signal. They describe the bar; what to do about it is
not a thing this file claims to know.
"""
try:
    from dashboard import indicators, volume_profile
except ImportError:  # imported as a top-level module from dashboard/
    import indicators
    import volume_profile

#: How far above or below its own average a bar's volume must sit before the
#: word changes. Conventional, and unvalidated here: nothing in this project
#: has shown 1.5x separates confirmed moves from unconfirmed ones.
VOLUME_HEAVY = 1.5
VOLUME_LIGHT = 0.6

#: Within this percentage of a high-volume node, price is "at" it rather than
#: merely near it. A node is a price region the auction spent time in, not a
#: line, so an exact match is not the question.
NODE_AT_PCT = 0.75


def volume_confirmation(df, lookback: int = 20) -> dict:
    """
    Is the latest bar's move backed by participation?

    A trend on thin volume is a different object from the same trend on heavy
    volume, and the strip previously showed raw share count — a number with no
    reference, which is unreadable without knowing the symbol's usual turnover.

    The average deliberately **excludes the latest bar**: including it drags the
    mean toward the value being compared against, which understates exactly the
    outliers this is for.
    """
    volumes = df["volume"].astype(float)
    if len(volumes) < lookback + 1:
        return {"verdict": "unknown", "ratio": None, "latest": None,
                "average": None,
                "basis": f"needs {lookback + 1} bars, has {len(volumes)}"}

    latest = float(volumes.iloc[-1])
    average = float(volumes.iloc[-(lookback + 1):-1].mean())
    if not average > 0:
        return {"verdict": "unknown", "ratio": None, "latest": latest,
                "average": average,
                "basis": "no traded volume in the comparison window"}

    ratio = latest / average
    if ratio >= VOLUME_HEAVY:
        verdict = "heavy"
    elif ratio <= VOLUME_LIGHT:
        verdict = "light"
    else:
        verdict = "typical"
    return {
        "verdict": verdict,
        "ratio": ratio,
        "latest": latest,
        "average": average,
        "basis": (f"{ratio:.2f}x the {lookback}-bar average of "
                  f"{average:,.0f} (heavy at {VOLUME_HEAVY}x, "
                  f"light at {VOLUME_LIGHT}x)"),
    }


def trend_strength(df, precomputed: dict = None) -> dict:
    """
    The ADX reading behind the regime label, and the label's own test.

    The strip showed a regime word with "classifier" underneath it. The number
    that decided it was never displayed, which is why "Mixed Trend" could cover
    the warm-up, a disputed trend and the 20-23 threshold gap without anyone
    noticing they were different.
    """
    p = precomputed or {}
    adx_df = p.get("adx") if p.get("adx") is not None else indicators.calculate_adx(df)
    regime_df = (p.get("regime") if p.get("regime") is not None
                 else indicators.classify_market_regime(df, precomputed=p))

    adx_series = adx_df["adx"].astype(float)
    adx = adx_series.iloc[-1] if len(adx_series) else None
    regime = regime_df["regime"].iloc[-1] if len(regime_df) else None

    if adx is None or adx != adx:            # NaN
        return {"adx": None, "regime": regime, "verdict": "unknown",
                "basis": "ADX is not computable on this many bars"}

    if adx > indicators.ADX_TRENDING:
        verdict = "trending"
    elif adx < indicators.ADX_RANGING:
        verdict = "ranging"
    else:
        verdict = "transitional"

    return {
        "adx": float(adx),
        "regime": regime,
        "verdict": verdict,
        "basis": (f"ADX {adx:.1f} — {indicators.describe_regime(regime)}"
                  if regime else f"ADX {adx:.1f}"),
    }


def auction_position(df, lookback: int = 100, buckets: int = 20) -> dict:
    """
    Where price sits relative to the nearest high-volume node.

    A high-volume node is a price the auction spent time agreeing on, so it is
    where resistance and support actually are — as opposed to a round number or
    a drawn line. The volume profile was already computed for its own tab and
    never consulted by the live strip.
    """
    if len(df) < 2:
        return {"verdict": "unknown", "node": None, "distance_pct": None,
                "basis": "not enough bars for a profile"}

    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    volumes = df["volume"].astype(float).tolist()
    price = float(df["close"].iloc[-1])

    # Clamp to what exists. Asking for a 100-bar profile of a 60-bar frame
    # returned no nodes at all, so the panel said "unknown" on a window that
    # had a perfectly good profile in it -- an answer withheld for a reason
    # that was not the reader's.
    window = min(lookback, len(df))
    nodes = volume_profile.high_volume_nodes(highs, lows, volumes,
                                             lookback=window, buckets=buckets)
    if not nodes:
        return {"verdict": "unknown", "node": None, "distance_pct": None,
                "basis": "no high-volume node in the window"}

    nearest = volume_profile._nearest(price, nodes)
    node_price = float(nearest["price"])
    if not price > 0:
        return {"verdict": "unknown", "node": node_price, "distance_pct": None,
                "basis": "non-positive price"}

    distance_pct = (price - node_price) / price * 100
    if abs(distance_pct) <= NODE_AT_PCT:
        verdict = "at a node"
    elif distance_pct > 0:
        verdict = "above the nearest node"
    else:
        verdict = "below the nearest node"

    return {
        "verdict": verdict,
        "node": node_price,
        "distance_pct": distance_pct,
        "is_poc": bool(nearest.get("is_poc")),
        "volume_share": float(nearest.get("volume_share") or 0.0),
        "window": window,
        "basis": (f"nearest high-volume node {node_price:,.2f}"
                  + (" (point of control)" if nearest.get("is_poc") else "")
                  + f", {abs(distance_pct):.2f}% "
                  + ("above" if distance_pct > 0 else "below")
                  + f" it over {window} bars; 'at' within {NODE_AT_PCT}%"),
    }


def live_signals(df, precomputed: dict = None) -> dict:
    """All three, for a caller that wants the panel rather than one reading."""
    return {
        "volume": volume_confirmation(df),
        "trend": trend_strength(df, precomputed),
        "auction": auction_position(df),
    }


def signal_lines(df, precomputed: dict = None) -> list:
    """
    The panel as markdown bullets, so the dashboard and the MCP tool cannot
    drift into describing the same bar differently.

    Every line carries its basis. A model reading "volume heavy" with no
    reference cannot tell whether that is 1.6x or 16x, and the difference
    changes what the sentence means.
    """
    s = live_signals(df, precomputed)
    volume, trend, auction = s["volume"], s["trend"], s["auction"]

    lines = []
    if volume["ratio"] is None:
        lines.append(f"- **Volume**: not comparable — {volume['basis']}")
    else:
        lines.append(f"- **Volume**: {volume['verdict'].upper()} — {volume['basis']}")

    if trend["adx"] is None:
        lines.append(f"- **Trend strength**: unknown — {trend['basis']}")
    else:
        lines.append(f"- **Trend strength**: {trend['verdict'].upper()} — {trend['basis']}")

    if auction["node"] is None:
        lines.append(f"- **Auction**: unknown — {auction['basis']}")
    else:
        lines.append(f"- **Auction**: {auction['verdict'].upper()} — {auction['basis']}")
    return lines
