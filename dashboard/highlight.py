"""
Mark the figures and terms worth noticing inside a block of filing text.

A hover preview is only useful if the eye lands on the right thing. Filing prose
buries the number in a sentence -- "resulting in a net charge of $1.2 billion" --
so the preview highlights money, percentages, share counts and dates, plus a
small vocabulary of terms that change how a filing reads.

Escaping happens here, before any markup is added. The input is text pulled from
a third-party document, so it is treated as hostile: everything is escaped first
and only then are our own <mark> tags introduced. Highlighting is cosmetic and
must never be a way for a filing to inject markup into the dashboard.
"""
import html
import re

# Terms that change the reading of a filing. Deliberately short: a highlighter
# that marks everything marks nothing.
KEY_TERMS = (
    "going concern", "material weakness", "restatement", "non-reliance",
    "impairment", "goodwill impairment", "write-down", "writedown",
    "default", "covenant", "breach", "bankruptcy", "chapter 11",
    "delisting", "subpoena", "investigation", "settlement", "litigation",
    "resigned", "resignation", "terminated", "appointed", "succeed",
    "guidance", "outlook", "raised", "lowered", "withdrawn", "reaffirmed",
    "record revenue", "beat", "miss", "shortfall",
    "acquisition", "merger", "divestiture", "spin-off", "tender offer",
    "buyback", "repurchase", "dividend", "special dividend",
    "10b5-1", "restructuring", "layoff", "workforce reduction",
)

_FIGURE = re.compile(
    r"""(
        \$\s?\d[\d,]*(?:\.\d+)?\s?(?:billion|million|thousand|bn|mm|m|k|b)?   # money
      | \d[\d,]*(?:\.\d+)?\s?%                                                # percent
      | \d[\d,]*(?:\.\d+)?\s?(?:billion|million|thousand)\s+shares?           # share counts
      | \b\d{4}-\d{2}-\d{2}\b                                                 # ISO date
      | \b\d{1,2}/\d{1,2}/\d{2,4}\b                                           # US date
      | \b(?:January|February|March|April|May|June|July|August|September|
            October|November|December)\s+\d{1,2},?\s+\d{4}\b
    )""",
    re.VERBOSE | re.IGNORECASE,
)

_TERMS = re.compile(r"\b(" + "|".join(re.escape(t) for t in KEY_TERMS) + r")\b", re.IGNORECASE)


def highlight(text: str, limit: int = 700) -> str:
    """
    Escaped HTML with <mark class="fm-fig"> on figures and <mark class="fm-term">
    on notable terms. Truncates on a word boundary so a preview never ends
    mid-number, which would misread as a different number.
    """
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return ""

    if len(raw) > limit:
        cut = raw[:limit]
        space = cut.rfind(" ")
        raw = (cut[:space] if space > limit * 0.6 else cut) + "…"

    safe = html.escape(raw)
    # Terms first, then figures: a figure inside an already-marked term would
    # otherwise nest tags. Both patterns skip anything already inside a tag.
    safe = _TERMS.sub(lambda m: f'<mark class="fm-term">{m.group(0)}</mark>', safe)
    safe = _FIGURE.sub(
        lambda m: m.group(0) if "<mark" in m.group(0) else f'<mark class="fm-fig">{m.group(0)}</mark>',
        safe)
    return safe


def summarise_filing(filing: dict, describe) -> str:
    """
    One line of what a filing says, before any highlighting.

    Prefers the item codes (which carry the news for an 8-K) over the form name,
    and falls back to EDGAR's own description.
    """
    parts = [describe(filing.get("form", ""), filing.get("items", ""))]
    desc = (filing.get("description") or "").strip()
    if desc and desc.upper() not in ("", parts[0].upper()) and not desc.upper().startswith("FORM "):
        parts.append(desc)
    if filing.get("report_date"):
        parts.append(f"Period {filing['report_date']}")
    return " · ".join(p for p in parts if p)
