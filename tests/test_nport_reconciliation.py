"""
NPORT reconciliation: the check that works, and the one that does not.

The obvious idea was to compare the sum of holdings against the total or net
assets the filing declares. Measured on Vanguard 500 (519 holdings, $1.42tn)
the sum sits 0.055% below totAssets and 0.136% above netAssets, because a fund
holds cash and receivables and owes liabilities and none of that appears in
`invstOrSec`. A tolerance wide enough to accept that is too wide to catch
anything.

Every holding files both a value and a percentage, which is a better check and
a per-row one.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import edgar_forms as ef

NET = 1_000_000.0


def _holding(value, pct, name="ACME"):
    return {"name": name, "value_usd": value, "pct_of_fund": pct}


def _parsed(holdings, net=NET, total=None, holdings_value=None):
    return {"holdings": holdings, "net_assets": net,
            "total_assets": total if total is not None else (net * 1.002 if net else None),
            "holdings_value": (holdings_value if holdings_value is not None
                               else sum(h["value_usd"] for h in holdings))}


def test_a_faithful_parse_reconciles():
    holdings = [_holding(250_000, 25.0), _holding(100_000, 10.0),
                _holding(1_000, 0.1)]
    result = ef.reconcile_nport(_parsed(holdings))
    assert result["reconciled"] is True, result["problems"]
    assert result["checked"] == 3


def test_a_magnitude_error_is_caught():
    """
    The failure this exists for. A value read a hundredfold out still looks
    like a value; its implied percentage does not.
    """
    result = ef.reconcile_nport(_parsed([_holding(250_000 * 100, 25.0)]))
    assert result["reconciled"] is False
    assert "disagree" in result["problems"][0]
    assert result["disagreed"] == 1


def test_rounding_in_the_filed_percentage_is_tolerated():
    """Filers round pctVal. A check that fires on rounding is a check people
    learn to ignore."""
    value = 123_456.0
    exact = value / NET * 100
    result = ef.reconcile_nport(_parsed([_holding(value, round(exact, 4))]))
    assert result["reconciled"] is True


def test_the_worst_disagreement_is_named():
    holdings = [_holding(250_000, 25.0),
                _holding(100_000, 99.0, name="WRONG ONE")]
    result = ef.reconcile_nport(_parsed(holdings))
    assert result["reconciled"] is False
    assert "WRONG ONE" in result["problems"][0]


def test_the_gap_to_total_assets_is_reported_but_never_a_failure():
    """
    Securities are not all of a fund's assets. Reporting the remainder is
    useful; calling it a discrepancy would mean every fund fails.
    """
    result = ef.reconcile_nport(_parsed([_holding(900_000, 90.0)],
                                        total=1_000_000.0))
    assert result["reconciled"] is True
    assert any("cash and receivables" in c for c in result["checks"])


def test_a_filing_without_net_assets_is_unchecked_rather_than_passed():
    result = ef.reconcile_nport(_parsed([_holding(1.0, 1.0)], net=None))
    assert result["reconciled"] is None
    assert result["checked"] == 0


def test_holdings_missing_a_value_or_percentage_are_skipped_not_failed():
    holdings = [_holding(250_000, 25.0), {"name": "NO NUMBERS"},
                {"name": "VALUE ONLY", "value_usd": 5.0}]
    result = ef.reconcile_nport(_parsed(holdings, holdings_value=250_005.0))
    assert result["reconciled"] is True
    assert result["checked"] == 1


def test_reconciliation_covers_every_holding_not_the_displayed_slice():
    """
    fund_holdings takes a limit. Checking only what it shows would make the
    check easier the less of the filing you look at.
    """
    xml = "<netAssets>1000000</netAssets><totAssets>1002000</totAssets>" + "".join(
        f"<invstOrSec><name>H{i}</name><valUSD>1000</valUSD>"
        f"<pctVal>0.1</pctVal></invstOrSec>" for i in range(40))
    parsed = ef.parse_nport(xml, limit=5)
    assert len(parsed["holdings"]) == 5
    assert parsed["reconciliation"]["checked"] == 40


def test_the_sum_against_net_assets_is_not_used_as_the_test():
    """
    Pins the design decision. Vanguard 500's holdings sum to 0.136% above its
    filed net assets and the filing is correct; a check on that aggregate would
    fail a good parse of a good filing.
    """
    holdings = [_holding(1_001_360.0, 100.136)]
    result = ef.reconcile_nport(_parsed(holdings, net=NET, total=1_001_500.0))
    assert result["reconciled"] is True, (
        "the aggregate gap must not be treated as a discrepancy")
