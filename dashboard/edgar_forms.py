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


# =====================================================================
# PROSE FILINGS — SECTION EXTRACTION
# =====================================================================
# 10-K and 10-Q bodies are HTML, not data. A Micron 10-K is 2.4 MB raw and
# still ~97,000 tokens once tags are stripped -- half a context window for one
# document. So sections are located and returned under a character budget.
#
# Boundary detection is genuinely heuristic and is reported as such: "Item 1A"
# occurs eight times in a typical 10-K (table of contents, the section itself,
# and cross-references), and filers punctuate the headings inconsistently.

FILING_SECTIONS = {
    "1":   "Business",
    "1A":  "Risk Factors",
    "1B":  "Unresolved Staff Comments",
    "2":   "Properties",
    "3":   "Legal Proceedings",
    "5":   "Market for Registrant's Common Equity",
    "7":   "Management's Discussion and Analysis",
    "7A":  "Quantitative and Qualitative Disclosures About Market Risk",
    "8":   "Financial Statements and Supplementary Data",
    "9A":  "Controls and Procedures",
}

_ITEM_ORDER = ["1", "1A", "1B", "2", "3", "4", "5", "6", "7", "7A", "8", "9", "9A", "9B", "10", "11", "12", "13", "14", "15"]


def html_to_text(raw: str) -> str:
    """Flatten filing HTML to readable text, dropping scripts, styles and markup."""
    import html as _html
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<(br|/p|/div|/tr|/h\d)[^>]*>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = _html.unescape(txt)
    txt = txt.replace(" ", " ")
    txt = re.sub(r"[ \t]+", " ", txt)
    return re.sub(r"\n\s*\n+", "\n\n", txt).strip()


def _item_positions(text: str, code: str, headings_only: bool = False):
    """
    Offsets where 'Item <code>' appears, punctuation-agnostic.

    With `headings_only`, keep just the occurrences that look like a real
    heading: at the start of a line and set in capitals. Filings cross-reference
    each other constantly ("see Item 1A. Risk Factors for a discussion..."), and
    those references sit mid-sentence in mixed case.
    """
    pattern = rf"item\s*{re.escape(code)}\s*[\.\)\:\-–—]?\s"
    out = []
    for m in re.finditer(pattern, text, re.I):
        if headings_only:
            line_start = text.rfind("\n", 0, m.start())
            if m.start() - line_start > 3:          # not at the start of a line
                continue
            if not m.group(0).strip().startswith(("ITEM", "Item")):
                continue
            if m.group(0)[:4] != "ITEM":            # real headings are capitalised
                continue
        out.append(m.start())
    return out


def extract_section(text: str, code: str, budget: int = 8000):
    """
    Pull one Item out of a flattened filing.

    Chooses between candidate headings by span: table-of-contents entries sit
    adjacent to one another, while the real section runs for thousands of
    characters before the next Item. The longest span wins.

    Returns (section_text, meta) where meta records how confident that choice
    is and whether the text was truncated.
    """
    code = code.upper().strip()

    # Prefer true headings; fall back to any mention if the filer does not
    # capitalise them, and say which was used.
    starts = _item_positions(text, code, headings_only=True)
    strict = bool(starts)
    if not starts:
        starts = _item_positions(text, code)
    if not starts:
        return None, {"found": False, "reason": f"No 'Item {code}' heading found"}

    try:
        following = _ITEM_ORDER[_ITEM_ORDER.index(code) + 1:]
    except ValueError:
        following = []

    best, best_span = None, -1
    for s in starts:
        # The guard only needs to clear the heading line itself. Setting it too
        # wide (200 chars) discarded a genuinely adjacent next Item, so a short
        # section -- a one-line legal cross-reference, say -- ran on to the end
        # of the document.
        ends = [p for nxt in following
                for p in _item_positions(text, nxt, headings_only=strict) if p > s + 40]
        end = min(ends) if ends else min(s + budget * 4, len(text))
        if end - s > best_span:
            best, best_span = (s, end), end - s

    if best is None:
        return None, {"found": False, "reason": "Could not bound the section"}

    start, end = best
    body = text[start:end].strip()
    truncated = len(body) > budget
    return (body[:budget], {
        "found": True,
        "candidates": len(starts),
        "full_length": len(body),
        "truncated": truncated,
        "matched_heading": nz.normalize_text(body[:70]),
        "boundary_basis": "capitalised headings" if strict else "any mention (filer did not capitalise headings)",
        "confidence": "high" if (strict and best_span > 3000) else "low",
    })


