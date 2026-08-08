"""
Form 4 parsing tests.

The question these exist to answer correctly is the one an analyst actually
asks: "was that sale under a 10b5-1 plan?" It is a boolean in the XML, and
getting it wrong — or conflating "not disclosed" with "no" — changes the
reading of the trade entirely.

The fixture below mirrors a real Micron Form 4.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import edgar_forms as ef


FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-07-24</periodOfReport>
  <issuer>
    <issuerName>MICRON TECHNOLOGY INC</issuerName>
    <issuerTradingSymbol>MU</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>MEHROTRA SANJAY</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>President and CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>true</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-07-24</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1050.00</value></transactionShares>
        <transactionPricePerShare><value>942.87</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>312168.00</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <footnoteId id="F1"/>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-07-15</value></transactionDate>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>663.00</value></transactionShares>
        <transactionPricePerShare><value>983.12</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <footnotes>
    <footnote id="F1">The sales reported were effected pursuant to a Rule 10b5-1 trading plan.</footnote>
  </footnotes>
</ownershipDocument>"""


@pytest.fixture
def parsed():
    return ef.parse_form4(FORM4)


# =====================================================================
# The 10b5-1 question
# =====================================================================

def test_detects_a_10b5_1_plan(parsed):
    assert parsed["plan_10b5_1"] is True


def test_detects_the_absence_of_a_plan():
    assert ef.parse_form4(FORM4.replace("<aff10b5One>true", "<aff10b5One>false"))["plan_10b5_1"] is False


def test_missing_flag_is_unknown_not_false():
    """
    Older forms predate the checkbox. Reporting "no plan" for a filing that
    simply did not say is a different claim from the one the document makes.
    """
    stripped = FORM4.replace("<aff10b5One>true</aff10b5One>", "")
    assert ef.parse_form4(stripped)["plan_10b5_1"] is None


def test_plan_footnote_is_captured(parsed):
    assert any("10b5-1" in fn for fn in parsed["footnotes"])
    assert "10b5-1" in parsed["transactions"][0]["footnotes"][0]


# =====================================================================
# Who and what
# =====================================================================

def test_identifies_the_insider_and_roles(parsed):
    assert parsed["owner"] == "MEHROTRA SANJAY"
    assert parsed["is_officer"] and parsed["is_director"]
    assert not parsed["is_ten_percent_owner"]
    assert "President and CEO" in parsed["roles"]
    assert "Director" in parsed["roles"]


def test_identifies_the_issuer(parsed):
    assert parsed["issuer"] == "MICRON TECHNOLOGY INC"
    assert parsed["issuer_ticker"] == "MU"


def test_transaction_values(parsed):
    sale = parsed["transactions"][0]
    assert sale["date"] == "2026-07-24"
    assert sale["code"] == "S"
    assert sale["shares"] == pytest.approx(1050.0)
    assert sale["price"] == pytest.approx(942.87)
    assert sale["value"] == pytest.approx(1050 * 942.87)
    assert sale["shares_after"] == pytest.approx(312168.0)
    assert sale["direction"] == "dispose"


# =====================================================================
# Decisions vs compensation mechanics
# =====================================================================

def test_open_market_sale_is_a_decision(parsed):
    assert parsed["transactions"][0]["is_open_market_decision"] is True


def test_tax_withholding_is_not_a_decision(parsed):
    """
    Code F is shares withheld to pay tax on a vesting grant. Counting it as
    "the insider sold" is how a routine payroll event becomes a bearish
    headline.
    """
    withholding = parsed["transactions"][1]
    assert withholding["code"] == "F"
    assert withholding["code_label"] == "Shares withheld for tax"
    assert withholding["is_open_market_decision"] is False


@pytest.mark.parametrize("code,decision", [
    ("P", True), ("S", True),
    ("A", False), ("F", False), ("M", False), ("G", False), ("X", False),
])
def test_only_open_market_trades_count_as_decisions(code, decision):
    assert ef.TRANSACTION_CODES[code][2] is decision


def test_flow_summary_separates_decisions_from_mechanics(parsed):
    flow = ef.summarise_insider_flow([parsed])
    assert flow["open_market_sold_value"] == pytest.approx(1050 * 942.87)
    assert flow["non_discretionary_value"] == pytest.approx(663 * 983.12)
    assert flow["sales_under_10b5_1"] == 1
    assert flow["sales_not_under_10b5_1"] == 0


def test_flow_summary_counts_an_unplanned_sale():
    unplanned = ef.parse_form4(FORM4.replace("<aff10b5One>true", "<aff10b5One>false"))
    flow = ef.summarise_insider_flow([unplanned])
    assert flow["sales_not_under_10b5_1"] == 1
    assert flow["sales_under_10b5_1"] == 0


def test_purchases_net_against_sales():
    buy = FORM4.replace("<transactionCode>S<", "<transactionCode>P<") \
               .replace("<value>D</value>", "<value>A</value>")
    flow = ef.summarise_insider_flow([ef.parse_form4(buy)])
    assert flow["open_market_bought_value"] > 0
    assert flow["net_value"] > 0


# =====================================================================
# Robustness
# =====================================================================

