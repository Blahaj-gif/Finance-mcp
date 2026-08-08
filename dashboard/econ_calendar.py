"""
Economic calendar and real-time filings, from free public APIs.

Two sources, deliberately kept separate from the broker feed:

  * **BLS** (bls.gov) for macro releases -- CPI and core CPI, PPI, the
    employment situation, JOLTS. The v1 API needs no registration but is capped
    at 25 queries a day per IP, so results are cached hard. Setting
    ``BLS_API_KEY`` in .env upgrades to v2 (500/day, longer history, and
    server-side calculations).

  * **SEC EDGAR** for filings. No key exists, but the SEC's fair-access policy
    requires a descriptive ``User-Agent`` carrying a real contact address and
    caps traffic at 10 requests/second. Set ``SEC_USER_AGENT`` in .env --
    requests are refused without it rather than sent anonymously, because
    being rate-banned by the SEC is a worse outcome than a clear error.

Everything here is read-only and public. Nothing touches the broker.
"""
import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

try:
    from dashboard import normalization as nz
except ImportError:  # imported as a top-level module from dashboard/
    import normalization as nz

BLS_API_KEY = os.getenv("BLS_API_KEY", "").strip()
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip()

BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_SCHEDULE = "https://www.bls.gov/schedule/news_release/{slug}.htm"


# =====================================================================
# RATE / POLLING LAYER
# =====================================================================
# Separate budgets per host: the SEC publishes a hard 10 req/s ceiling, while
# BLS v1 allows only 25 queries per *day*, so the constraint there is call
# count rather than spacing. Both are enforced here so no caller can bypass it.

class _HostLimiter:
    def __init__(self, min_interval: float, daily_cap: int = None):
        self.min_interval = min_interval
        self.daily_cap = daily_cap
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._day = None
        self._count = 0

    def acquire(self):
        with self._lock:
            today = datetime.date.today()
            if self._day != today:
                self._day, self._count = today, 0

            if self.daily_cap is not None and self._count >= self.daily_cap:
                raise RuntimeError(
                    f"Daily request cap of {self.daily_cap} reached for this source. "
                    "Set BLS_API_KEY in .env to raise it to 500/day."
                )

            wait = self._next_allowed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_allowed = time.monotonic() + self.min_interval
            self._count += 1

    def remaining_today(self):
        if self.daily_cap is None:
            return None
        with self._lock:
            if self._day != datetime.date.today():
                return self.daily_cap
            return max(0, self.daily_cap - self._count)


# SEC: 10 req/s ceiling -> 0.12 s spacing leaves headroom.
SEC_LIMITER = _HostLimiter(min_interval=0.12)
# BLS *API*: 25/day unregistered, 500/day with a key. This quota is precious.
BLS_LIMITER = _HostLimiter(min_interval=0.5, daily_cap=500 if BLS_API_KEY else 25)
# BLS *website* (the release-schedule pages) is an ordinary web fetch and does
# not touch the API quota. Charging schedule scrapes against it burned 8 of the
# 25 daily queries on a single calendar call.
BLS_WEB_LIMITER = _HostLimiter(min_interval=0.15)


class _TTLCache:
    """Small TTL cache. Macro series move monthly; filings move constantly."""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key, ttl):
        with self._lock:
            hit = self._data.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        return None

    def put(self, key, value):
        with self._lock:
            self._data[key] = (time.time(), value)

    def clear(self):
        with self._lock:
            self._data.clear()


CACHE = _TTLCache()
TTL_MACRO = 6 * 3600        # BLS revises monthly; 6h is generous and protects the quota
TTL_SCHEDULE = 24 * 3600    # release calendars change rarely
TTL_FILINGS = 120           # "almost real time" -- 2 minutes
TTL_CIK_MAP = 7 * 24 * 3600


def _http(url, data=None, headers=None, timeout=30):
    """
    GET/POST returning decoded text.

    Handles gzip and deflate itself: urllib does not decompress transparently,
    and the SEC's fair-access policy asks clients to accept compression.
    """
    import gzip
    import zlib

    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()

    if "gzip" in encoding:
        raw = gzip.decompress(raw)
    elif "deflate" in encoding:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)

    return raw.decode("utf-8", errors="replace")


