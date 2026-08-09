"""
Central bank and federal statistical series.

Reviewed each source before wiring it, because they differ sharply in what they
cost and what they actually deliver:

  FRED   The backbone. The JSON API needs a free key, but the CSV endpoint
         behind fredgraph serves any single series with no key at all -- 16,853
         daily observations of the 10-year back to 1962, for example. It also
         republishes the other central banks' policy rates, which is the only
         practical route to the Bank of Japan.

  ECB    Statistical Data Warehouse, SDMX-JSON, no key. Fresher and richer for
         euro-area series than FRED's republished copies.

  BoE    The IADB CSV endpoint, no key. Also fresher than FRED: FRED's BOERUKM
         series was discontinued in 2017 and still returns, which is exactly
         the sort of quietly stale number this project exists to catch.

  BoJ    No usable public API. Their statistics site is HTML with no JSON
         endpoint, so the policy rate is taken from FRED and its lag is stated
         rather than hidden -- it is monthly, not daily.

  BEA    Requires a free key. Returns an empty body rather than an error when
         the key is missing, so absence is checked explicitly.

Every fetch runs through a per-host rate limiter and a TTL cache, the same
shape as the BLS and SEC clients.
"""
import csv
import datetime
import io
import json
import os
import urllib.parse

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
    from dashboard import econ_calendar as ec
except ImportError:
    import normalization as nz
    import econ_calendar as ec

FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()
BEA_API_KEY = os.getenv("BEA_API_KEY", "").strip()

FRED_REGISTRATION_URL = "https://fredaccount.stlouisfed.org/apikeys"
BEA_REGISTRATION_URL = "https://apps.bea.gov/API/signup/"

# Public data portals, courteous spacing. None publishes a hard limit the way
# the SEC does, so these are conservative rather than derived.
FRED_LIMITER = ec._HostLimiter(min_interval=0.2)
ECB_LIMITER = ec._HostLimiter(min_interval=0.2)
BOE_LIMITER = ec._HostLimiter(min_interval=0.3)
BEA_LIMITER = ec._HostLimiter(min_interval=0.3)

TTL_DAILY = 6 * 3600        # daily series revise slowly
TTL_POLICY = 12 * 3600      # policy rates change on scheduled dates


# =====================================================================
# SERIES CATALOGUE
# =====================================================================
# key -> (source, series id, label, unit)

SERIES = {
    # --- Policy rates -------------------------------------------------
    "fed_funds":        ("fred", "DFF", "US Fed Funds Rate (effective)", "percent"),
    "fed_target_upper": ("fred", "DFEDTARU", "US Fed Funds Target — upper bound", "percent"),
    "ecb_deposit":      ("ecb", "FM/D.U2.EUR.4F.KR.DFR.LEV", "ECB Deposit Facility Rate", "percent"),
    "ecb_refi":         ("ecb", "FM/D.U2.EUR.4F.KR.MRR_FR.LEV", "ECB Main Refinancing Rate", "percent"),
    "boe_bank_rate":    ("boe", "IUDBEDR", "Bank of England Bank Rate", "percent"),
    "boe_sonia":        ("fred", "IUDSOIA", "SONIA (BoE overnight index)", "percent"),
    "boj_call_rate":    ("fred", "IRSTCI01JPM156N", "Bank of Japan Call Rate (monthly)", "percent"),

    # --- US yield curve ----------------------------------------------
    "us_2y":            ("fred", "DGS2", "US 2-Year Treasury Yield", "percent"),
    "us_10y":           ("fred", "DGS10", "US 10-Year Treasury Yield", "percent"),
    "us_30y":           ("fred", "DGS30", "US 30-Year Treasury Yield", "percent"),
    "curve_10y_2y":     ("fred", "T10Y2Y", "US 10Y minus 2Y spread", "percent"),
    "curve_10y_3m":     ("fred", "T10Y3M", "US 10Y minus 3M spread", "percent"),
    "breakeven_10y":    ("fred", "T10YIE", "10-Year Breakeven Inflation", "percent"),

    # --- Growth, inflation, labour -----------------------------------
    "us_cpi":           ("fred", "CPIAUCSL", "US CPI, All Urban (SA)", "index"),
    "us_core_pce":      ("fred", "PCEPILFE", "US Core PCE Price Index", "index"),
    "us_gdp_real":      ("fred", "GDPC1", "US Real GDP (chained 2017$)", "billions"),
    "us_unemployment":  ("fred", "UNRATE", "US Unemployment Rate", "percent"),
    "euro_hicp":        ("ecb", "ICP/M.U2.N.000000.4.ANR", "Euro area HICP, annual rate", "percent"),

    # --- Financial conditions ----------------------------------------
    "dollar_index":     ("fred", "DTWEXBGS", "US Dollar Index (broad, goods & services)", "index"),
    "vix":              ("fred", "VIXCLS", "CBOE Volatility Index", "index"),
    "hy_spread":        ("fred", "BAMLH0A0HYM2", "US High-Yield Option-Adjusted Spread", "percent"),
    "mortgage_30y":     ("fred", "MORTGAGE30US", "US 30-Year Fixed Mortgage Rate", "percent"),
}

