"""
Option pricing, IV solving and quote quality.

These exist because an audit of Yahoo's live chain found its `impliedVolatility`
column inconsistent with the quotes printed beside it: re-pricing the ATM call
at Yahoo's own IV came out 9.4% below the mid on AAPL, 14.2% on NVDA, 15.3% on
MU and 17.6% on SPY. Solving from the mid reproduces it to within 0.1%. The
tests below pin that solver and the liquidity rules that decide which rows are
worth averaging.
"""
import math
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import options_math as om


# =====================================================================
# Black-Scholes
# =====================================================================

def test_put_call_parity_holds():
    """C - P = S - K*exp(-rT). If this fails nothing downstream is meaningful."""
    s, k, t, iv, r = 100.0, 95.0, 0.5, 0.3, 0.04
    c = om.bs_price(s, k, t, iv, True, r)
    p = om.bs_price(s, k, t, iv, False, r)
    assert c - p == pytest.approx(s - k * math.exp(-r * t), abs=1e-9)


def test_a_zero_time_option_is_worth_its_intrinsic():
    assert om.bs_price(110, 100, 0, 0.3, True) == pytest.approx(10)
    assert om.bs_price(90, 100, 0, 0.3, True) == pytest.approx(0)
    assert om.bs_price(90, 100, 0, 0.3, False) == pytest.approx(10)


def test_price_rises_monotonically_with_volatility():
    prices = [om.bs_price(100, 100, 0.5, iv, True) for iv in (0.1, 0.2, 0.4, 0.8)]
    assert prices == sorted(prices)


def test_call_delta_is_bounded_and_put_delta_is_negative():
    g_call = om.greeks(100, 100, 0.5, 0.3, True)
    g_put = om.greeks(100, 100, 0.5, 0.3, False)
    assert 0 < g_call["delta"] < 1
    assert -1 < g_put["delta"] < 0
    # Same strike and vol: delta_call - delta_put = 1.
    assert g_call["delta"] - g_put["delta"] == pytest.approx(1.0, abs=1e-6)
    assert g_call["gamma"] == pytest.approx(g_put["gamma"], abs=1e-9)
    assert g_call["theta"] < 0                       # long options decay


def test_greeks_are_empty_rather_than_infinite_at_expiry():
    """Gamma divides by sqrt(t); at t=0 that is a division by zero."""
    assert om.greeks(100, 100, 0, 0.3, True) == {}
    assert om.greeks(100, 100, 0.5, 0, True) == {}


# =====================================================================
# Implied volatility
# =====================================================================

@pytest.mark.parametrize("iv", [0.05, 0.15, 0.3, 0.75, 1.5, 3.0])
@pytest.mark.parametrize("is_call", [True, False])
def test_the_solver_recovers_the_volatility_it_was_priced_at(iv, is_call):
    s, k, t = 100.0, 105.0, 0.35
    price = om.bs_price(s, k, t, iv, is_call)
    assert om.implied_vol(price, s, k, t, is_call) == pytest.approx(iv, abs=1e-4)


def test_the_solver_works_on_deep_wings_where_newton_diverges():
    """
    Vega collapses to nearly zero far from the money, which is where Yahoo's own
    solver produced 673% on an MU strike. Bisection has no such failure mode.
    """
    for k in (40.0, 250.0):
        price = om.bs_price(100.0, k, 0.5, 0.45, True)
        assert om.implied_vol(price, 100.0, k, 0.5, True) == pytest.approx(0.45, abs=1e-3)


def test_a_price_below_intrinsic_has_no_solution():
    """A crossed or stale quote must not be answered with a number."""
    assert om.implied_vol(2.0, 120.0, 100.0, 0.5, True) is None


def test_an_absurd_price_has_no_solution():
    assert om.implied_vol(99.0, 100.0, 100.0, 0.01, True) is None


def test_non_prices_return_none_rather_than_raising():
    for bad in (0, None, -1, float("nan")):
        assert om.implied_vol(bad, 100, 100, 0.5, True) is None
    assert om.implied_vol(5, 100, 100, 0, True) is None


# =====================================================================
# Quote quality
# =====================================================================

def test_a_two_sided_market_is_priced_at_the_mid():
    price, quality = om.quote_mid({"bid": 4.0, "ask": 4.6, "lastPrice": 9.9})
    assert price == pytest.approx(4.3)
    assert quality == "mid"


def test_a_zero_bid_falls_back_to_the_ask_and_says_so():
    price, quality = om.quote_mid({"bid": 0.0, "ask": 0.4, "lastPrice": 9.9})
    assert (price, quality) == (0.4, "ask")


def test_no_market_falls_back_to_the_last_trade_and_says_so():
    """
    The audit found last trades up to 31 days old on illiquid strikes. Using
    one is sometimes the only option; hiding that it happened is not.
    """
    price, quality = om.quote_mid({"bid": 0, "ask": 0, "lastPrice": 3.3})
    assert (price, quality) == (3.3, "last")


def test_a_row_with_nothing_usable_returns_nothing():
    assert om.quote_mid({"bid": 0, "ask": 0, "lastPrice": 0}) == (None, None)
    assert om.quote_mid({}) == (None, None)


def test_a_crossed_market_is_not_treated_as_a_mid():
    """ask < bid is a broken quote, not a tight one."""
    price, quality = om.quote_mid({"bid": 5.0, "ask": 4.0, "lastPrice": 4.5})
    assert quality != "mid"


