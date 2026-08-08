"""
Option pricing and quote quality.

Written because an audit of Yahoo's chain found two things that make its data
unsafe to build on unmodified:

1. `impliedVolatility` is not the vol implied by the quotes Yahoo publishes
   alongside it. Re-pricing the ATM call with Black-Scholes at Yahoo's own IV
   came out 9.4% below the mid on AAPL, 14.2% on NVDA and 15.3% on MU -- the
   column is solved off `lastPrice`, which on illiquid strikes was up to 31
   days old. So IV is solved here, from the mid, and Yahoo's column is used
   only as a last resort.

2. A large share of rows have no market at all: 30% of AAPL strikes had a zero
   bid, and the 90th-percentile relative spread was 200%. Averaging those into
   a skew or an ATM reading imports noise as signal.

What Yahoo gets right, and why it is still worth using: put-call parity holds
to 0.01-0.11% of spot across AAPL, SPY, NVDA and MU, open interest is present
on 100% of rows and volume on ~96%. The quotes are real; the derived column is
not.
"""
import math

# Yahoo ships no greeks at all, so everything here is computed.
DEFAULT_RATE = 0.04


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(spot, strike, t, iv, is_call, r=DEFAULT_RATE) -> float:
    """Black-Scholes price of a European option."""
    if t <= 0 or iv <= 0:
        intrinsic = (spot - strike) if is_call else (strike - spot)
        return max(intrinsic, 0.0)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def greeks(spot, strike, t, iv, is_call, r=DEFAULT_RATE) -> dict:
    """Per-share sensitivities. Theta is per calendar day."""
    if t <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return {}
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    theta_year = (-(spot * _norm_pdf(d1) * iv) / (2 * math.sqrt(t))
                  + (-r if is_call else r) * strike * math.exp(-r * t)
                  * (_norm_cdf(d2) if is_call else _norm_cdf(-d2)))
    return {
        "delta": _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1,
        "gamma": _norm_pdf(d1) / (spot * iv * math.sqrt(t)),
        "vega": spot * _norm_pdf(d1) * math.sqrt(t) / 100,   # per 1 vol point
        "theta": theta_year / 365,
    }


IV_LOWER, IV_UPPER = 1e-4, 8.0


def implied_vol(price, spot, strike, t, is_call, r=DEFAULT_RATE):
    """
    Solve for the volatility that reproduces `price`, or None if none exists.

    Bisection rather than Newton: vega collapses to nearly zero on deep ITM and
    far OTM strikes, where Newton diverges or lands on an absurd root. Yahoo's
    own column shows exactly that failure -- 673% on a far OTM MU strike.

    Returns None when the price is outside the no-arbitrage band, which is the
    honest answer for a stale or crossed quote. A number would look like a
    measurement.
    """
    if not (price and price > 0) or t <= 0 or spot <= 0 or strike <= 0:
        return None

    # The no-arbitrage floor for a European option is the *discounted* one:
    # S - Ke^-rT for a call, Ke^-rT - S for a put. Using undiscounted intrinsic
    # rejects deep in-the-money puts, which legitimately trade below K - S
    # because the strike is only received at expiry. A 5%-vol put five points
    # ITM is worth about 3.54 against an intrinsic of 5.00, and the naive bound
    # called that unsolvable.
    discounted = strike * math.exp(-r * t)
    floor = max((spot - discounted) if is_call else (discounted - spot), 0.0)
    if price < floor - 1e-9:
        return None                                   # below the no-arbitrage floor
    if price >= bs_price(spot, strike, t, IV_UPPER, is_call, r):
        return None                                   # above the 800% vol price

    lo, hi = IV_LOWER, IV_UPPER
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, mid, is_call, r) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)


