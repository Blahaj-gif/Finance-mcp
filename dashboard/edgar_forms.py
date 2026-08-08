"""
Structured extraction from SEC filings.

The point of this module is that most of what an analyst wants from a filing is
already a machine-readable field, not prose. "Was that sale under a 10b5-1
plan?" is answered by `<aff10b5One>` in the Form 4 XML -- a boolean. Reading
the filing into a language model to find that out would be slower, more
expensive, and less reliable than parsing it.

So: parse deterministically, return a bounded summary. The model gets an answer
rather than a document to read.

For the genuinely unstructured parts (8-K narrative, risk factors) the approach
is section extraction with a character budget, not a full dump.
"""
import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request

try:
    from dashboard import normalization as nz
    from dashboard import econ_calendar as ec
except ImportError:  # imported as a top-level module from dashboard/
    import normalization as nz
    import econ_calendar as ec


# Form 4 transaction codes. The distinction matters: an "S" is a decision to
# sell, an "F" is shares withheld to pay tax on a vesting grant. Treating the
# second as a bearish signal is a common and expensive misreading.
TRANSACTION_CODES = {
    "P": ("Open-market purchase", "acquire", True),
    "S": ("Open-market sale", "dispose", True),
    "A": ("Grant / award", "acquire", False),
    "D": ("Disposition to the issuer", "dispose", False),
    "F": ("Shares withheld for tax", "dispose", False),
    "M": ("Option/derivative exercise", "acquire", False),
    "C": ("Conversion of a derivative", "acquire", False),
    "E": ("Expiration (short position)", "dispose", False),
    "G": ("Gift", "dispose", False),
    "V": ("Voluntary early report", "other", False),
    "X": ("Exercise of in-the-money derivative", "acquire", False),
    "J": ("Other (see footnotes)", "other", False),
    "K": ("Equity swap", "other", False),
    "U": ("Tender of shares", "dispose", False),
}

# 8-K item codes worth surfacing by name.
EIGHT_K_ITEMS = {
    "1.01": "Entry into a material agreement",
    "1.02": "Termination of a material agreement",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Completion of an acquisition or disposition",
    "2.02": "Results of operations (earnings)",
    "2.03": "Creation of a material financial obligation",
    "2.04": "Triggering of a financial obligation",
    "2.05": "Costs of exit or disposal",
    "2.06": "Material impairment",
    "3.01": "Delisting or listing-standard failure",
    "3.02": "Unregistered sale of equity",
    "4.01": "Change of accountant",
    "4.02": "Non-reliance on prior financials",
    "5.01": "Change in control",
    "5.02": "Director/officer departure or appointment",
    "5.03": "Amendment to bylaws or fiscal year",
    "5.07": "Submission of matters to a shareholder vote",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}


def _text(xml: str, tag: str, default=None):
    """First value of `tag`, stripping any nested <value> wrapper Form 4 uses."""
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", xml, re.S | re.I)
    if not m:
        return default
    inner = m.group(1)
    v = re.search(r"<value>(.*?)</value>", inner, re.S | re.I)
    raw = v.group(1) if v else inner
    return nz.normalize_text(re.sub(r"<[^>]+>", " ", raw)) or default


def _blocks(xml: str, tag: str):
    return re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", xml, re.S | re.I)


def _fetch(url: str) -> str:
    ec.SEC_LIMITER.acquire()
    return ec._http(url, headers=ec._sec_headers())


def _filing_dir(cik: str, accession: str) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{str(int(cik))}/"
            f"{accession.replace('-', '')}")


def fetch_form4_xml(cik: str, accession: str, primary_document: str = None) -> str:
    """
    Retrieve the Form 4 XML.

    `primaryDocument` from the submissions API is usually the rendered HTML, so
    fall back to reading the filing directory index for the real XML.
    """
    base = _filing_dir(cik, accession)

    if primary_document and primary_document.lower().endswith(".xml"):
        # EDGAR reports primaryDocument as "xslF345X06/primarydocument.xml" --
        # the XSL-rendered *HTML* view, despite the .xml suffix. The machine
        # readable document is the same filename without that directory.
        name = primary_document.rsplit("/", 1)[-1]
        return _fetch(f"{base}/{name}")

    listing = _fetch(f"{base}/")
    candidates = re.findall(r'href="([^"]+\.xml)"', listing)
    # Skip the XSL rendering stylesheets; take the document itself.
    candidates = [c for c in candidates if "xsl" not in c.lower()] or candidates
    if not candidates:
        raise ValueError(f"No XML document found in {base}")

    href = candidates[0]
    return _fetch(href if href.startswith("http")
                  else "https://www.sec.gov" + href)