@pytest.mark.parametrize("row,liquid", [
    ({"bid": 4.0, "ask": 4.2}, True),         # 4.9% spread
    ({"bid": 1.0, "ask": 1.8}, False),        # 57% spread
    ({"bid": 0.0, "ask": 0.5}, False),        # zero bid: 30% of AAPL strikes
    ({"bid": 0.0, "ask": 0.0}, False),
    ({"bid": 5.0, "ask": 4.0}, False),        # crossed
])
def test_liquidity_screen(row, liquid):
    assert om.is_liquid(row) is liquid


# =====================================================================
# Row-level IV, with provenance
# =====================================================================

def test_a_solvable_row_is_solved_not_taken_from_yahoo():
    price = om.bs_price(100, 100, 0.5, 0.42, True)
    row = {"strike": 100, "bid": price - 0.01, "ask": price + 0.01,
           "lastPrice": 999, "impliedVolatility": 0.20}
    got = om.solve_row_iv(row, 100, 0.5, True)
    assert got["iv_source"] == "solved"
    assert got["iv"] == pytest.approx(0.42, abs=1e-3)
    assert got["iv"] != pytest.approx(0.20)


def test_an_unsolvable_row_falls_back_to_yahoos_column():
    row = {"strike": 100, "bid": 0, "ask": 0, "lastPrice": 0,
           "impliedVolatility": 0.31}
    got = om.solve_row_iv(row, 100, 0.5, True)
    assert got["iv_source"] == "yahoo" and got["iv"] == pytest.approx(0.31)


def test_an_absurd_yahoo_iv_is_rejected_rather_than_used():
    """NVDA had 9 rows above 300% and MU 13, topping out at 673%."""
    row = {"strike": 100, "bid": 0, "ask": 0, "lastPrice": 0,
           "impliedVolatility": 6.7}
    got = om.solve_row_iv(row, 100, 0.5, True)
    assert got["iv"] is None and got["iv_source"] is None


# =====================================================================
# ATM aggregation
# =====================================================================

def chain(strikes, spot, t, iv, is_call):
    return pd.DataFrame([{
        "strike": k,
        "bid": om.bs_price(spot, k, t, iv, is_call) - 0.01,
        "ask": om.bs_price(spot, k, t, iv, is_call) + 0.01,
        "lastPrice": 0.0,
        "impliedVolatility": 0.99,
    } for k in strikes])


def test_atm_iv_picks_the_nearest_strike_and_averages_both_sides():
    spot, t, iv = 100.0, 0.25, 0.28
    strikes = [90, 95, 99, 101, 105, 110]
    got = om.atm_iv(chain(strikes, spot, t, iv, True),
                    chain(strikes, spot, t, iv, False), spot, t)
    assert got["strike"] in (99, 101)
    assert got["iv"] == pytest.approx(iv, abs=2e-3)
    assert set(got["sources"]) == {"solved"}
    assert got["call_put_gap"] < 1e-2        # parity: both sides agree


def test_atm_iv_on_an_empty_chain_returns_nothing():
    empty = pd.DataFrame(columns=["strike", "bid", "ask", "lastPrice", "impliedVolatility"])
    assert om.atm_iv(empty, empty, 100, 0.25)["iv"] is None
    assert om.atm_iv(None, None, 100, 0.25)["iv"] is None


def test_the_straddle_is_priced_off_the_mid_not_the_last_trade():
    """A last trade up to 31 days old understates the move being charged."""
    spot, t, iv = 100.0, 0.25, 0.3
    calls = chain([100], spot, t, iv, True)
    puts = chain([100], spot, t, iv, False)
    calls.loc[0, "lastPrice"] = 0.01        # a stale print
    puts.loc[0, "lastPrice"] = 0.01
    got = om.straddle_price(calls, puts, spot)
    expected = om.bs_price(spot, 100, t, iv, True) + om.bs_price(spot, 100, t, iv, False)
    assert got["straddle"] == pytest.approx(expected, abs=0.05)
    assert got["quality"] == "mid"
    assert got["straddle"] > 1.0            # nothing like the 0.02 stale prints


def test_the_straddle_reports_a_degraded_quote_rather_than_hiding_it():
    calls = pd.DataFrame([{"strike": 100, "bid": 0, "ask": 0, "lastPrice": 5.0,
                           "impliedVolatility": 0.3}])
    puts = pd.DataFrame([{"strike": 100, "bid": 4.9, "ask": 5.1, "lastPrice": 5.0,
                          "impliedVolatility": 0.3}])
    got = om.straddle_price(calls, puts, 100.0)
    assert got["quality"] == "last/mid"


# =====================================================================
# The tools must not go back to the unreliable column
# =====================================================================

def test_no_tool_reads_yahoos_iv_column_outside_the_fallback():
    """
    Every options tool used to take `impliedVolatility` at face value. The one
    remaining reference should be the explicit fallback inside options_math,
    plus the raw chain dump where it is labelled as Yahoo's own figure.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "finance_mcp.py"), encoding="utf-8").read()
    hits = [i for i, line in enumerate(src.splitlines(), 1)
            if "impliedVolatility" in line]
    assert len(hits) <= 4, (
        f"{len(hits)} raw uses of Yahoo's impliedVolatility remain at lines {hits}; "
        "solve from the mid via options_math instead")