def search_filing(text: str, query: str, window: int = 500, max_hits: int = 6):
    """Keyword windows out of a filing, for questions a section boundary will not answer."""
    hits = []
    for m in re.finditer(re.escape(query), text, re.I):
        a = max(0, m.start() - window // 2)
        hits.append({"offset": m.start(),
                     "excerpt": nz.normalize_text(text[a:a + window])})
        if len(hits) >= max_hits:
            break
    return hits


def fetch_filing_text(cik: str, accession: str, primary_document: str) -> str:
    """Download a filing's primary document and flatten it to text."""
    base = _filing_dir(cik, accession)
    name = (primary_document or "").rsplit("/", 1)[-1]
    if not name:
        listing = _fetch(f"{base}/")
        docs = re.findall(r'href="([^"]+\.(?:htm|html|txt))"', listing, re.I)
        if not docs:
            raise ValueError(f"No readable document in {base}")
        name = docs[0].rsplit("/", 1)[-1]
    return html_to_text(_fetch(f"{base}/{name}"))


# =====================================================================
# 13F — INSTITUTIONAL HOLDINGS
# =====================================================================

def parse_13f(xml: str) -> list:
    """Holdings from a 13F information table. Fully structured; no guessing needed."""
    rows = []
    for block in _blocks(xml, "infoTable"):
        rows.append({
            "issuer": _text(block, "nameOfIssuer", ""),
            "class": _text(block, "titleOfClass", ""),
            "cusip": _text(block, "cusip", ""),
            "value": nz.parse_number(_text(block, "value")),
            "shares": nz.parse_number(_text(block, "sshPrnamt")),
            "share_type": _text(block, "sshPrnamtType", ""),
            "put_call": _text(block, "putCall", ""),
            "discretion": _text(block, "investmentDiscretion", ""),
        })
    return rows


def institutional_holdings(symbol_or_cik: str, limit: int = 25) -> dict:
    """
    Latest 13F-HR holdings for an institution, largest position first.

    `value` is reported in whole dollars on modern filings; older ones used
    thousands, so the total is sanity-checked rather than assumed.
    """
    ident = str(symbol_or_cik).strip()
    if ident.isdigit():
        cik, name = ident.zfill(10), f"CIK {ident}"
    else:
        info = ec.ticker_to_cik(ident)
        cik, name = info["cik"], info["title"]

    filings = ec.company_filings(ident if not ident.isdigit() else ident,
                                 forms=["13F-HR"], limit=1) if not ident.isdigit() else []
    if not filings:
        ec.SEC_LIMITER.acquire()
        payload = json.loads(ec._http(
            f"https://data.sec.gov/submissions/CIK{cik}.json", headers=ec._sec_headers()))
        name = nz.normalize_text(payload.get("name", name))
        recent = payload.get("filings", {}).get("recent", {})
        filings = []
        for i in range(len(recent.get("form", []))):
            if recent["form"][i].upper() == "13F-HR":
                filings = [{"accession": recent["accessionNumber"][i],
                            "filing_date": recent["filingDate"][i],
                            "report_date": recent.get("reportDate", [""] * (i + 1))[i]}]
                break

    if not filings:
        raise ValueError(f"No 13F-HR filing found for {symbol_or_cik}")

    f = filings[0]
    base = _filing_dir(cik, f["accession"])
    listing = _fetch(f"{base}/")
    xmls = [x for x in re.findall(r'href="([^"]+\.xml)"', listing)
            if "primary_doc" not in x.lower() and "xsl" not in x.lower()]
    if not xmls:
        raise ValueError(f"No information table in {base}")

    href = xmls[0]
    holdings = parse_13f(_fetch(href if href.startswith("http") else "https://www.sec.gov" + href))

    # A 13F may report one issuer across several rows -- different managers or
    # investment-discretion categories. Summing them gives the fund's actual
    # position; leaving them split understates every holding it affects.
    merged = {}
    for h in holdings:
        key = (h["cusip"], h.get("put_call") or "")
        if key in merged:
            merged[key]["value"] = (merged[key]["value"] or 0) + (h["value"] or 0)
            merged[key]["shares"] = (merged[key]["shares"] or 0) + (h["shares"] or 0)
            merged[key]["rows"] += 1
        else:
            h = dict(h); h["rows"] = 1
            merged[key] = h
    holdings = sorted(merged.values(), key=lambda h: h.get("value") or 0, reverse=True)
    total = sum(h.get("value") or 0 for h in holdings)

    return {"institution": name, "cik": cik, "filed": f.get("filing_date"),
            "period": f.get("report_date"), "positions": len(holdings),
            "total_value": total, "holdings": holdings[:limit]}


def describe_8k_items(items: str) -> list:
    """Turn an 8-K's comma-separated item codes into their meanings."""
    out = []
    for raw in (items or "").split(","):
        code = raw.strip()
        if not code:
            continue
        out.append(f"{code} — {EIGHT_K_ITEMS.get(code, 'Other')}")
    return out