def parse_form4(xml: str) -> dict:
    """
    Parse a Form 4 into the facts an analyst actually asks for.

    The 10b5-1 flag is `aff10b5One`, a checkbox added to the form in 2023. When
    absent (older filings, or a filer who left it blank) it is reported as
    unknown rather than False -- "not disclosed" and "not under a plan" are
    different answers.
    """
    owner = _text(xml, "rptOwnerName", "")
    is_director = (_text(xml, "isDirector", "") or "").lower() in ("1", "true")
    is_officer = (_text(xml, "isOfficer", "") or "").lower() in ("1", "true")
    is_ten_pct = (_text(xml, "isTenPercentOwner", "") or "").lower() in ("1", "true")
    title = _text(xml, "officerTitle", "")

    roles = []
    if is_officer:
        roles.append(title or "Officer")
    if is_director:
        roles.append("Director")
    if is_ten_pct:
        roles.append("10% owner")

    raw_flag = _text(xml, "aff10b5One")
    if raw_flag is None:
        plan_10b5_1 = None            # not disclosed on this form
    else:
        plan_10b5_1 = raw_flag.strip().lower() in ("1", "true")

    footnotes = {}
    for fid, body in re.findall(r'<footnote\s+id="([^"]+)"[^>]*>(.*?)</footnote>', xml, re.S | re.I):
        footnotes[fid] = nz.normalize_text(re.sub(r"<[^>]+>", " ", body))

    transactions = []
    for kind, derivative in (("nonDerivativeTransaction", False),
                             ("derivativeTransaction", True)):
        for block in _blocks(xml, kind):
            code = (_text(block, "transactionCode", "") or "").upper()
            label, direction, open_market = TRANSACTION_CODES.get(
                code, (f"Code {code}", "other", False))

            shares = nz.parse_number(_text(block, "transactionShares"))
            price = nz.parse_number(_text(block, "transactionPricePerShare"))
            acq_disp = (_text(block, "transactionAcquiredDisposedCode", "") or "").upper()

            refs = re.findall(r'footnoteId\s+id="([^"]+)"', block, re.I)
            transactions.append({
                "date": _text(block, "transactionDate", ""),
                "security": _text(block, "securityTitle", ""),
                "code": code,
                "code_label": label,
                "direction": "dispose" if acq_disp == "D" else ("acquire" if acq_disp == "A" else direction),
                "is_open_market_decision": open_market,
                "shares": shares,
                "price": price,
                "value": (shares * price) if (shares and price) else None,
                "shares_after": nz.parse_number(_text(block, "sharesOwnedFollowingTransaction")),
                "derivative": derivative,
                "footnotes": [footnotes.get(r, "") for r in refs if footnotes.get(r)],
            })

    return {
        "owner": owner,
        "roles": roles,
        "officer_title": title,
        "is_director": is_director,
        "is_officer": is_officer,
        "is_ten_percent_owner": is_ten_pct,
        "issuer": _text(xml, "issuerName", ""),
        "issuer_ticker": _text(xml, "issuerTradingSymbol", ""),
        "period": _text(xml, "periodOfReport", ""),
        "plan_10b5_1": plan_10b5_1,
        "transactions": transactions,
        "footnotes": list(footnotes.values()),
    }


def insider_transactions(symbol: str, limit: int = 10, person: str = None,
                         since: str = None) -> dict:
    """
    Parsed Form 4 activity for a company, newest first.

    Args:
        limit: how many Form 4 filings to parse (each may hold several transactions).
        person: case-insensitive substring match on the reporting owner's name.
        since: ISO date; drop transactions before it.
    """
    filings = ec.company_filings(symbol, forms=["4"], limit=max(limit * 3, limit))
    info = ec.ticker_to_cik(symbol)

    wanted_person = nz.to_ascii(person).lower() if person else None
    parsed, errors = [], []

    for f in filings:
        if len(parsed) >= limit:
            break
        try:
            xml = fetch_form4_xml(info["cik"], f["accession"], f.get("primary_document"))
            report = parse_form4(xml)
        except Exception as e:
            errors.append(f"{f.get('accession', '?')}: {str(e)[:70]}")
            continue

        if wanted_person and wanted_person not in nz.to_ascii(report["owner"]).lower():
            continue

        if since:
            report["transactions"] = [t for t in report["transactions"]
                                      if (t.get("date") or "") >= since]
            if not report["transactions"]:
                continue

        report["accession"] = f.get("accession")
        report["filed"] = f.get("filing_date")
        report["accepted"] = f.get("acceptance")
        report["url"] = f.get("url")
        parsed.append(report)

    return {"symbol": symbol.upper(), "company": info["title"],
            "filings": parsed, "errors": errors}


def summarise_insider_flow(reports: list) -> dict:
    """
    Net open-market activity across parsed Form 4s.

    Grants, option exercises and tax withholding are counted separately: they
    are compensation mechanics, not a view on the stock, and folding them into
    "insiders sold $X" is how that number gets misread.
    """
    bought = sold = 0.0
    bought_sh = sold_sh = 0.0
    planned = unplanned = 0
    other_value = 0.0

    for r in reports:
        for t in r["transactions"]:
            value = t.get("value") or 0.0
            if not t["is_open_market_decision"]:
                other_value += value
                continue
            if t["direction"] == "acquire":
                bought += value
                bought_sh += t.get("shares") or 0.0
            elif t["direction"] == "dispose":
                sold += value
                sold_sh += t.get("shares") or 0.0
                if r.get("plan_10b5_1") is True:
                    planned += 1
                elif r.get("plan_10b5_1") is False:
                    unplanned += 1

    return {
        "open_market_bought_value": bought,
        "open_market_sold_value": sold,
        "open_market_bought_shares": bought_sh,
        "open_market_sold_shares": sold_sh,
        "net_value": bought - sold,
        "non_discretionary_value": other_value,
        "sales_under_10b5_1": planned,
        "sales_not_under_10b5_1": unplanned,
    }


def describe_8k_items(items: str) -> list:
    """Turn an 8-K's comma-separated item codes into their meanings."""
    out = []
    for raw in (items or "").split(","):
        code = raw.strip()
        if not code:
            continue
        out.append(f"{code} — {EIGHT_K_ITEMS.get(code, 'Other')}")
    return out