def test_empty_document_does_not_raise():
    out = ef.parse_form4("<ownershipDocument></ownershipDocument>")
    assert out["transactions"] == []
    assert out["plan_10b5_1"] is None


def test_derivative_transactions_are_flagged():
    xml = FORM4.replace("nonDerivativeTable", "derivativeTable") \
               .replace("nonDerivativeTransaction", "derivativeTransaction")
    assert ef.parse_form4(xml)["transactions"][0]["derivative"] is True


def test_xsl_rendered_path_is_rewritten_to_the_real_xml(monkeypatch):
    """
    EDGAR reports primaryDocument as "xslF345X06/primarydocument.xml" — the
    rendered HTML, despite the .xml suffix. Fetching that yields no fields.
    """
    seen = {}

    def fake(url):
        seen["url"] = url
        return FORM4

    monkeypatch.setattr(ef, "_fetch", fake)
    ef.fetch_form4_xml("0000723125", "0001242654-26-000014", "xslF345X06/primarydocument.xml")
    assert "xslF345X06" not in seen["url"]
    assert seen["url"].endswith("/primarydocument.xml")


def test_8k_item_codes_are_named():
    described = ef.describe_8k_items("2.02,9.01")
    assert any("Results of operations" in d for d in described)
    assert any("Financial statements" in d for d in described)


# =====================================================================
# Prose sections: boundaries are heuristic, and must say so
# =====================================================================

TENK = """
TABLE OF CONTENTS
Item 1. Business 3
Item 1A. Risk Factors 12
Item 3. Legal Proceedings 40
Item 7. Management's Discussion 50

ITEM 1. BUSINESS
We make memory. """ + ("Business detail. " * 200) + """
See Item 1A. Risk Factors for a discussion of these potential impacts.

ITEM 1A. RISK FACTORS
""" + ("A risk we face. " * 500) + """

ITEM 3. LEGAL PROCEEDINGS
For a discussion see Item 8.

ITEM 7. MANAGEMENT'S DISCUSSION
""" + ("Revenue rose. " * 300)


def test_section_extraction_finds_the_real_heading_not_a_cross_reference():
    """
    "See Item 1A. Risk Factors for a discussion..." sits mid-sentence inside
    Item 1. Latching onto it starts the section in the wrong place.
    """
    body, meta = ef.extract_section(TENK, "1A", budget=50_000)
    assert meta["found"] and meta["confidence"] == "high"
    assert body.startswith("ITEM 1A. RISK FACTORS")
    assert "A risk we face." in body
    assert "We make memory" not in body


def test_section_stops_at_the_next_item():
    body, _ = ef.extract_section(TENK, "1A", budget=50_000)
    assert "LEGAL PROCEEDINGS" not in body


def test_table_of_contents_entries_are_not_chosen():
    body, _ = ef.extract_section(TENK, "7", budget=50_000)
    assert body.startswith("ITEM 7.")
    assert "Revenue rose." in body


def test_budget_truncates_and_reports_it():
    body, meta = ef.extract_section(TENK, "1A", budget=300)
    assert len(body) <= 300
    assert meta["truncated"] is True
    assert meta["full_length"] > 300


def test_short_section_is_flagged_low_confidence():
    """Item 3 here is a one-line cross-reference — real, but worth a caveat."""
    _, meta = ef.extract_section(TENK, "3", budget=5000)
    assert meta["found"] and meta["confidence"] == "low"


def test_missing_section_is_reported_not_guessed():
    body, meta = ef.extract_section(TENK, "9A")
    assert body is None and meta["found"] is False


def test_search_returns_bounded_windows():
    hits = ef.search_filing(TENK, "memory", window=100, max_hits=3)
    assert hits and len(hits) <= 3
    assert all(len(h["excerpt"]) <= 140 for h in hits)


def test_html_flattening_drops_markup_and_scripts():
    raw = "<html><script>var x=1;</script><style>p{}</style><p>Hello&nbsp;World</p></html>"
    out = ef.html_to_text(raw)
    assert "Hello World" in out
    assert "var x" not in out and "p{}" not in out


# =====================================================================
# 13F
# =====================================================================

INFOTABLE = """<informationTable>
 <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
  <cusip>037833100</cusip><value>20000000</value>
  <shrsOrPrnAmt><sshPrnamt>80000000</sshPrnamt></shrsOrPrnAmt></infoTable>
 <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
  <cusip>037833100</cusip><value>15000000</value>
  <shrsOrPrnAmt><sshPrnamt>61000000</sshPrnamt></shrsOrPrnAmt></infoTable>
 <infoTable><nameOfIssuer>COCA COLA CO</nameOfIssuer><titleOfClass>COM</titleOfClass>
  <cusip>191216100</cusip><value>30000000</value>
  <shrsOrPrnAmt><sshPrnamt>400000000</sshPrnamt></shrsOrPrnAmt></infoTable>
</informationTable>"""


def test_13f_parses_every_row():
    rows = ef.parse_13f(INFOTABLE)
    assert len(rows) == 3
    assert rows[0]["issuer"] == "APPLE INC"
    assert rows[0]["cusip"] == "037833100"
    assert rows[0]["value"] == pytest.approx(20_000_000)
    assert rows[0]["shares"] == pytest.approx(80_000_000)
