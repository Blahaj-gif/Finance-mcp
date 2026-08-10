"""
Economic calendar and real-time filings, from free public APIs.

Four sources, deliberately kept separate from the broker feed:

  * **BLS** (bls.gov) for macro releases -- CPI and core CPI, PPI, the
    employment situation, JOLTS. The v1 API needs no registration but is capped
    at 25 queries a day per IP, so results are cached hard. Setting
    ``BLS_API_KEY`` in .env upgrades to v2 (500/day, longer history, and
    server-side calculations). Note that the *schedule* pages live on
    www.bls.gov and the *data* on api.bls.gov; they fail independently.

  * **Federal Reserve** for FOMC decision dates, scraped from the Fed's own
    calendar page. Not a BLS release, so a BLS-only calendar simply does not
    contain the most market-moving date there is.

  * **BEA** for PCE, GDP and the trade balance. PCE is the Fed's stated target
    measure and is a BEA release -- another thing a BLS-only calendar omits.

  * **SEC EDGAR** for filings. No key exists, but the SEC's fair-access policy
    requires a descriptive ``User-Agent`` carrying a real contact address and
    caps traffic at 10 requests/second. Set ``SEC_USER_AGENT`` in .env --
    requests are refused without it rather than sent anonymously, because
    being rate-banned by the SEC is a worse outcome than a clear error.

What is *not* here, and cannot be from a free source: **consensus forecasts**.
Street estimates are a licensed product. Every comparison in this module is
therefore against the previous print and is labelled that way -- a "surprise"
against a prior reading is not a surprise, because the market trades the gap to
expectations and we do not have expectations.

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
    from dashboard.envfile import load_env
except ImportError:  # imported as a top-level module from dashboard/
    from envfile import load_env

# Read .env here rather than relying on another module having done it:
# these constants are captured at import time, and were empty whenever this
# module was imported before webull_client.
load_env()

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

    # Public statistical endpoints flap. BLS, FRED and EDGAR all return a
    # transient 5xx often enough that a single attempt makes an otherwise sound
    # tool look unreliable. Retry those; never retry a 4xx, which is our fault
    # and will fail identically.
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
            break
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last_error = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = e
        if attempt < 2:
            time.sleep(0.6 * (2 ** attempt))
    else:
        raise last_error

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
                      headers={"User-Agent": SEC_USER_AGENT or "Finance MCP"})

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
# BLS — JOINING THE SCHEDULE TO THE NUMBERS
# =====================================================================
# The schedule says *when*; fetch_bls_series says *what*. Joining them is what
# turns "CPI on Wednesday" into "CPI Wednesday; June printed +2.7% YoY, the
# third straight deceleration" -- context a reader can actually act on.
#
# The join key is the reference period, not the release date: the CPI released
# on 12 August 2026 *is* the July 2026 reading, and pairing a release row with
# whatever observation happens to be newest would silently label the wrong month.

# Which series each release publishes. Only releases whose numbers we actually
# carry appear here; the rest keep their schedule row and are marked "unmapped"
# rather than quietly showing nothing.
RELEASE_SERIES = {
    "cpi":    ["cpi", "core_cpi"],
    "ppi":    ["ppi"],
    "empsit": ["payrolls", "unemployment", "avg_hourly_pay"],
}

# What the market actually quotes for each series.
#
# Nonfarm payrolls as a percentage ("+0.09%") is arithmetically correct and
# useless -- the print is the change in level, +147,000 jobs. CPI as an index
# level (324.099) is equally useless; the print is the year-over-year rate.
# Getting this wrong produces numbers that are right and unrecognisable.
RELEASE_HEADLINE = {
    "cpi": "yoy", "cpi_sa": "yoy", "core_cpi": "yoy",
    "ppi": "yoy", "avg_hourly_pay": "yoy",
    "unemployment": "level", "labor_force": "level",
    "payrolls": "level_change",
}

# Table-width labels. "CPI-U, All Items (NSA)" is the correct name and does not
# fit beside three other series in one calendar row.
RELEASE_SHORT = {
    "cpi": "CPI", "cpi_sa": "CPI (SA)", "core_cpi": "Core CPI", "ppi": "PPI",
    "payrolls": "Payrolls", "unemployment": "Unemployment",
    "avg_hourly_pay": "Avg hourly earnings", "labor_force": "Participation",
}

_QUARTERS = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4,
             "first": 1, "second": 2, "third": 3, "fourth": 4}


def parse_reference_period(text):
    """
    "July 2026" -> (2026, 7, "month");  "2nd Quarter 2026" -> (2026, 6, "quarter").

    A quarter resolves to its final month, which is what a quarterly series is
    stamped with. Returns None when the period is not a form we can join on --
    an annual or semi-annual release, or a heading the table parser picked up.
    """
    cleaned = nz.normalize_text(text or "").replace(".", "")
    if not cleaned:
        return None

    m = re.search(r"(\d)(?:st|nd|rd|th)?\s*(?:quarter|qtr|q)\s*(\d{4})", cleaned, re.I)
    if m:
        q, year = int(m.group(1)), int(m.group(2))
        return (year, q * 3, "quarter") if 1 <= q <= 4 else None

    m = re.search(r"(1st|2nd|3rd|4th|first|second|third|fourth)\s+quarter\s+(\d{4})", cleaned, re.I)
    if m:
        return (int(m.group(2)), _QUARTERS[m.group(1).lower()] * 3, "quarter")

    m = re.search(r"([A-Za-z]{3,9})\s+(\d{4})", cleaned)
    if m:
        name, year = m.group(1).lower(), int(m.group(2))
        for full, num in _MONTHS.items():
            if full.lower().startswith(name[:3]) and len(name) >= 3:
                return (year, num, "month")
    return None


def headline(key, unit, obs, prev_value):
    """
    Render one observation the way the release is quoted, plus how to read it.

    Returns None when the shape asked for is not computable -- a level change
    with no prior month has nothing to subtract, and a fabricated zero there
    would read as "no jobs added".
    """
    kind = RELEASE_HEADLINE.get(key, "level")
    value = obs["value"]

    if kind == "yoy":
        if obs.get("yoy_pct") is None:
            return None
        return {"kind": "yoy", "number": obs["yoy_pct"],
                "text": f"{obs['yoy_pct']:+.1f}% YoY", "index_level": value}

    if kind == "level_change":
        if prev_value is None:
            return None
        delta = value - prev_value
        if unit == "thousands":
            return {"kind": "level_change", "number": delta,
                    "text": f"{delta:+,.0f}k", "index_level": value}
        return {"kind": "level_change", "number": delta,
                "text": f"{delta:+,.1f}", "index_level": value}

    suffix = "%" if unit == "percent" else ""
    return {"kind": "level", "number": value, "text": f"{value:,.1f}{suffix}",
            "index_level": value}


def release_series_for(entries):
    """Which BLS series the releases in this window actually publish."""
    return sorted({k for e in entries for k in RELEASE_SERIES.get(e.get("slug"), [])})


def attach_release_values(entries, today=None, data=None):
    """
    Add the published numbers to schedule rows, in place. Returns (entries, warnings).

    Each entry gains ``values`` (a list, one per series that release publishes)
    and ``value_status``, one of:

      published  -- the reading for this row's own reference period is out
      awaiting   -- the release date has passed but the API has not got it yet
      scheduled  -- still in the future; ``prior`` carries the last published
                    reading, explicitly stamped with *its* reference period
      unmapped   -- a release whose series we do not carry

    ``prior`` is never a forecast and is never presented as one. We have no
    consensus feed -- street estimates are a licensed product -- so every change
    here is measured against the previous print, and named that way. A "surprise"
    against a prior reading is not a surprise; the market trades the gap to
    expectations, and we do not have expectations.

    Never raises: a calendar that loses its numbers is still a calendar, and one
    exhausted BLS quota should not take the schedule down with it.
    """
    today = today or datetime.date.today()
    warnings = []

    wanted = release_series_for(entries)
    if not wanted:
        for e in entries:
            e.setdefault("values", [])
            e.setdefault("value_status", "unmapped")
        return entries, warnings

    # One API call for every series across every row: the unregistered quota is
    # 25 queries a *day*, so a per-row fetch would exhaust it on one calendar.
    # `data` lets a caller that already needs some of these series hand its own
    # result in -- get_economic_calendar was spending two of the day's queries
    # per call, one here and one for its "latest prints" table, because the two
    # key sets differ and so miss each other's cache entry.
    if data is None:
        try:
            data = fetch_bls_series(wanted, start_year=min(e["date"].year for e in entries) - 1)
        except Exception as exc:
            warnings.append(f"Release values unavailable: {str(exc)[:120]}")
            for e in entries:
                e.setdefault("values", [])
                e.setdefault("value_status", "unavailable")
            return entries, warnings

    missing = [k for k in wanted if k not in data]
    if missing:
        warnings.append("No data supplied for: " + ", ".join(missing))
        wanted = [k for k in wanted if k in data]
        if not wanted:
            for e in entries:
                e.setdefault("values", [])
                e.setdefault("value_status", "unavailable")
            return entries, warnings

    index = {}      # key -> {(year, month): observation}
    newest = {}     # key -> newest observation
    for key, block in data.items():
        obs = block["observations"]          # newest-first
        index[key] = {(o["year"], o["month"]): o for o in obs}
        newest[key] = obs[0] if obs else None

    def prev_value(key, year, month):
        p = (year, month - 1) if month > 1 else (year - 1, 12)
        got = index[key].get(p)
        return got["value"] if got else None

    for entry in entries:
        # Intersect with what we actually have: a caller-supplied `data` may
        # legitimately omit a series, and indexing it blindly would take the
        # whole calendar down over one absent row.
        keys = [k for k in RELEASE_SERIES.get(entry.get("slug"), []) if k in data]
        entry["values"] = []
        if not keys:
            entry["value_status"] = "unmapped"
            continue

        ref = parse_reference_period(entry.get("reference_period"))
        entry["reference"] = {"year": ref[0], "month": ref[1], "kind": ref[2]} if ref else None

        published = False
        for key in keys:
            block = data[key]
            unit = block["unit"]
            row = {"series": key, "label": block["label"], "unit": unit,
                   "short": RELEASE_SHORT.get(key, block["label"]),
                   "series_id": block["series_id"]}

            obs = index[key].get((ref[0], ref[1])) if ref else None
            if obs is not None:
                published = True
                row["status"] = "published"
                row["period"] = f"{obs['period']} {obs['year']}"
                row["value"] = obs["value"]
                row["mom"], row["yoy"] = obs.get("mom_pct"), obs.get("yoy_pct")
                row["change_unit"] = obs.get("change_unit")
                row["headline"] = headline(key, unit, obs, prev_value(key, obs["year"], obs["month"]))
                if obs.get("warnings"):
                    warnings.extend(f"{block['label']}: {w}" for w in obs["warnings"])
            else:
                last = newest.get(key)
                row["status"] = "scheduled" if entry["date"] > today else "awaiting"
                if last is not None:
                    row["prior"] = {
                        "period": f"{last['period']} {last['year']}",
                        "value": last["value"],
                        "mom": last.get("mom_pct"), "yoy": last.get("yoy_pct"),
                        "change_unit": last.get("change_unit"),
                        "headline": headline(key, unit, last,
                                              prev_value(key, last["year"], last["month"])),
                    }
            entry["values"].append(row)

        if published:
            entry["value_status"] = "published"
        elif entry["date"] > today:
            entry["value_status"] = "scheduled"
        else:
            entry["value_status"] = "awaiting"
            warnings.append(
                f"{entry['release']} ({entry.get('reference_period', '?')}) was due "
                f"{entry['date']} but has not appeared in the BLS API yet")

    return entries, warnings


# =====================================================================
# FEDERAL RESERVE — FOMC DECISIONS
# =====================================================================
# The most market-moving date on the calendar, and it is not a BLS release, so
# a BLS-only calendar simply does not contain it.
#
# Both the meeting dates and the *decision* date are read from the Fed's own
# calendar page. What is not on that page is the time: the statement has landed
# at 2:00 PM ET at every meeting since 2013, but that is a convention we are
# asserting, not a figure the page supplies, and it is labelled as such.

FOMC_CALENDAR = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

_FOMC_YEAR = re.compile(r'<h4><a id="\d+">(\d{4})\s+FOMC\s+Meetings</a></h4>', re.I)
_FOMC_ROW = re.compile(
    r'fomc-meeting__month[^>]*>\s*(?:<strong>)?(.*?)(?:</strong>)?\s*</div>'
    r'(.*?)fomc-meeting__date[^>]*>(.*?)</div>', re.S)


def _month_number(name):
    name = nz.normalize_text(name).replace(".", "").lower()
    for full, num in _MONTHS.items():
        if name and full.lower().startswith(name[:3]):
            return num
    return None


def _parse_fomc_row(months_text, days_text, year):
    """
    "April" + "28-29"    -> (Apr 28, Apr 29)
    "Apr/May" + "30-1"   -> (Apr 30, May 1)      -- meetings straddle month ends
    "August" + "22 (notation vote)" -> (Aug 22, Aug 22), unscheduled
    """
    months = [m for m in (_month_number(p) for p in months_text.split("/")) if m]
    if not months:
        return None

    note = ""
    m = re.search(r"\(([^)]*)\)", days_text)
    if m:
        note = nz.normalize_text(m.group(1))
    projections = "*" in days_text

    days = [int(d) for d in re.findall(r"\d{1,2}", re.sub(r"\([^)]*\)", "", days_text))]
    if not days:
        return None

    start_month = months[0]
    end_month = months[-1] if len(months) > 1 else start_month
    start_year = end_year = year
    # A December/January meeting ends in the following year.
    if end_month < start_month:
        end_year += 1

    try:
        start = datetime.date(start_year, start_month, days[0])
        end = datetime.date(end_year, end_month, days[-1])
    except ValueError:
        return None

    return {"start": start, "end": end, "note": note, "projections": projections}


def _meeting_span(start, end):
    """'Mar 17-18', or 'Apr 30 - May 1' when the meeting straddles a month."""
    months = list(_MONTHS)
    a = f"{months[start.month - 1][:3]} {start.day}"
    if start == end:
        return a
    if start.month == end.month:
        return f"{a}-{end.day}"
    return f"{a} - {months[end.month - 1][:3]} {end.day}"


def fetch_fomc_meetings():
    """
    Every FOMC meeting the Fed's calendar lists, oldest-first.

    ``date`` is the *decision* date -- the final day, when the statement is
    released. That is the market-moving moment; the first day is carried
    separately as ``start_date`` rather than being the thing sorted on.
    """
    cached = CACHE.get("fomc-cal", TTL_SCHEDULE)
    if cached is not None:
        return cached

    SEC_LIMITER.acquire()      # ordinary web fetch; reuse the polite spacing
    html_text = _http(FOMC_CALENDAR,
                      headers={"User-Agent": SEC_USER_AGENT or "Finance MCP",
                               "Accept-Encoding": "gzip, deflate"})
    html_text = re.sub(r"<script.*?</script>", "", html_text, flags=re.S)

    # Year panels are not in chronological order on the page (the next year is
    # appended after the archive), so each meeting takes the year of the panel
    # it physically sits inside rather than the order it was found in.
    panels = [(m.start(), int(m.group(1))) for m in _FOMC_YEAR.finditer(html_text)]
    if not panels:
        raise RuntimeError("FOMC calendar page did not contain any year panels; "
                           "the Federal Reserve page layout has changed")

    def year_at(pos):
        year = None
        for start, y in panels:
            if start <= pos:
                year = y
            else:
                break
        return year

    out = []
    for row in _FOMC_ROW.finditer(html_text):
        year = year_at(row.start())
        if year is None:
            continue
        parsed = _parse_fomc_row(row.group(1), row.group(3), year)
        if not parsed:
            continue
        # The asterisk marks a Summary of Economic Projections meeting, but the
        # row's own body names the materials -- read the words, not the glyph.
        projections = parsed["projections"] or "Projection Materials" in row.group(2)
        unscheduled = "notation" in parsed["note"].lower()
        out.append({
            "source": "Federal Reserve",
            "release": "FOMC rate decision",
            "slug": "fomc",
            "date": parsed["end"],
            "start_date": parsed["start"],
            "reference_period": _meeting_span(parsed["start"], parsed["end"]),
            "time_et": "2:00 PM (customary)",
            "projections": projections,
            "unscheduled": unscheduled,
            "note": parsed["note"],
        })

    out.sort(key=lambda e: e["date"])
    CACHE.put("fomc-cal", out)
    return out


# =====================================================================
# BEA — PCE, GDP, TRADE
# =====================================================================
# PCE is the Federal Reserve's stated target measure and it is a BEA release,
# not a BLS one -- so a BLS-only calendar omits the inflation gauge that
# actually drives policy. GDP is here for the same reason.

BEA_SCHEDULE = "https://www.bea.gov/news/schedule"

# BEA publishes a great deal that no one trades: state personal income, direct
# investment by country, activities of multinational enterprises. Matching an
# explicit list keeps the calendar to releases that move a market, and anything
# unmatched is counted and reported rather than silently dropped.
#
# The GDP pattern requires an estimate vintage -- "GDP (Advance Estimate)" --
# because a bare "GDP" prefix also catches "GDP by County and Personal Income
# by County", a regional statistic that is not the national print.
BEA_RELEASES = (
    (re.compile(r"^Personal Income and Outlays"), "Personal Income & Outlays (PCE)", "pce"),
    (re.compile(r"^(?:GDP|Gross Domestic Product)\s*\((?:Advance|Second|Third)\s+Estimate\)",
                re.I), "Gross Domestic Product", "gdp"),
    (re.compile(r"^U\.S\. International Trade in Goods and Services"),
     "International Trade Balance", "trade"),
)

_BEA_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_BEA_DATE = re.compile(r'class="release-date"[^>]*>(.*?)</div>', re.S)
_BEA_TIME = re.compile(r'class="text-muted"[^>]*>(.*?)</small>', re.S)
_BEA_TITLE = re.compile(r'class="release-title[^"]*"[^>]*>(.*?)</td>', re.S)
_BEA_YEAR = re.compile(r"Year\s+(\d{4})")


def fetch_bea_schedule():
    """
    Scheduled BEA releases we track, oldest-first. Returns (entries, skipped).

    ``skipped`` is the count of rows that parsed but matched no tracked release
    -- the page carries far more than three, and a silent filter would make an
    incomplete calendar indistinguishable from a complete one.
    """
    cached = CACHE.get("bea-cal", TTL_SCHEDULE)
    if cached is not None:
        return cached

    SEC_LIMITER.acquire()
    html_text = _http(BEA_SCHEDULE,
                      headers={"User-Agent": SEC_USER_AGENT or "Finance MCP",
                               "Accept-Encoding": "gzip, deflate"})
    html_text = re.sub(r"<script.*?</script>", "", html_text, flags=re.S)

    year_m = _BEA_YEAR.search(html_text)
    if not year_m:
        raise RuntimeError("BEA schedule page carried no year header; layout has changed")
    year = int(year_m.group(1))

    entries, skipped, last_month = [], 0, 0
    for row in _BEA_ROW.finditer(html_text):
        block = row.group(1)
        date_m, title_m = _BEA_DATE.search(block), _BEA_TITLE.search(block)
        if not date_m or not title_m:
            continue

        # The date cells carry no year -- only the table header does. Rows are
        # chronological, so a month that moves backwards means the year rolled.
        raw_date = nz.normalize_text(_TAG.sub("", date_m.group(1)))
        dm = re.match(r"([A-Za-z]+)\s+(\d{1,2})", raw_date)
        if not dm:
            continue
        month = _month_number(dm.group(1))
        if not month:
            continue
        if month < last_month:
            year += 1
        last_month = month
        try:
            when = datetime.date(year, month, int(dm.group(2)))
        except ValueError:
            continue

        title = nz.normalize_text(_TAG.sub(" ", title_m.group(1)))
        match = next((r for r in BEA_RELEASES if r[0].match(title)), None)
        if match is None:
            skipped += 1
            continue

        # "GDP (Third Estimate), Industries, Corporate Profits, State GDP, and
        # State Personal Income, 2nd Quarter 2026" -- the period is the part
        # anyone reads; the rest stays available as full_title.
        period = re.search(r"((?:\d(?:st|nd|rd|th)\s+Quarter|[A-Z][a-z]+)\s+\d{4})", title)
        reference = period.group(1) if period else title
        # Advance, second and third estimates of the same quarter are three
        # separate market events; without the vintage two rows read identically.
        vintage = re.search(r"\((Advance|Second|Third)\s+Estimate\)", title, re.I)
        if vintage:
            reference = f"{reference} ({vintage.group(1).title()})"

        time_m = _BEA_TIME.search(block)
        entries.append({
            "source": "BEA",
            "release": match[1],
            "slug": f"bea_{match[2]}",
            "date": when,
            "reference_period": reference,
            "time_et": nz.normalize_text(_TAG.sub("", time_m.group(1))) if time_m else "",
            "full_title": title,
        })

    entries.sort(key=lambda e: e["date"])
    result = (entries, skipped)
    CACHE.put("bea-cal", result)
    return result


# =====================================================================
# ONE CALENDAR
# =====================================================================

CALENDAR_SOURCES = ("bls", "fomc", "bea")


def economic_calendar(days_ahead=30, days_back=7, sources=None, with_values=True,
                      today=None, values=None):
    """
    Every tracked release inside the window, from every source, chronologically.

    Returns ``(entries, warnings)``. One source failing degrades that source
    only: a Fed page redesign should not take CPI off the calendar with it, and
    the warning names which source went missing so an empty week is never
    mistaken for a quiet week.
    """
    today = today or datetime.date.today()
    lo = today - datetime.timedelta(days=days_back)
    hi = today + datetime.timedelta(days=days_ahead)
    wanted = [s for s in (sources or CALENDAR_SOURCES) if s in CALENDAR_SOURCES]

    entries, warnings = [], []

    # Three different hosts, so the per-host limiters do not contend with each
    # other and the waiting overlaps instead of stacking. Sequentially this was
    # bls.gov (~1.1s) + federalreserve.gov (~0.2s) + bea.gov (~1.6s); the
    # limiters stay authoritative, concurrency only removes the dead time.
    from concurrent.futures import ThreadPoolExecutor

    jobs = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        if "bls" in wanted:
            jobs["bls"] = pool.submit(upcoming_releases, days_ahead=days_ahead,
                                      days_back=days_back)
        if "fomc" in wanted:
            jobs["fomc"] = pool.submit(fetch_fomc_meetings)
        if "bea" in wanted:
            jobs["bea"] = pool.submit(fetch_bea_schedule)

    if "bls" in jobs:
        try:
            found, failed = jobs["bls"].result()
        except Exception as exc:
            found, failed = [], [f"schedules unavailable: {str(exc)[:120]}"]
        for e in found:
            e.setdefault("source", "BLS")
        if with_values and found:
            found, value_warnings = attach_release_values(found, today=today, data=values)
            warnings.extend(value_warnings)
        entries.extend(found)
        warnings.extend(f"BLS {f}" for f in failed)

    if "fomc" in jobs:
        try:
            entries.extend(m for m in jobs["fomc"].result() if lo <= m["date"] <= hi)
        except Exception as exc:
            warnings.append(f"FOMC calendar unavailable: {str(exc)[:120]}")

    if "bea" in jobs:
        try:
            found, skipped = jobs["bea"].result()
            entries.extend(e for e in found if lo <= e["date"] <= hi)
        except Exception as exc:
            warnings.append(f"BEA schedule unavailable: {str(exc)[:120]}")

    entries.sort(key=lambda e: (e["date"], e.get("source", ""), e["release"]))
    return entries, warnings


def describe_reading(entry):
    """
    One line of "what this row says", shared by the MCP tool and the dashboard
    so the two can never drift into describing the same row differently.

    A published row shows its own print. A scheduled row shows the previous
    print, labelled ``prior`` and stamped with the period it belongs to -- never
    rendered as if it were an expectation for the release being waited on.
    """
    if entry.get("slug") == "fomc":
        bits = []
        if entry.get("projections"):
            bits.append("projections / dot plot")
        if entry.get("unscheduled"):
            bits.append(entry.get("note") or "unscheduled")
        return " · ".join(bits)

    parts = []
    for v in entry.get("values", []):
        name = v.get("short") or v.get("label")
        head = v.get("headline")
        if v.get("status") == "published" and head:
            parts.append(f"{name} {head['text']}")
        elif (v.get("prior") or {}).get("headline"):
            prior = v["prior"]
            parts.append(f"{name} prior {prior['headline']['text']} ({prior['period']})")

    if not parts and entry.get("value_status") == "awaiting":
        return "due, not yet published"
    return " · ".join(parts)


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
            "accession": accession,
            "primary_document": doc,
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


EARNINGS_ITEM = "2.02"


def earnings_filings(symbol, limit=8):
    """
    The 8-Ks that actually announced results, newest-first.

    Item 2.02 is "Results of Operations and Financial Condition" -- the filing a
    company makes when it releases a quarter. This is the authoritative answer to
    "did they report, and exactly when": ``acceptance`` carries seconds, and in
    practice lands within a minute of the press release.

    It is a record, not a schedule. It can confirm a past quarter and can never
    tell you the date of a future one.
    """
    filings = company_filings(symbol, forms=["8-K"], limit=max(limit * 6, 40))
    hits = [f for f in filings
            if EARNINGS_ITEM in [i.strip() for i in (f.get("items") or "").split(",")]]
    return hits[:limit]


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