def quote_mid(row) -> tuple:
    """
    The price to value a contract at, and how much to trust it.

    Returns (price, quality) where quality is one of:
      "mid"    -- a genuine two-sided market, bid and ask both positive
      "ask"    -- bid is zero; the ask is the only live side
      "last"   -- no market at all, falling back to the last trade, which the
                  audit found to be up to 31 days old on illiquid strikes
      None     -- nothing usable
    """
    bid = _as_float(row.get("bid"))
    ask = _as_float(row.get("ask"))
    last = _as_float(row.get("lastPrice"))

    if bid and ask and bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0, "mid"
    if ask and ask > 0:
        return ask, "ask"
    if last and last > 0:
        return last, "last"
    return None, None


def _as_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def solve_row_iv(row, spot, t, is_call, r=DEFAULT_RATE) -> dict:
    """
    IV for one chain row, solved from its own quote.

    Falls back to Yahoo's published column only when the quote cannot be
    solved, and always says which source was used -- a caller aggregating these
    needs to be able to drop the fallbacks.
    """
    price, quality = quote_mid(row)
    iv = implied_vol(price, spot, float(row["strike"]), t, is_call, r) if price else None
    if iv is not None:
        return {"iv": iv, "iv_source": "solved", "price": price, "price_quality": quality}

    fallback = _as_float(row.get("impliedVolatility"))
    if fallback and 0 < fallback <= 3.0:      # above 300% the column is garbage
        return {"iv": fallback, "iv_source": "yahoo", "price": price,
                "price_quality": quality}
    return {"iv": None, "iv_source": None, "price": price, "price_quality": quality}


def is_liquid(row, max_relative_spread=0.5) -> bool:
    """
    Whether a row carries enough of a market to be worth averaging.

    Zero-bid rows are the common case (30% of AAPL strikes) and a 200% relative
    spread means the market is one-sided in all but name.
    """
    bid = _as_float(row.get("bid")) or 0.0
    ask = _as_float(row.get("ask")) or 0.0
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return False
    return (ask - bid) / mid <= max_relative_spread


def atm_iv(calls, puts, spot, t, r=DEFAULT_RATE) -> dict:
    """
    At-the-money implied volatility, averaged across the call and the put.

    Both sides because a single side inherits any skew at that strike; parity
    means a clean chain gives nearly the same answer either way, so a large
    disagreement between them is itself a data-quality signal worth returning.
    """
    out = {"iv": None, "call_iv": None, "put_iv": None, "strike": None,
           "sources": [], "price_quality": []}
    if calls is None or puts is None or calls.empty or puts.empty:
        return out

    call = calls.iloc[(calls["strike"] - spot).abs().argsort().iloc[0]]
    put = puts.iloc[(puts["strike"] - spot).abs().argsort().iloc[0]]
    out["strike"] = float(call["strike"])

    c = solve_row_iv(call, spot, t, True, r)
    p = solve_row_iv(put, spot, t, False, r)
    out["call_iv"], out["put_iv"] = c["iv"], p["iv"]
    out["sources"] = [s for s in (c["iv_source"], p["iv_source"]) if s]
    out["price_quality"] = [q for q in (c["price_quality"], p["price_quality"]) if q]

    have = [v for v in (c["iv"], p["iv"]) if v is not None]
    if have:
        out["iv"] = sum(have) / len(have)
        if len(have) == 2:
            out["call_put_gap"] = abs(c["iv"] - p["iv"])
    return out


def straddle_price(calls, puts, spot) -> dict:
    """
    ATM straddle cost -- the market's own expected move to expiry.

    Priced off the mid, not `lastPrice`: the audit found last trades up to 31
    days stale, and a stale straddle understates the move the market is
    actually charging for.
    """
    if calls is None or puts is None or calls.empty or puts.empty:
        return {}
    call = calls.iloc[(calls["strike"] - spot).abs().argsort().iloc[0]]
    put = puts.iloc[(puts["strike"] - spot).abs().argsort().iloc[0]]
    c_price, c_q = quote_mid(call)
    p_price, p_q = quote_mid(put)
    if c_price is None or p_price is None:
        return {}
    return {
        "straddle": c_price + p_price,
        "strike": float(call["strike"]),
        "call": c_price,
        "put": p_price,
        "quality": "mid" if c_q == p_q == "mid" else f"{c_q}/{p_q}",
    }
