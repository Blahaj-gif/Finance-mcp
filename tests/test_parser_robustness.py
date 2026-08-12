"""
What the filing parsers do with input that is not the happy path.

A parser for public filings meets whatever the filer's software produced.
Crashing is the visible failure and the parsers already survive it — every one
of these inputs returns rather than raises. The dangerous failure is the quiet
one: a value that comes back looking like a value and is not.

That is what these test. The CDATA case below was a real defect: `<[^>]+>`
matches from `<!` to the first `>` inside a CDATA section, so
`<![CDATA[Acme <&> Co]]>` returned "Co]]>" — the tail after the accidental
match, which reads like an issuer name rather than like a parse failure.

No machine learning here, deliberately. These are schema-defined documents;
a probabilistic parse could not be reconciled against the totals the filings
declare about themselves, and that reconciliation is the only reason to trust
the numbers at all.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import edgar_forms as ef

#: Shapes a real filer's software actually emits, plus a few a corrupted
#: download would.
HOSTILE = {
    "empty": "",
    "not xml at all": "<<<>>> not a document",
    "truncated mid-tag": "<ownershipDocument><issuer><issuerName>Acme",
    "namespaced": '<ns2:ownershipDocument xmlns:ns2="x">'
                  "<ns2:issuerName>Acme</ns2:issuerName></ns2:ownershipDocument>",
    "cdata": "<ownershipDocument><issuerName><![CDATA[Acme <&> Co]]>"
             "</issuerName></ownershipDocument>",
    "entities": "<ownershipDocument><issuerName>AT&amp;T &lt;Inc&gt;"
                "</issuerName></ownershipDocument>",
    "nested same tag": "<infoTable><infoTable><value>5</value></infoTable>"
                       "<value>10</value></infoTable>",
    "attributes on the value": '<infoTable><value units="x">1,234</value></infoTable>',
    "utf-8 bom": "﻿<ownershipDocument><issuerName>Acme</issuerName>"
                 "</ownershipDocument>",
    "html not xml": "<html><body><table><tr><td>value</td></tr></table></body></html>",
    "very long value": "<infoTable><value>" + "9" * 5000 + "</value></infoTable>",
    "null byte": "<infoTable><value>12\x0034</value></infoTable>",
    "unclosed tag": "<infoTable><value>10<value></infoTable>",
    "wrong document type": "<nport><invstOrSec><name>Bond</name></invstOrSec></nport>",
}

PARSERS = {
    "parse_form4": ef.parse_form4,
    "parse_13f": ef.parse_13f,
    "parse_13dg": ef.parse_13dg,
    "parse_form144": ef.parse_form144,
    "parse_nport": ef.parse_nport,
    "parse_holdings": ef.parse_holdings,
}


@pytest.mark.parametrize("label", sorted(HOSTILE))
@pytest.mark.parametrize("name", sorted(PARSERS))
def test_no_parser_raises_on_malformed_input(name, label):
    """
    A filing that cannot be read must come back empty, not as a traceback: one
    bad document in a list of twenty should not take the other nineteen with
    it.
    """
    result = PARSERS[name](HOSTILE[label])
    assert result is not None
    assert isinstance(result, (dict, list))


# =====================================================================
# Extraction, which is the part that can be quietly wrong
# =====================================================================

@pytest.mark.parametrize("xml,expected", [
    ("<issuerName>Acme Corp</issuerName>", "Acme Corp"),
    ('<ns2:issuerName>Acme Corp</ns2:issuerName>', "Acme Corp"),
    ("<issuerName>AT&amp;T</issuerName>", "AT&T"),
    ("<issuerName>\n   Acme Corp\n  </issuerName>", "Acme Corp"),
    ('<issuerName id="7">Acme Corp</issuerName>', "Acme Corp"),
    ("<issuerName>Acme <b>Corp</b></issuerName>", "Acme Corp"),
])
def test_ordinary_filing_syntax_reads_correctly(xml, expected):
    assert ef._text(xml, "issuerName") == expected


@pytest.mark.parametrize("xml,expected", [
    ("<issuerName><![CDATA[Acme <&> Co]]></issuerName>", "Acme <&> Co"),
    ("<issuerName><![CDATA[A <b>bold</b> name]]></issuerName>", "A <b>bold</b> name"),
    ("<issuerName><![CDATA[Acme]]> <![CDATA[Corp]]></issuerName>", "Acme Corp"),
])
def test_cdata_is_content_not_markup(xml, expected):
    """
    The defect: the tag stripper matched from `<!` to the first `>` inside the
    section and returned whatever followed. `<![CDATA[Acme <&> Co]]>` came back
    as "Co]]>" — not obviously wrong to a reader, which is what made it worth
    fixing. SEC filers use CDATA for exactly the names containing the
    characters that break naive stripping.
    """
    assert ef._text(xml, "issuerName") == expected


def test_an_absent_tag_is_absent_rather_than_empty():
    """None and "" are different answers: one is "not filed", the other is
    "filed blank", and only one of them should ever be shown as a value."""
    assert ef._text("<other>x</other>", "issuerName") is None
    assert ef._text("<other>x</other>", "issuerName", default="?") == "?"


def test_a_wrong_document_type_yields_nothing_rather_than_wrong_things():
    """
    A 13F parser handed a Form 4 must not find rows in it. Returning something
    plausible from the wrong document is worse than returning nothing.
    """
    form4 = ("<ownershipDocument><issuerName>Acme</issuerName>"
             "<nonDerivativeTransaction><transactionShares><value>100</value>"
             "</transactionShares></nonDerivativeTransaction></ownershipDocument>")
    assert ef.parse_13f(form4) == []
    assert ef.parse_nport(form4)["holdings"] == []


def test_numbers_keep_their_magnitude_through_the_parser():
    """
    Every serious bug this project has had was a number that looked plausible
    and was off by a factor: a percentage times a hundred, cash-flow tags read
    cumulatively, an exponent eaten by a character strip.
    """
    xml = ("<infoTable><nameOfIssuer>ACME</nameOfIssuer>"
           "<value>1,234,567</value><sshPrnamt>89,000</sshPrnamt></infoTable>")
    row = ef.parse_13f(xml)[0]
    assert row["value"] == pytest.approx(1_234_567)
    assert row["shares"] == pytest.approx(89_000)


def test_a_truncated_download_does_not_produce_half_a_holding():
    """
    A download cut mid-document should yield the rows that completed, not a
    final row with some fields filled and the rest silently defaulted.
    """
    truncated = ("<infoTable><nameOfIssuer>FIRST</nameOfIssuer><value>100</value>"
                 "</infoTable><infoTable><nameOfIssuer>SECOND</nameOfIssuer><val")
    rows = ef.parse_13f(truncated)
    assert [r["issuer"] for r in rows] == ["FIRST"]
