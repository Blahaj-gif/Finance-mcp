"""
Property-based tests for the filing parsers, and the reconciliation checks.

The four parser bugs this project has found were all malformed-input cases that
a generator would have hit in seconds: `parse_number("1.2e3")` returning 1.23
because a character strip ate the exponent, 8-K item codes split on comma only
so semicolon forms lost every code, cash-flow tags filed cumulatively, and a
surprise percentage multiplied by a hundred.

Each was found by a person looking at output. A generator does not get bored.
"""
import math
import os
import sys

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import edgar_forms as ef
from dashboard import normalization as nz


# =====================================================================
# parse_number
# =====================================================================

@given(st.floats(allow_nan=False, allow_infinity=False,
                 min_value=-1e12, max_value=1e12))
def test_a_plain_number_survives_a_round_trip(value):
    """
    The bug this closes: "1.2e3" parsed to 1.23, because the character strip
    that removes currency symbols removed the exponent with them. It read as a
    real number, three orders of magnitude out.
    """
    parsed = nz.parse_number(repr(value))
    assert parsed is not None, repr(value)
    assert math.isclose(parsed, value, rel_tol=1e-9, abs_tol=1e-9)


@given(st.integers(min_value=-10**12, max_value=10**12))
def test_thousands_separators_do_not_change_the_value(value):
    assert nz.parse_number(f"{value:,}") == pytest.approx(float(value))


@given(st.floats(min_value=0.01, max_value=1e9, allow_nan=False,
                 allow_infinity=False))
def test_currency_and_whitespace_are_noise_not_magnitude(value):
    text = f"{value:.2f}"
    bare = nz.parse_number(text)
    for decorated in (f"${text}", f"  {text}  ", f"USD {text}", f"{text} USD"):
        assert nz.parse_number(decorated) == pytest.approx(bare), decorated


@given(st.floats(min_value=0.01, max_value=1e9, allow_nan=False,
                 allow_infinity=False))
def test_a_parenthesised_number_is_negative(value):
    """Accounting notation. Reading (1,234) as positive inverts a cash flow."""
    text = f"{value:.2f}"
    assert nz.parse_number(f"({text})") == pytest.approx(-float(text))


@given(st.text(max_size=40))
def test_unparseable_text_yields_none_rather_than_zero(text):
    """
    Zero is a number someone will act on. Absence must never look like a
    measurement -- the rule this whole project turns on.
    """
    result = nz.parse_number(text)
    assert result is None or isinstance(result, float)
    if result == 0.0:
        assert any(ch.isdigit() for ch in text), (
            f"{text!r} contains no digit yet parsed as zero")


# =====================================================================
# 13F reconciliation
# =====================================================================

_holding = st.fixed_dictionaries({
    "value": st.floats(min_value=0, max_value=1e11, allow_nan=False,
                       allow_infinity=False),
    "rows": st.integers(min_value=1, max_value=40),
})


@given(st.lists(_holding, min_size=1, max_size=60))
def test_a_faithful_parse_always_reconciles(holdings):
    """The check must not fire on correct input, or it will be ignored."""
    summary = {"entries": sum(h["rows"] for h in holdings),
               "value": sum(h["value"] for h in holdings)}
    result = ef.reconcile_13f(holdings, summary)
    assert result["reconciled"] is True, result["problems"]


@given(st.lists(_holding, min_size=2, max_size=60), st.integers(min_value=1))
def test_a_dropped_row_is_always_caught(holdings, drop):
    """
    The failure this exists for. A parse that silently loses rows produces a
    smaller number, and a smaller number is indistinguishable from a smaller
    portfolio.
    """
    total_rows = sum(h["rows"] for h in holdings)
    assume(drop < total_rows)
    summary = {"entries": total_rows + drop,
               "value": sum(h["value"] for h in holdings)}
    result = ef.reconcile_13f(holdings, summary)
    assert result["reconciled"] is False
    assert any("entry count" in p for p in result["problems"])


@given(st.lists(_holding, min_size=1, max_size=40),
       st.floats(min_value=1e6, max_value=1e10))
def test_a_value_that_is_off_by_a_position_is_caught(holdings, gap):
    summary = {"entries": sum(h["rows"] for h in holdings),
               "value": sum(h["value"] for h in holdings) + gap}
    result = ef.reconcile_13f(holdings, summary)
    assert result["reconciled"] is False
    assert any("total value" in p for p in result["problems"])