def _sec_headers():
    if not SEC_USER_AGENT:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. The SEC requires a descriptive User-Agent "
            "with a real contact address, e.g.\n"
            '    SEC_USER_AGENT=Your Name (you@example.com)\n'
            "Add it to .env. Requests are not sent without one."
        )
    return {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}


# =====================================================================
# BLS — MACRO SERIES
# =====================================================================

BLS_SERIES = {
    "cpi":            ("CUUR0000SA0",    "CPI-U, All Items (NSA)",            "index"),
    "cpi_sa":         ("CUSR0000SA0",    "CPI-U, All Items (SA)",             "index"),
    "core_cpi":       ("CUUR0000SA0L1E", "Core CPI (ex food & energy)",       "index"),
    "unemployment":   ("LNS14000000",    "Unemployment Rate",                 "percent"),
    "payrolls":       ("CES0000000001",  "Total Nonfarm Payrolls",            "thousands"),
    "ppi":            ("WPUFD4",         "PPI, Final Demand",                 "index"),
    "avg_hourly_pay": ("CES0500000003",  "Average Hourly Earnings, Private",  "dollars"),
    "labor_force":    ("LNS11300000",    "Labor Force Participation Rate",    "percent"),
}

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def fetch_bls_series(keys, start_year=None, end_year=None):
    """
    Fetch one or more macro series and compute month-over-month and
    year-over-year changes.

    Returns {key: {"label", "unit", "series_id", "observations": [...]}} with
    observations newest-first.
    """
    keys = [k for k in keys if k in BLS_SERIES]
    if not keys:
        raise ValueError(f"No known series requested. Available: {', '.join(BLS_SERIES)}")

    today = datetime.date.today()
    end_year = end_year or today.year
    start_year = start_year or (end_year - 2)

    cache_key = ("bls", tuple(sorted(keys)), start_year, end_year, bool(BLS_API_KEY))
    cached = CACHE.get(cache_key, TTL_MACRO)
    if cached is not None:
        return cached

    payload = {
        "seriesid": [BLS_SERIES[k][0] for k in keys],
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    if BLS_API_KEY:
        # v2 only: ask BLS to compute its own percent changes. We still compute
        # ours, and compare -- an independent second opinion on the arithmetic
        # costs nothing here and catches a period-alignment mistake.
        payload["registrationkey"] = BLS_API_KEY
        payload["calculations"] = True

    BLS_LIMITER.acquire()
    raw = _http(BLS_V2 if BLS_API_KEY else BLS_V1,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
    body = json.loads(raw)

    if body.get("status") != "REQUEST_SUCCEEDED":
        msgs = "; ".join(body.get("message", [])) or body.get("status", "unknown error")
        raise RuntimeError(f"BLS API rejected the request: {msgs}")

    by_id = {s["seriesID"]: s for s in body.get("Results", {}).get("series", [])}
    out = {}
    for key in keys:
        sid, label, unit = BLS_SERIES[key]
        rows = by_id.get(sid, {}).get("data", [])

        obs = []
        for row in rows:
            period = row.get("period", "")
            if not period.startswith("M") or period == "M13":   # M13 is an annual average
                continue
            value = nz.parse_number(row.get("value"))
            if value is None:
                continue
            # v2 returns its own pct_changes when `calculations` is requested.
            bls_calc = None
            calcs = (row.get("calculations") or {}).get("pct_changes") or {}
            if calcs:
                bls_calc = {"mom": nz.parse_number(calcs.get("1")),
                            "yoy": nz.parse_number(calcs.get("12"))}

            obs.append({
                "year": int(row["year"]),
                "month": int(period[1:]),
                "period": nz.normalize_text(row.get("periodName", "")),
                "value": value,
                "bls_calc": bls_calc,
                "footnotes": [nz.normalize_text(f.get("text", ""))
                              for f in row.get("footnotes", []) if f.get("text")],
            })

        obs.sort(key=lambda o: (o["year"], o["month"]), reverse=True)
        index = {(o["year"], o["month"]): o["value"] for o in obs}
        # A series that is already a rate changes in percentage *points*. Saying
        # unemployment fell "2.38%" when it went 4.2 -> 4.1 is the percent change
        # of a percentage, which reads as five times the move that occurred.
        is_rate = unit == "percent"
        for o in obs:
            y, m = o["year"], o["month"]
            prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
            prev, year_ago = index.get(prev_m), index.get((y - 1, m))

            if is_rate:
                o["mom_pct"] = (o["value"] - prev) if prev is not None else None
                o["yoy_pct"] = (o["value"] - year_ago) if year_ago is not None else None
                o["change_unit"] = "pp"
            else:
                o["mom_pct"] = ((o["value"] / prev - 1) * 100) if prev else None
                o["yoy_pct"] = ((o["value"] / year_ago - 1) * 100) if year_ago else None
                o["change_unit"] = "%"

                # Where BLS supplied its own figure (v2), check ours against it.
                calc = o.get("bls_calc") or {}
                for ours, theirs in (("mom_pct", "mom"), ("yoy_pct", "yoy")):
                    a, b = o.get(ours), calc.get(theirs)
                    if a is not None and b is not None and abs(a - b) > 0.15:
                        o.setdefault("warnings", []).append(
                            f"{ours} {a:+.2f}% disagrees with the BLS-computed {b:+.2f}%")

        out[key] = {"series_id": sid, "label": label, "unit": unit, "observations": obs}

    CACHE.put(cache_key, out)
    return out


# =====================================================================
# BLS — RELEASE CALENDAR
# =====================================================================

BLS_RELEASES = {
    "cpi":    "Consumer Price Index",
    "ppi":    "Producer Price Index",
    "empsit": "Employment Situation",
    "jolts":  "Job Openings & Labor Turnover",
    "eci":    "Employment Cost Index",
    "realer": "Real Earnings",
    "ximpim": "Import & Export Price Indexes",
    "prod2":  "Productivity & Costs",
}

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TAG = re.compile(r"<[^>]+>")


def _parse_release_date(text):
    """BLS writes dates as 'Aug. 12, 2026' / 'Sept. 10, 2026' / 'May 12, 2026'."""
    cleaned = nz.normalize_text(text).replace(".", "")
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", cleaned)
    if not m:
        return None
    name, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
    for full, num in _MONTHS.items():
        if full.lower().startswith(name[:3]):
            try:
                return datetime.date(year, num, day)
            except ValueError:
                return None
    return None


def fetch_release_schedule(slug):
    """Scheduled release dates for one BLS product, oldest-first."""
    if slug not in BLS_RELEASES:
        raise ValueError(f"Unknown release '{slug}'. Available: {', '.join(BLS_RELEASES)}")

    cache_key = ("bls-sched", slug)
    cached = CACHE.get(cache_key, TTL_SCHEDULE)
    if cached is not None:
        return cached

    BLS_WEB_LIMITER.acquire()
    html_text = _http(BLS_SCHEDULE.format(slug=slug),
                      headers={"User-Agent": SEC_USER_AGENT or "Replicant Quant MCP"})

    entries = []
    for row in _ROW.findall(html_text):
        cells = [nz.normalize_text(_TAG.sub("", c)) for c in _CELL.findall(row)]
        cells = [c for c in cells if c]
        if len(cells) < 3:
            continue
        when = _parse_release_date(cells[1])
        if not when:
            continue
        entries.append({
            "release": BLS_RELEASES[slug],
            "slug": slug,
            "reference_period": cells[0],
            "date": when,
            "time_et": cells[2],
        })

    entries.sort(key=lambda e: e["date"])
    CACHE.put(cache_key, entries)
    return entries


def upcoming_releases(days_ahead=30, days_back=7, slugs=None):
    """Every scheduled BLS release inside the window, chronologically."""
    today = datetime.date.today()
    lo, hi = today - datetime.timedelta(days=days_back), today + datetime.timedelta(days=days_ahead)

    found, failed = [], []
    for slug in (slugs or list(BLS_RELEASES)):
        try:
            found.extend(e for e in fetch_release_schedule(slug) if lo <= e["date"] <= hi)
        except Exception as e:
            failed.append(f"{slug}: {str(e)[:80]}")

    found.sort(key=lambda e: (e["date"], e["release"]))
    return found, failed


# =====================================================================
# SEC EDGAR
# =====================================================================

def ticker_to_cik(symbol):
    """Resolve a ticker to its zero-padded 10-digit CIK."""
    cached = CACHE.get("cik-map", TTL_CIK_MAP)
    if cached is None:
        SEC_LIMITER.acquire()
        raw = _http("https://www.sec.gov/files/company_tickers.json", headers=_sec_headers())
        data = json.loads(raw)
        cached = {}
        for row in data.values():
            t = nz.normalize_text(row.get("ticker", "")).upper()
            if t:
                cached[t] = {"cik": str(row["cik_str"]).zfill(10),
                             "title": nz.normalize_text(row.get("title", ""))}
        CACHE.put("cik-map", cached)

    hit = cached.get(nz.normalize_text(symbol).upper())
    if not hit:
        raise ValueError(f"No SEC registrant found for ticker '{symbol}'")
    return hit


def company_filings(symbol, forms=None, limit=20):
    """
    Recent filings for one company, newest-first.

    `acceptance` is the timestamp the SEC accepted the document, which is what
    makes this near-real-time -- `filing_date` alone is only day-resolution.
    """
    info = ticker_to_cik(symbol)
    cache_key = ("filings", info["cik"])
    payload = CACHE.get(cache_key, TTL_FILINGS)
    if payload is None:
        SEC_LIMITER.acquire()
        payload = json.loads(_http(
            f"https://data.sec.gov/submissions/CIK{info['cik']}.json", headers=_sec_headers()))
        CACHE.put(cache_key, payload)

    recent = payload.get("filings", {}).get("recent", {})
    wanted = {f.upper() for f in forms} if forms else None
    cik_num = str(int(info["cik"]))

    out = []
    for i in range(len(recent.get("accessionNumber", []))):
        form = nz.normalize_text(recent["form"][i]).upper()
        if wanted and form not in wanted:
            continue
        accession = recent["accessionNumber"][i]
        doc = recent.get("primaryDocument", [None] * (i + 1))[i]
        out.append({
            "company": nz.normalize_text(payload.get("name", info["title"])),
            "cik": info["cik"],
            "form": form,
            "filing_date": recent["filingDate"][i],
            "report_date": recent.get("reportDate", [""] * (i + 1))[i],
            "acceptance": recent.get("acceptanceDateTime", [""] * (i + 1))[i],
            "items": nz.normalize_text(recent.get("items", [""] * (i + 1))[i]),
            "description": nz.normalize_text(recent.get("primaryDocDescription", [""] * (i + 1))[i]),
            "url": (f"https://www.sec.gov/Archives/edgar/data/{cik_num}/"
                    f"{accession.replace('-', '')}/{doc}" if doc else
                    f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{accession.replace('-', '')}"),
        })
        if len(out) >= limit:
            break
    return out


_ATOM_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_ATOM_FIELD = {
    "title": re.compile(r"<title>(.*?)</title>", re.S),
    "updated": re.compile(r"<updated>(.*?)</updated>", re.S),
    "link": re.compile(r'<link[^>]*href="([^"]+)"', re.S),
    "summary": re.compile(r"<summary[^>]*>(.*?)</summary>", re.S),
}


def live_filings(form_type="8-K", count=40):
    """
    The EDGAR firehose: filings from every registrant as they are accepted,
    newest-first. Timestamps carry seconds, so this is as close to real time as
    a polled public feed gets -- latency is your poll interval, not the source.
    """
    cache_key = ("live", form_type, count)
    cached = CACHE.get(cache_key, TTL_FILINGS)
    if cached is not None:
        return cached

    SEC_LIMITER.acquire()
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
           f"&type={urllib.parse.quote(form_type)}&company=&dateb=&owner=include"
           f"&count={int(count)}&output=atom")
    xml = _http(url, headers=_sec_headers())

    out = []
    for chunk in _ATOM_ENTRY.findall(xml):
        title = nz.normalize_text(_TAG.sub("", (_ATOM_FIELD["title"].search(chunk) or _Empty()).group(1)))
        updated = (_ATOM_FIELD["updated"].search(chunk) or _Empty()).group(1).strip()
        link = (_ATOM_FIELD["link"].search(chunk) or _Empty()).group(1).strip()

        # Titles read "8-K - COMPANY NAME (0001234567) (Filer)".
        m = re.match(r"([A-Z0-9/\-]+)\s*-\s*(.*?)\s*\((\d{10})\)", title)
        out.append({
            "form": m.group(1) if m else form_type,
            "company": nz.normalize_text(m.group(2)) if m else title,
            "cik": m.group(3) if m else "",
            "accepted": updated,
            "url": link,
            "scripts": nz.detect_scripts(m.group(2) if m else title),
        })

    CACHE.put(cache_key, out)
    return out


class _Empty:
    def group(self, _n):
        return ""


def full_text_search(query, forms=None, date_from=None, date_to=None, limit=20):
    """
    EDGAR full-text search across filing bodies (2001-present).

    Useful for catalysts that never appear in structured fields -- "tariff",
    "going concern", a named counterparty.
    """
    params = {"q": query}
    if forms:
        params["forms"] = ",".join(forms) if isinstance(forms, (list, tuple)) else str(forms)
    if date_from:
        params["dateRange"] = "custom"
        params["startdt"] = str(date_from)
        params["enddt"] = str(date_to or datetime.date.today())

    url = "https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params)
    SEC_LIMITER.acquire()
    body = json.loads(_http(url, headers=_sec_headers()))

    hits = body.get("hits", {}).get("hits", [])
    out = []
    for h in hits[:limit]:
        src = h.get("_source", {})
        names = src.get("display_names") or []
        # Search returns "Company Name (TICK) (CIK 0000123456)" -- keep the name.
        raw_name = nz.normalize_text(names[0] if names else "")
        company = re.sub(r"\s*\((?:CIK\s*)?[A-Z0-9]{1,10}\)\s*$", "", raw_name)
        company = re.sub(r"\s*\(CIK\s*\d{10}\)\s*$", "", company).strip()
        out.append({
            "form": nz.normalize_text(src.get("file_type", "")),
            "company": company or raw_name,
            "filed": src.get("file_date", ""),
            "accession": h.get("_id", "").split(":")[0],
            "url": f"https://www.sec.gov/Archives/edgar/data/"
                   f"{(src.get('ciks') or [''])[0].lstrip('0')}/"
                   f"{h.get('_id','').split(':')[0].replace('-','')}",
        })
    return {"total": body.get("hits", {}).get("total", {}).get("value", 0), "results": out}


# =====================================================================
# EDGAR XBRL — AUTHORITATIVE FUNDAMENTALS
# =====================================================================
# Yahoo's fundamentals are convenient but second-hand. These come straight out
# of the filed XBRL, so every figure carries the form and filing date it came
# from and can be checked against the document itself.

# Several tags express the same concept depending on filer and era; try in order.
XBRL_CONCEPTS = {
    "shares_outstanding": [("dei", "EntityCommonStockSharesOutstanding")],
    "revenue": [("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("us-gaap", "Revenues"),
                ("us-gaap", "SalesRevenueNet")],
    "net_income": [("us-gaap", "NetIncomeLoss")],
    "eps_diluted": [("us-gaap", "EarningsPerShareDiluted")],
    "assets": [("us-gaap", "Assets")],
    "liabilities": [("us-gaap", "Liabilities")],
    "cash": [("us-gaap", "CashAndCashEquivalentsAtCarryingValue")],
    "stockholders_equity": [("us-gaap", "StockholdersEquity")],
}


def xbrl_concept(cik: str, taxonomy: str, tag: str):
    """One XBRL concept's full history, or None if the filer never tagged it."""
    cache_key = ("xbrl", cik, taxonomy, tag)
    cached = CACHE.get(cache_key, TTL_MACRO)
    if cached is not None:
        return cached
    try:
        SEC_LIMITER.acquire()
        data = json.loads(_http(
            f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json",
            headers=_sec_headers()))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            CACHE.put(cache_key, False)
            return None
        raise
    CACHE.put(cache_key, data)
    return data


def company_financials(symbol: str) -> dict:
    """
    Latest reported value for each headline concept, with provenance.

    Each entry carries the value, the period it covers, and the form and filing
    date it was reported in -- so a number can always be traced to a document.
    """
    info = ticker_to_cik(symbol)
    out = {"company": info["title"], "cik": info["cik"], "symbol": symbol.upper(), "facts": {}}

    for name, candidates in XBRL_CONCEPTS.items():
        for taxonomy, tag in candidates:
            data = xbrl_concept(info["cik"], taxonomy, tag)
            if not data:
                continue

            best = None
            for unit, rows in (data.get("units") or {}).items():
                for row in rows:
                    if row.get("val") is None:
                        continue
                    # Prefer the most recently *filed* figure, not the newest period.
                    key = (row.get("filed", ""), row.get("end", ""))
                    if best is None or key > best[0]:
                        best = (key, {
                            "value": float(row["val"]),
                            "unit": unit,
                            "start": row.get("start"),
                            "end": row.get("end"),
                            "form": row.get("form"),
                            "filed": row.get("filed"),
                            "fiscal_period": row.get("fp"),
                            "concept": f"{taxonomy}:{tag}",
                        })
            if best:
                out["facts"][name] = best[1]
                break

    return out


def cross_check_fundamentals(symbol: str, yahoo_values: dict, tolerance_pct: float = 1.0) -> list:
    """
    Compare externally sourced figures against the filed XBRL.

    Returns a list of {field, external, filed, divergence_pct, form, filed_date,
    agrees}. An empty list means nothing comparable was found, not that
    everything agreed -- callers should say which.
    """
    filings = company_financials(symbol)
    findings = []

    for field, external in yahoo_values.items():
        fact = filings["facts"].get(field)
        if fact is None or external in (None, 0):
            continue
        filed_value = fact["value"]
        if filed_value == 0:
            continue
        divergence = abs(float(external) - filed_value) / abs(filed_value) * 100
        findings.append({
            "field": field,
            "external": float(external),
            "filed": filed_value,
            "divergence_pct": divergence,
            "agrees": divergence <= tolerance_pct,
            "form": fact.get("form"),
            "filed_date": fact.get("filed"),
            "period_end": fact.get("end"),
            "concept": fact.get("concept"),
        })
    return findings


BLS_REGISTRATION_URL = "https://data.bls.gov/registrationEngine/"


def validate_bls_key(key: str = None) -> dict:
    """
    Check a BLS registration key against the live v2 endpoint.

    Returns {"valid", "tier", "daily_cap", "detail"}. A key is only worth
    trusting once it has actually been accepted -- a typo'd key does not error,
    it silently degrades you to the v1 limits, which is the kind of failure that
    only shows up as an exhausted quota days later.
    """
    candidate = (key or BLS_API_KEY or "").strip()
    if not candidate:
        return {
            "valid": False,
            "tier": "v1 (unregistered)",
            "daily_cap": 25,
            "detail": f"No key set. Register free at {BLS_REGISTRATION_URL} then set "
                      "BLS_API_KEY in .env.",
        }

    payload = {
        "seriesid": ["CUUR0000SA0"],
        "startyear": str(datetime.date.today().year),
        "endyear": str(datetime.date.today().year),
        "registrationkey": candidate,
    }

    try:
        BLS_LIMITER.acquire()
        body = json.loads(_http(BLS_V2, data=json.dumps(payload).encode("utf-8"),
                                headers={"Content-Type": "application/json"}))
    except Exception as e:
        return {"valid": False, "tier": "unknown", "daily_cap": None,
                "detail": f"Could not reach the BLS v2 endpoint: {e}"}

    status = body.get("status", "")
    messages = [nz.normalize_text(m) for m in body.get("message", []) if m]

    if status == "REQUEST_SUCCEEDED":
        return {"valid": True, "tier": "v2 (registered)", "daily_cap": 500,
                "detail": "Key accepted. 500 queries/day, 20 years of history, "
                          "and server-side calculations available."
                          + (f" Notes: {'; '.join(messages)}" if messages else "")}

    return {"valid": False, "tier": "v1 (unregistered)", "daily_cap": 25,
            "detail": f"BLS rejected the key ({status}): "
                      + ("; ".join(messages) or "no detail given")
                      + f". Re-register at {BLS_REGISTRATION_URL} if this persists."}


def source_status():
    """What each source is configured for and how much quota is left."""
    return {
        "bls": {
            "tier": "v2 (registered)" if BLS_API_KEY else "v1 (unregistered)",
            "daily_cap": BLS_LIMITER.daily_cap,
            "remaining_today": BLS_LIMITER.remaining_today(),
            "note": "" if BLS_API_KEY else "Set BLS_API_KEY in .env for 500 queries/day.",
        },
        "sec": {
            "user_agent_configured": bool(SEC_USER_AGENT),
            "rate_limit": "10 req/s (enforced at 0.12s spacing)",
            "note": "" if SEC_USER_AGENT else
                    "Set SEC_USER_AGENT='Your Name (you@example.com)' in .env — required by SEC policy.",
        },
    }


import urllib.parse  # noqa: E402  (used by live_filings / full_text_search)
