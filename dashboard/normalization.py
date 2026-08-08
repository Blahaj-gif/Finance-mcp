"""
Text and number normalization for data arriving from public feeds.

Company names, filing descriptions and government release text arrive in a
variety of encodings and scripts: full-width CJK punctuation, accented Latin,
Cyrillic and Greek transliterations, non-ASCII digits, HTML entities, and
numbers written with European or Indian grouping conventions. Comparing or
parsing any of that naively produces silent mismatches -- "Société Générale"
failing to match "Societe Generale", or "1.234,56" parsed as 1.234.

Everything here is pure and dependency-free, so it is cheap to test.
"""
import html
import re
import unicodedata

# Digits outside ASCII that appear in filings and international press releases.
_DIGIT_MAP = {}
for _base, _name in (
    (0x0660, "arabic-indic"), (0x06F0, "extended arabic-indic"),
    (0x0966, "devanagari"), (0x09E6, "bengali"), (0x0A66, "gurmukhi"),
    (0x0AE6, "gujarati"), (0x0B66, "oriya"), (0x0BE6, "tamil"),
    (0x0C66, "telugu"), (0x0CE6, "kannada"), (0x0D66, "malayalam"),
    (0x0E50, "thai"), (0x0ED0, "lao"), (0x0F20, "tibetan"),
    (0x1040, "myanmar"), (0x17E0, "khmer"), (0xFF10, "fullwidth"),
):
    for _i in range(10):
        _DIGIT_MAP[chr(_base + _i)] = str(_i)

_SCRIPT_RANGES = [
    # Includes the fullwidth forms block so "ＡＰＰＬＥ" is recognised as Latin
    # before NFKC folds it.
    ("Latin",      [(0x0041, 0x024F), (0x1E00, 0x1EFF), (0xFF21, 0xFF3A), (0xFF41, 0xFF5A)]),
    ("Greek",      [(0x0370, 0x03FF), (0x1F00, 0x1FFF)]),
    ("Cyrillic",   [(0x0400, 0x04FF)]),
    ("Hebrew",     [(0x0590, 0x05FF)]),
    ("Arabic",     [(0x0600, 0x06FF), (0x0750, 0x077F)]),
    ("Devanagari", [(0x0900, 0x097F)]),
    ("Thai",       [(0x0E00, 0x0E7F)]),
    ("Han",        [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)]),
    ("Hiragana",   [(0x3040, 0x309F)]),
    ("Katakana",   [(0x30A0, 0x30FF)]),
    ("Hangul",     [(0xAC00, 0xD7AF), (0x1100, 0x11FF)]),
]

_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_digits(text: str) -> str:
    """Map non-ASCII decimal digits to ASCII, leaving everything else alone."""
    if not text:
        return text
    return "".join(_DIGIT_MAP.get(ch, ch) for ch in text)


def normalize_text(text: str, form: str = "NFKC") -> str:
    """
    Canonical form for comparison and display.

    NFKC folds compatibility characters -- full-width Latin, ligatures, CJK
    punctuation -- into their ordinary equivalents, which is what makes
    "ＡＰＰＬＥ" and "APPLE" comparable. HTML entities are decoded first
    because feeds double-encode them routinely.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    text = html.unescape(text)
    text = unicodedata.normalize(form, text)
    text = normalize_digits(text)
    text = _CONTROL.sub(" ", text)
    return _WS.sub(" ", text).strip()


# Letters that NFKD does not decompose -- they are atomic, not a base plus a
# combining mark -- so a naive fold deletes them and "Ærø Bank" becomes
# "r Bank". Common across Nordic, German and Central European filer names.
_ATOMIC_LATIN = {
    "Æ": "AE", "æ": "ae", "Ø": "O", "ø": "o", "Å": "A", "å": "a",
    "Ð": "D", "ð": "d", "Þ": "Th", "þ": "th", "ß": "ss", "ẞ": "SS",
    "Œ": "OE", "œ": "oe", "Ł": "L", "ł": "l", "Đ": "D", "đ": "d",
    "Ħ": "H", "ħ": "h", "Ŀ": "L", "ŀ": "l", "Ŋ": "N", "ŋ": "n",
    "Ŧ": "T", "ŧ": "t", "Ə": "E", "ə": "e", "Ɖ": "D", "Ɵ": "O",
}


def to_ascii(text: str) -> str:
    """
    Best-effort ASCII fold, for matching rather than display.

    Strips combining marks so "Société Générale" matches "Societe Generale",
    and expands atomic Latin letters that have no decomposition. Scripts with
    no Latin equivalent (Han, Arabic) drop out -- callers should keep the
    normalized original alongside this.
    """
    if not text:
        return ""
    text = normalize_text(text)
    text = "".join(_ATOMIC_LATIN.get(c, c) for c in text)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.encode("ascii", "ignore").decode("ascii").strip()


def detect_scripts(text: str) -> list:
    """Which writing systems appear in `text`, most frequent first."""
    if not text:
        return []
    counts = {}
    for ch in text:
        cp = ord(ch)
        if ch.isspace() or not ch.isalpha():
            continue
        for name, ranges in _SCRIPT_RANGES:
            if any(lo <= cp <= hi for lo, hi in ranges):
                counts[name] = counts.get(name, 0) + 1
                break
    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def parse_number(value, locale_hint: str = None):
    """
    Parse a number that may be written in any common convention.

    Handles ASCII and non-ASCII digits, thousands separators (comma, period,
    space, apostrophe, narrow no-break space), both decimal separators,
    parenthesised negatives used in financial statements, and trailing percent
    or currency symbols.

    `locale_hint` of "eu" forces the European reading of an ambiguous string
    like "1.234" (1234 rather than 1.234). Returns None when there is no number.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = normalize_text(str(value))
    if not s:
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):        # (1,234) == -1234
        negative, s = True, s[1:-1]

    s = re.sub(r"[^\d,.\-+−\s'  ]", "", s)   # drop currency/percent
    s = s.replace("−", "-")                             # unicode minus
    s = re.sub(r"[\s  ']", "", s)                  # space/apostrophe grouping
    if not s or not re.search(r"\d", s):
        return None

    if s.count("-") and not s.startswith("-"):
        s = s.replace("-", "")
    if s.startswith("-"):
        negative = not negative
        s = s[1:]
    s = s.lstrip("+")

    last_comma, last_dot = s.rfind(","), s.rfind(".")

    if last_comma >= 0 and last_dot >= 0:
        # Whichever comes last is the decimal separator.
        if last_comma > last_dot:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif last_comma >= 0:
        tail = s[last_comma + 1:]
        if s.count(",") > 1:
            # More than one comma can only be grouping: 1,234,567.
            s = s.replace(",", "")
        elif len(tail) == 3 and locale_hint not in ("eu", "decimal_comma"):
            # A lone comma three digits from the end is grouping in US style.
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif last_dot >= 0:
        tail = s[last_dot + 1:]
        if s.count(".") > 1 or (len(tail) == 3 and locale_hint == "eu"):
            s = s.replace(".", "")

    try:
        out = float(s)
    except ValueError:
        return None
    return -out if negative else out


def normalize_record(record: dict, text_keys=(), number_keys=()) -> dict:
    """Apply the right normalizer to each field of a feed record."""
    out = dict(record)
    for k in text_keys:
        if k in out:
            out[k] = normalize_text(out[k])
    for k in number_keys:
        if k in out:
            out[k] = parse_number(out[k])
    return out