SOURCE_LABELS = {
    "fred": "FRED (Federal Reserve Bank of St. Louis)",
    "ecb": "European Central Bank SDW",
    "boe": "Bank of England IADB",
    "bea": "US Bureau of Economic Analysis",
}


# =====================================================================
# FETCHERS
# =====================================================================

def _fred_series(series_id: str, observations: int = 260):
    """
    One FRED series, newest first.

    Uses the keyless CSV endpoint unless FRED_API_KEY is set, in which case the
    JSON API is used for server-side date filtering. Both return the same shape.
    """
    cache_key = ("fred", series_id, observations, bool(FRED_API_KEY))
    hit = ec.CACHE.get(cache_key, TTL_DAILY)
    if hit is not None:
        return hit

    FRED_LIMITER.acquire()
    rows = []

    if FRED_API_KEY:
        params = {"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
                  "sort_order": "desc", "limit": str(observations)}
        body = json.loads(ec._http(
            "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(params)))
        if "error_message" in body:
            raise RuntimeError(f"FRED rejected the request: {body['error_message']}")
        for o in body.get("observations", []):
            value = nz.parse_number(o.get("value"))
            if value is not None:
                rows.append({"date": o["date"], "value": value})
    else:
        text = ec._http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header or len(header) < 2:
            raise RuntimeError(f"FRED returned no data for '{series_id}'")
        for row in reader:
            if len(row) < 2:
                continue
            value = nz.parse_number(row[1])
            if value is not None:
                rows.append({"date": row[0], "value": value})
        rows.reverse()
        rows = rows[:observations]

    if not rows:
        raise RuntimeError(f"FRED series '{series_id}' returned no usable observations")
    ec.CACHE.put(cache_key, rows)
    return rows


def _ecb_series(flow_key: str, observations: int = 260):
    """One ECB SDW series via SDMX-JSON, newest first. No key required."""
    cache_key = ("ecb", flow_key, observations)
    hit = ec.CACHE.get(cache_key, TTL_POLICY)
    if hit is not None:
        return hit

    ECB_LIMITER.acquire()
    url = (f"https://data-api.ecb.europa.eu/service/data/{flow_key}"
           f"?format=jsondata&lastNObservations={int(observations)}")
    body = json.loads(ec._http(url))

    series = body.get("dataSets", [{}])[0].get("series", {})
    if not series:
        raise RuntimeError(f"ECB returned no series for '{flow_key}'")
    observations_map = series[list(series)[0]].get("observations", {})

    # SDMX keeps the time labels in a parallel structure, indexed by position.
    dims = body.get("structure", {}).get("dimensions", {}).get("observation", [])
    periods = dims[0].get("values", []) if dims else []

    rows = []
    for idx, payload in observations_map.items():
        value = payload[0] if payload else None
        if value is None:
            continue
        i = int(idx)
        label = periods[i].get("id") if i < len(periods) else str(i)
        rows.append({"date": label, "value": float(value)})

    rows.sort(key=lambda r: r["date"], reverse=True)
    ec.CACHE.put(cache_key, rows)
    return rows


def _boe_series(series_code: str, observations: int = 260):
    """One Bank of England IADB series, newest first. No key required."""
    cache_key = ("boe", series_code, observations)
    hit = ec.CACHE.get(cache_key, TTL_POLICY)
    if hit is not None:
        return hit

    today = datetime.date.today()
    start = today - datetime.timedelta(days=max(observations * 2, 400))
    fmt = lambda d: d.strftime("%d/%b/%Y")

    BOE_LIMITER.acquire()
    url = ("https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp?csv.x=yes"
           f"&Datefrom={fmt(start)}&Dateto={fmt(today)}&SeriesCodes={series_code}"
           "&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
    text = ec._http(url, headers={"User-Agent": ec.SEC_USER_AGENT or "Finance MCP"})

    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    rows = []
    for row in reader:
        if len(row) < 2:
            continue
        value = nz.parse_number(row[1])
        if value is None:
            continue
        try:
            iso = datetime.datetime.strptime(row[0].strip(), "%d %b %Y").date().isoformat()
        except ValueError:
            iso = row[0].strip()
        rows.append({"date": iso, "value": value})

    if not rows:
        raise RuntimeError(f"Bank of England returned no data for '{series_code}'")
    rows.reverse()
    rows = rows[:observations]
    ec.CACHE.put(cache_key, rows)
    return rows


def bea_dataset(table: str = "T10101", frequency: str = "Q", year: str = "LAST5"):
    """
    A BEA NIPA table. Requires BEA_API_KEY.

    BEA answers a keyless request with an empty body rather than an error, so
    the missing key is reported here instead of surfacing as a parse failure.
    """
    if not BEA_API_KEY:
        raise RuntimeError(
            "BEA_API_KEY is not set. Register free at "
            f"{BEA_REGISTRATION_URL} and add BEA_API_KEY to .env.")

    cache_key = ("bea", table, frequency, year)
    hit = ec.CACHE.get(cache_key, TTL_DAILY)
    if hit is not None:
        return hit

    params = {"UserID": BEA_API_KEY, "method": "GetData", "DataSetName": "NIPA",
              "TableName": table, "Frequency": frequency, "Year": year,
              "ResultFormat": "JSON"}
    BEA_LIMITER.acquire()
    raw = ec._http("https://apps.bea.gov/api/data/?" + urllib.parse.urlencode(params))
    if not raw.strip():
        raise RuntimeError("BEA returned an empty response — the key is usually the cause.")

    body = json.loads(raw)
    results = body.get("BEAAPI", {}).get("Results", {})
    if "Error" in results:
        raise RuntimeError(f"BEA rejected the request: {results['Error'].get('APIErrorDescription', results['Error'])}")

    rows = [{
        "line": r.get("LineDescription", ""),
        "period": r.get("TimePeriod", ""),
        "value": nz.parse_number(r.get("DataValue")),
        "unit": r.get("CL_UNIT", ""),
    } for r in results.get("Data", [])]

    out = {"table": table, "notes": results.get("Notes", []), "rows": rows}
    ec.CACHE.put(cache_key, out)
    return out


# =====================================================================
# PUBLIC INTERFACE
# =====================================================================

def _observed_cadence_days(rows):
    """
    Typical gap between observations, measured from the data itself.

    A fixed tolerance cannot serve both a daily yield and a monthly policy rate:
    it either cries wolf on the monthly series or misses a dead daily one. The
    series tells us its own cadence, so use that.
    """
    gaps = []
    for a, b in zip(rows, rows[1:]):
        try:
            d1 = datetime.date.fromisoformat(a["date"][:10])
            d2 = datetime.date.fromisoformat(b["date"][:10])
        except ValueError:
            continue
        delta = (d1 - d2).days
        if 0 < delta < 400:
            gaps.append(delta)
        if len(gaps) >= 12:
            break
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]


def fetch_series(keys, observations: int = 24):
    """
    Fetch one or more catalogued series, with change calculations and a
    staleness check on each.

    The staleness check earns its place: FRED still serves BOERUKM, a Bank of
    England series discontinued in 2017, and nothing in the response says so.
    """
    keys = [k for k in keys if k in SERIES]
    if not keys:
        raise ValueError(f"No known series requested. Available: {', '.join(SERIES)}")

    today = datetime.date.today()
    out = {}

    for key in keys:
        source, series_id, label, unit = SERIES[key]
        try:
            if source == "fred":
                rows = _fred_series(series_id, max(observations * 2, 60))
            elif source == "ecb":
                rows = _ecb_series(series_id, max(observations * 2, 60))
            elif source == "boe":
                rows = _boe_series(series_id, max(observations * 2, 60))
            else:
                raise RuntimeError(f"No fetcher for source '{source}'")
        except Exception as e:
            out[key] = {"error": str(e)[:160], "label": label, "source": source,
                        "series_id": series_id}
            continue

        rows = rows[:observations]
        latest = rows[0] if rows else None

        age_days = None
        stale = False
        cadence = _observed_cadence_days(rows)
        if latest:
            try:
                seen = datetime.date.fromisoformat(latest["date"][:10])
                age_days = (today - seen).days
                # Late by more than three of its own publication intervals.
                # Weekends and holidays make the floor necessary for daily series.
                tolerance = max((cadence or 30) * 3, 10)
                stale = age_days > tolerance
            except ValueError:
                pass

        prev = rows[1]["value"] if len(rows) > 1 else None
        year_ago = None
        if latest:
            for r in rows:
                if r["date"][:4] and r["date"] < latest["date"][:4]:
                    pass
            year_ago = rows[min(len(rows) - 1, 12)]["value"] if len(rows) > 12 else None

        out[key] = {
            "label": label, "source": source, "series_id": series_id, "unit": unit,
            "observations": rows, "latest": latest,
            "change": (latest["value"] - prev) if (latest and prev is not None) else None,
            "change_vs_12_ago": (latest["value"] - year_ago) if (latest and year_ago is not None) else None,
            "age_days": age_days, "stale": stale, "cadence_days": cadence,
        }

    return out


def source_status():
    """Configuration and key requirements for every source in this module."""
    return {
        "fred": {
            "tier": "API key" if FRED_API_KEY else "keyless CSV",
            "note": "" if FRED_API_KEY else
                    f"Works without a key. A free key at {FRED_REGISTRATION_URL} "
                    "enables the JSON API and server-side filtering.",
        },
        "ecb": {"tier": "keyless", "note": "No registration required."},
        "boe": {"tier": "keyless", "note": "No registration required."},
        "boj": {"tier": "via FRED", "note": "No public API; the call rate comes from "
                                            "FRED and is monthly, not daily."},
        "bea": {
            "tier": "API key" if BEA_API_KEY else "not configured",
            "note": "" if BEA_API_KEY else
                    f"Requires a free key from {BEA_REGISTRATION_URL}; BEA returns an "
                    "empty body rather than an error when it is missing.",
        },
    }
