"""Sources that publish on their own schedule, polled and written to the log.

Two of them, both checked against the live feeds rather than from memory:

**EDGAR.** `browse-edgar?action=getcurrent` is an Atom feed of filings as they
are accepted, and it is the only free source that is current to the minute. SEC
asks for a declared User-Agent and permits ten requests a second, so a poll once
a minute is far inside fair use and bounds the latency at a minute.

**Treasury buybacks.** `fiscaldata.treasury.gov` publishes every buyback
operation with its announcement, its twenty-minute window and its results. These
are *announced* rather than sudden -- a preliminary announcement exists before
the operation -- so this watches for the announcement appearing and for the
results landing, which are the two moments that move anything. The
`Liquidity Support` operation type is the one worth waking up for.

Corporate buybacks are the genuinely sudden kind, and they arrive as EDGAR
filings: `SC TO-I` is an issuer tender offer, an 8-K carries the announcement.
So they are covered by the first watcher rather than the second.

Nothing here decides anything. Each poll turns a feed into candidate events and
hands them to `events.record`, which owns deduplication and notification.
"""
import datetime
import json
import os
import re
import urllib.parse
import urllib.request

from . import events

EDGAR_CURRENT = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
    "&type={type}&count={count}&output=atom"
)
TREASURY_BUYBACKS = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/"
    "accounting/od/buybacks_operations"
    "?sort=-operation_date&page%5Bsize%5D={count}"
)

# SEC requires a declared identity and will refuse traffic without one. This is
# the one place the address matters, so it is configurable rather than a
# hard-coded stranger's inbox.
def _agent() -> str:
    return os.getenv("SEC_USER_AGENT", "finance-mcp (set SEC_USER_AGENT to your email)")


def _fetch(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _agent()})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_LINK = re.compile(r'<link[^>]*href="([^"]+)"')
_UPDATED = re.compile(r"<updated>(.*?)</updated>", re.S)
# The accession number is the only stable identity a filing has, and it is in
# the entry id rather than anywhere convenient.
_ACCESSION = re.compile(r"accession-?number=([\d-]+)", re.I)


def edgar_filings(form_type: str = "", count: int = 40, fetch=_fetch) -> list:
    """Candidate events for filings accepted since the last poll."""
    url = EDGAR_CURRENT.format(type=urllib.parse.quote(form_type), count=count)
    try:
        body = fetch(url)
    except Exception as exc:
        return [{"error": str(exc)[:120]}]

    out = []
    for chunk in _ENTRY.findall(body):
        title = (_TITLE.search(chunk) or [None, ""])[1].strip()
        link = (_LINK.search(chunk) or [None, ""])[1].strip()
        updated = (_UPDATED.search(chunk) or [None, ""])[1].strip()
        found = _ACCESSION.search(chunk) or _ACCESSION.search(link)
        key = f"edgar:{found.group(1)}" if found else f"edgar:{link or title}"
        # "SC TO-I - Some Company (0001234567) (Subject)" -- the form is the
        # part before the dash and it is what decides whether anyone cares.
        form = title.split(" - ", 1)[0].strip() if " - " in title else ""
        who = title.split(" - ", 1)[1].strip() if " - " in title else title
        out.append(
            {
                "kind": events.FILING,
                "key": key,
                "title": f"{form} filed" if form else "Filing",
                "detail": who,
                "url": link,
                "at": updated,
            }
        )
    return out


def treasury_buybacks(count: int = 6, fetch=_fetch) -> list:
    """Candidate events for buyback operations announced or settled."""
    try:
        body = fetch(TREASURY_BUYBACKS.format(count=count))
        rows = json.loads(body).get("data", [])
    except Exception as exc:
        return [{"error": str(exc)[:120]}]

    out = []
    for row in rows:
        date = row.get("operation_date", "")
        kind_of = row.get("operation_type", "buyback")
        bucket = row.get("maturity_bucket", "")
        window = f"{row.get('operation_start_time_est','')}-{row.get('operation_close_time_est','')} ET"
        has_results = _real(row.get("results_xml")) or _real(row.get("nbr_issues_accepted"))
        stage = "results" if has_results else "announced"
        cap = row.get("max_par_amt_redeemed") or ""
        out.append(
            {
                "kind": events.BUYBACK,
                # Announcement and results are two events for one operation, so
                # the stage is part of the identity or the second never fires.
                "key": f"treasury:{date}:{stage}",
                "title": f"Treasury buyback {stage} — {kind_of}",
                "detail": f"{bucket} {window}"
                + (f", up to ${int(cap):,}" if str(cap).isdigit() else ""),
                "url": "https://www.treasurydirect.gov/auctions/upcoming/",
                "at": date,
            }
        )
    return out


def _real(value) -> bool:
    """FiscalData sends the *string* "null" for an absent field."""
    return bool(value) and str(value).strip().lower() not in ("null", "none", "")


def poll_once(form_types=("8-K", "SC TO-I"), notifier=None, fetch=_fetch) -> dict:
    """One pass over every source. Returns what was new and what failed.

    Errors are returned rather than raised: a watcher that dies because SEC
    returned a 503 at three in the morning is a watcher that was not running
    when the thing it existed for happened.
    """
    # The first poll of a feed sees everything the feed holds, and all of it is
    # new because there is no history yet. Those get recorded — so the next poll
    # knows them — and deliberately not shown. An install that greets you with
    # fifty notifications about filings from before you had it is not an alert
    # system, and this was measured rather than imagined: eighteen seconds
    # against the live feeds produced fifty-six.
    if events.first_run():
        notifier = None

    new, errors = [], []
    candidates = []
    for form in form_types:
        candidates.extend(edgar_filings(form, fetch=fetch))
    candidates.extend(treasury_buybacks(fetch=fetch))

    for row in candidates:
        if "error" in row:
            errors.append(row["error"])
            continue
        if events.record(
            kind=row["kind"],
            title=row["title"],
            key=row["key"],
            detail=row.get("detail", ""),
            symbol=row.get("symbol", ""),
            url=row.get("url", ""),
            notifier=notifier,
        ):
            new.append(row)
    return {"new": new, "errors": errors}


def seed_quietly(fetch=_fetch) -> int:
    """Fill an empty log without notifying, and say how many it took in.

    Exposed so a first run can be done on purpose rather than as a side effect
    of the first poll happening to be first.
    """
    return len(poll_once(notifier=None, fetch=fetch)["new"])
