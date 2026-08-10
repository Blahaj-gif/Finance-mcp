"""
Normalization tests.

Feed data arrives in whatever script and number convention the filer used.
These pin the two failure modes that matter: a name that will not match its own
ASCII form, and a number silently parsed off by a factor of a thousand.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import normalization as nz


# =====================================================================
# Text
# =====================================================================

@pytest.mark.parametrize("raw,expected", [
    ("Société Générale", "Société Générale"),
    ("ＡＰＰＬＥ　ＩＮＣ", "APPLE INC"),          # fullwidth folds under NFKC
    ("Toyota&nbsp;Motor", "Toyota Motor"),        # HTML entity
    ("  spaced  out\t\n ", "spaced out"),    # NBSP + control whitespace
    ("café", "café"),
    ("", ""),
    (None, ""),
])
def test_normalize_text(raw, expected):
    assert nz.normalize_text(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Société Générale", "Societe Generale"),
    ("Ærø Bank", "AEro Bank"),
    ("Škoda", "Skoda"),
    ("ＡＰＰＬＥ", "APPLE"),
])
def test_to_ascii_folds_diacritics(raw, expected):
    assert nz.to_ascii(raw) == expected


def test_ascii_fold_makes_accented_names_matchable():
    """The point of the fold: two spellings of one company compare equal."""
    assert nz.to_ascii("Société Générale") == nz.to_ascii("Societe Generale")


@pytest.mark.parametrize("raw,script", [
    ("Apple Inc", "Latin"),
    ("Тинькофф", "Cyrillic"),
    ("自動車", "Han"),
    ("ΑΛΦΑ", "Greek"),
    ("บริษัท", "Thai"),
    ("ＡＰＰＬＥ", "Latin"),
])
def test_detect_scripts(raw, script):
    assert nz.detect_scripts(raw)[0] == script


def test_detect_scripts_reports_mixed_content():
    scripts = nz.detect_scripts("Toyota 自動車 株式会社")
    assert "Latin" in scripts and "Han" in scripts


# =====================================================================
# Numbers — the expensive failure mode
# =====================================================================

@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", 1234.56),      # US
    ("1.234,56", 1234.56),      # European
    ("1 234,56", 1234.56),      # French, space grouping
    ("1'234.56", 1234.56),      # Swiss
    ("1234.56", 1234.56),
    ("(1,234.56)", -1234.56),   # accounting negative
    ("-1,234.56", -1234.56),
    ("−1234.56", -1234.56),     # unicode minus
    ("12.5%", 12.5),
    ("$1,000", 1000.0),
    ("2,500", 2500.0),          # comma grouping three digits
    ("1.234", 1.234),           # ambiguous: read as decimal by default
    ("0", 0.0),
    ("", None),
    ("n/a", None),
    (None, None),
    (42, 42.0),
    (3.5, 3.5),
])
def test_parse_number(raw, expected):
    got = nz.parse_number(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


@pytest.mark.parametrize("raw,expected", [("٤٥٦", 456.0), ("१२३", 123.0), ("１２３", 123.0), ("๗๘๙", 789.0)])
def test_parse_non_ascii_digits(raw, expected):
    assert nz.parse_number(raw) == pytest.approx(expected)


def test_locale_hint_resolves_the_ambiguous_case():
    """'1.234' is 1.234 in the US and 1234 in Europe; the caller decides."""
    assert nz.parse_number("1.234") == pytest.approx(1.234)
    assert nz.parse_number("1.234", locale_hint="eu") == pytest.approx(1234.0)


def test_multiple_group_separators_are_not_decimals():
    assert nz.parse_number("1.234.567") == pytest.approx(1234567.0)
    assert nz.parse_number("1,234,567") == pytest.approx(1234567.0)


def test_normalize_record_applies_the_right_parser_per_field():
    rec = {"name": "Société  Générale", "value": "1.234,56", "other": "untouched"}
    out = nz.normalize_record(rec, text_keys=("name",), number_keys=("value",))
    assert out["name"] == "Société Générale"
    assert out["value"] == pytest.approx(1234.56)
    assert out["other"] == "untouched"


# =====================================================================
# Scientific notation
# =====================================================================

@pytest.mark.parametrize("text,expected", [
    ("1.2e3", 1200.0),
    ("1.2E9", 1.2e9),
    ("-3.5e-2", -0.035),
    ("(1.5e3)", -1500.0),
    ("2.5e0", 2.5),
])
def test_scientific_notation_keeps_its_exponent(text, expected):
    """
    The character strip that removes currency symbols also removed the "e", so
    "1.2e3" came back as 1.23 -- not a refusal, a confidently wrong number three
    orders of magnitude out. XBRL and JSON feeds both emit this form.
    """
    assert nz.parse_number(text) == pytest.approx(expected)


def test_an_overflowing_exponent_is_refused_not_returned_as_infinity():
    assert nz.parse_number("1e400") is None


def test_ordinary_numbers_are_unaffected_by_the_exponent_path():
    assert nz.parse_number("1,234.56") == pytest.approx(1234.56)
    assert nz.parse_number("(1,234.56)") == pytest.approx(-1234.56)
    assert nz.parse_number("1.234,56") == pytest.approx(1234.56)