@given(st.lists(_holding, min_size=1, max_size=40))
def test_a_confidential_omission_is_not_reported_as_a_parse_failure(holdings):
    """
    A filer may lawfully withhold positions pending a confidential treatment
    request. Then the table is *meant* to be short, and calling that a parse
    failure would be the tool misreading the law.
    """
    summary = {"entries": sum(h["rows"] for h in holdings) + 25,
               "value": sum(h["value"] for h in holdings),
               "confidential_omitted": True}
    result = ef.reconcile_13f(holdings, summary)
    assert result["reconciled"] is True
    assert any("withheld" in c for c in result["checks"])


def test_undeclared_totals_are_reported_as_unchecked_not_as_agreement():
    result = ef.reconcile_13f([{"value": 10.0, "rows": 1}], {})
    assert result["reconciled"] is True
    assert any("not declared" in c for c in result["checks"]), (
        "an absent total must be visible as absent, not silently passed")


# =====================================================================
# Form 4 chaining
# =====================================================================

def _txn(shares, after, direction="acquire", derivative=False):
    return {"shares": shares, "shares_after": after, "direction": direction,
            "derivative": derivative}


@given(st.lists(st.tuples(st.booleans(),
                          st.floats(min_value=1, max_value=1e5,
                                    allow_nan=False, allow_infinity=False)),
                min_size=2, max_size=12),
       st.floats(min_value=1e5, max_value=1e7))
def test_a_consistent_chain_reconciles(moves, opening):
    balance = opening
    transactions = [_txn(0.0, balance)]
    for dispose, size in moves:
        size = min(size, balance) if dispose else size
        balance = balance - size if dispose else balance + size
        transactions.append(
            _txn(size, balance, "dispose" if dispose else "acquire"))
    result = ef.reconcile_form4({"transactions": transactions})
    assert result["reconciled"] is True, result["problems"]


@given(st.floats(min_value=100, max_value=1e6), st.floats(min_value=10, max_value=1e4))
def test_a_direction_read_backwards_is_caught(opening, size):
    """
    The sign of an insider trade is the whole point of reading one. A sale
    booked as a purchase lands the running total somewhere the filing does not
    claim.
    """
    transactions = [_txn(0.0, opening),
                    _txn(size, opening - size, "acquire")]   # says buy, maths says sell
    result = ef.reconcile_form4({"transactions": transactions})
    assert result["reconciled"] is False
    assert any("running total" in p for p in result["problems"])


def test_derivative_rows_are_not_chained_against_share_counts():
    """
    An option or RSU grant carries its own running balance. Chaining the two
    together compares a share count against an option count -- a mismatch that
    means nothing and would cry wolf on most real filings.
    """
    transactions = [_txn(0.0, 1000.0),
                    _txn(500.0, 4000.0, "acquire", derivative=True),
                    _txn(200.0, 800.0, "dispose")]
    assert ef.reconcile_form4({"transactions": transactions})["reconciled"] is True


def test_a_chain_too_short_to_check_says_so_rather_than_passing():
    for transactions in ([], [_txn(10.0, 100.0)]):
        assert ef.reconcile_form4({"transactions": transactions})["reconciled"] is None


# =====================================================================
# Form 144
# =====================================================================

@given(st.floats(min_value=1, max_value=1e7), st.floats(min_value=0.5, max_value=5000))
def test_a_plausible_price_reconciles(shares, price):
    parsed = {"shares_to_sell": shares, "aggregate_market_value": shares * price}
    assert ef.reconcile_form144(parsed)["reconciled"] is True


@given(st.floats(min_value=1000, max_value=1e6))
def test_a_units_mistake_shows_up_as_an_implausible_price(shares):
    """
    The failure a magnitude error produces: a value in thousands divided by a
    share count in units gives a price no share trades at. It reads as a real
    number, which is why a range check catches what an eyeball does not.
    """
    parsed = {"shares_to_sell": shares, "aggregate_market_value": shares * 1e9}
    assert ef.reconcile_form144(parsed)["reconciled"] is False


@settings(max_examples=25)
@given(st.sampled_from([{}, {"shares_to_sell": 0}, {"aggregate_market_value": 0},
                        {"shares_to_sell": None, "aggregate_market_value": 5}]))
def test_a_missing_pair_is_unchecked_rather_than_failed(parsed):
    assert ef.reconcile_form144(parsed)["reconciled"] is None
