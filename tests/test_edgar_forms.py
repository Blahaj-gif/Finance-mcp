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


# =====================================================================
# Form 3 / 5 — holdings rather than transactions
# =====================================================================

FORM3 = """<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-06-11</periodOfReport>
  <issuer><issuerName>MICRON TECHNOLOGY INC</issuerName>
          <issuerTradingSymbol>MU</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Bjorlin Alexis</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeHolding>
      <securityTitle><value>Common Stock</value></securityTitle>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>260</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>
        <natureOfOwnership><value>By Trust</value></natureOfOwnership>
      </ownershipNature>
      <footnoteId id="F1"/>
    </nonDerivativeHolding>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeHolding>
      <securityTitle><value>Stock Option</value></securityTitle>
      <conversionOrExercisePrice><value>85.50</value></conversionOrExercisePrice>
      <expirationDate><value>2032-01-15</value></expirationDate>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </derivativeHolding>
  </derivativeTable>
  <footnotes><footnote id="F1">Shares held in a Trust.</footnote></footnotes>
</ownershipDocument>"""


def test_form3_holdings_are_parsed():
    """
    A Form 3 is an insider's opening position and contains no transactions.
    Parsing only transactions returns nothing for a filing that is all data.
    """
    r = ef.parse_ownership_form(FORM3, form="3")
    assert r["transactions"] == []
    assert len(r["holdings"]) == 2
    assert r["form"] == "3"
    assert "Initial statement" in r["form_meaning"]


def test_form3_holding_details():
    holdings = ef.parse_ownership_form(FORM3, form="3")["holdings"]
    common = [h for h in holdings if not h["derivative"]][0]
    assert common["shares_held"] == pytest.approx(260)
    assert common["ownership"] == "I"
    assert common["nature"] == "By Trust"
    assert "Trust" in common["footnotes"][0]


def test_derivative_holdings_carry_strike_and_expiry():
    option = [h for h in ef.parse_ownership_form(FORM3, form="3")["holdings"] if h["derivative"]][0]
    assert option["exercise_price"] == pytest.approx(85.50)
    assert option["expiry"] == "2032-01-15"
    assert option["shares_held"] == pytest.approx(5000)


def test_form4_still_reports_transactions_through_the_general_parser():
    r = ef.parse_ownership_form(FORM4, form="4")
    assert len(r["transactions"]) == 2
    assert r["plan_10b5_1"] is True
    assert r["holdings"] == []


# =====================================================================
# DEF 14A — executive compensation via inline XBRL
# =====================================================================

PROXY = """<html><body>
<ix:nonFraction name="ecd:PeoTotalCompAmt" contextRef="c1" unitRef="usd" scale="0"
 >30940146</ix:nonFraction>
<ix:nonFraction name="ecd:PeoActuallyPaidCompAmt" contextRef="c1" unitRef="usd" scale="3"
 >86570</ix:nonFraction>
<ix:nonFraction name="ecd:NetIncomeLoss" contextRef="c1" unitRef="usd" sign="-" scale="0"
 >1234000</ix:nonFraction>
<ix:nonNumeric name="ecd:AwardTmgMnpiCnsdrdFlag" contextRef="c1">true</ix:nonNumeric>
<ix:nonNumeric name="ecd:InsiderTrdPoliciesProcAdoptedFlag" contextRef="c1">true</ix:nonNumeric>
<ix:nonFraction name="us-gaap:Revenues" contextRef="c1" unitRef="usd">999</ix:nonFraction>
</body></html>"""


def test_inline_xbrl_extracts_only_the_requested_taxonomy():
    facts = ef.parse_inline_xbrl(PROXY, "ecd")
    assert len(facts) == 5
    assert all(not f["concept"].startswith("Revenues") for f in facts)


def test_inline_xbrl_applies_scale():
    """scale="3" means the printed number is in thousands."""
    comp = ef.executive_compensation(PROXY)
    assert comp["facts"]["PeoActuallyPaidCompAmt"]["value"] == pytest.approx(86_570_000)


def test_inline_xbrl_applies_sign():
    comp = ef.executive_compensation(PROXY)
    assert comp["facts"]["NetIncomeLoss"]["value"] == pytest.approx(-1_234_000)


def test_executive_compensation_labels_and_flags():
    comp = ef.executive_compensation(PROXY)
    assert comp["found"] is True
    assert comp["facts"]["PeoTotalCompAmt"]["label"].startswith("CEO total compensation")
    assert comp["flags"]["AwardTmgMnpiCnsdrdFlag"]["value"] is True
    assert "material non-public" in comp["flags"]["AwardTmgMnpiCnsdrdFlag"]["label"]


def test_no_inline_xbrl_is_reported_not_faked():
    assert ef.executive_compensation("<html><p>Nothing tagged here.</p></html>")["found"] is False


# =====================================================================
# Schedule 13D / 13G
# =====================================================================

COVER = """
SCHEDULE 13D
CUSIP Number: 09857L108
5. SOLE VOTING POWER  5,034,170
6. SHARED VOTING POWER  0
7. SOLE DISPOSITIVE POWER  5,034,170
9. AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON WITH (9)   5,034,170
11. PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11)   5.2%
ITEM 4. PURPOSE OF TRANSACTION
The Reporting Person intends to engage with management regarding capital allocation.
ITEM 5. INTEREST IN SECURITIES
"""


def test_13d_cover_page_fields():
    s = ef.parse_13dg(COVER)
    assert s["fields"]["aggregate_amount"] == pytest.approx(5_034_170)
    assert s["fields"]["percent_of_class"] == pytest.approx(5.2)
    assert s["fields"]["sole_voting"] == pytest.approx(5_034_170)
    assert s["confidence"] == "high"


def test_13d_percent_survives_the_row_reference():
    """
    The label reads "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11)   5.2%".
    A gap pattern that stops at the first digit lands on the row number.
    """
    assert ef.parse_13dg(COVER)["fields"]["percent_of_class"] == pytest.approx(5.2)


def test_13d_rejects_a_word_as_a_cusip():
    """"CUSIP Number)" followed by prose must not yield "Number" as an identifier."""
    s = ef.parse_13dg("SCHEDULE 13D\nCUSIP Number)   David Maryles, Managing Director\n")
    assert "cusip" not in s["fields"]


def test_13d_extracts_purpose_of_transaction():
    s = ef.parse_13dg(COVER)
    assert "purpose_of_transaction" in s
    assert "capital allocation" in s["purpose_of_transaction"]


def test_13d_confidence_degrades_without_the_anchors():
    partial = ef.parse_13dg("SCHEDULE 13G\n5. SOLE VOTING POWER  1,000\n")
    assert partial["confidence"] == "medium"
    assert ef.parse_13dg("nothing useful here")["confidence"] == "low"


def test_13dg_xml_path():
    xml = """<edgarSubmission><issuerName>ACME CORP</issuerName>
      <issuerCUSIP>037833100</issuerCUSIP><aggregateAmountOwned>1234567</aggregateAmountOwned>
      <percentOfClass>7.5</percentOfClass><soleVotingPower>1234567</soleVotingPower>
      </edgarSubmission>"""
    s = ef.parse_13dg(xml, is_xml=True)
    assert s["source"] == "xml" and s["confidence"] == "high"
    assert s["fields"]["aggregate_amount"] == pytest.approx(1_234_567)
    assert s["fields"]["percent_of_class"] == pytest.approx(7.5)
    assert s["issuer"] == "ACME CORP"
