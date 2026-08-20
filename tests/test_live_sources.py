"""
Every external source, asked for real. Opt in with FINANCE_LIVE_SOURCES=1.

Every other test in this repository stubs the network, and that is right: a
suite that fails because SEC returned a 503 at three in the morning is a suite
nobody trusts. But the two defects found in this code on 20 August 2026 were
both invisible to all 1,423 of those tests, for the same reason:

  * The euro-area HICP series had stopped publishing eight months earlier. The
    staleness check that exists to catch exactly that could not read its date
    label, computed no age, and reported it fresh. Every mock in the suite
    supplied a well-formed date, so no mock could have found it.

  * `poll_once` returns its errors rather than raising them, and the watcher
    loop discarded the return value. Without SEC_USER_AGENT set, SEC answers
    every request with 403 and the watcher reported itself healthy. Every test
    injected a working `fetch`, so no test ever saw a 403.

Both were found by running the thing against the real internet. This file makes
that repeatable instead of a thing somebody remembered to do once.

**What it asserts, and what it deliberately does not.** A source being
unreachable is not a defect in this code, so it is reported and skipped. What
is asserted is that *when a source answers, this code's account of the answer is
true*: the shape is what the callers assume, and — the important one — the
tool's own `stale` verdict agrees with the observation date it is sitting next
to. A number presented as current that is eight months old is the failure this
whole file exists for, and it is checkable without depending on any particular
value being any particular thing.

    FINANCE_LIVE_SOURCES=1 pytest tests/test_live_sources.py -v -s
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import central_banks as cb
from dashboard import econ_calendar as ec
from dashboard import envfile
from dashboard import watchers

LIVE = os.getenv("FINANCE_LIVE_SOURCES", "").strip().lower() in ("1", "true", "yes", "on")

pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="live sources are opt-in: set FINANCE_LIVE_SOURCES=1",
)


@pytest.fixture(scope="module", autouse=True)
def _credentials():
    """Load .env the way the server does.

    Not a convenience. Running these without it is how the SEC 403 was found,
    and a test that silently ran unauthenticated would report a source as
    broken when the only thing missing was a contact address.
    """
    envfile.load_env()


def _unreachable(reason):
    pytest.skip(f"source unreachable, which is not a defect in this code: {reason}")


# ---------------------------------------------------------------------------
# Central banks and federal series: FRED, ECB, BoE.


def test_every_catalogued_series_answers_and_is_shaped_as_callers_assume():
    try:
        rows = cb.fetch_series(list(cb.SERIES), observations=24)
    except Exception as exc:
        _unreachable(exc)

    broken = []
    for key in cb.SERIES:
        row = rows.get(key)
        if row is None:
            broken.append(f"{key}: absent from the result")
            continue
        if row.get("error"):
            # One source down is not this code being wrong.
            print(f"  {key:<18} unreachable: {row['error'][:70]}")
            continue
        for field in ("label", "source", "unit", "observations", "latest",
                      "age_days", "stale", "cadence_days"):
            if field not in row:
                broken.append(f"{key}: no {field}")
        if not row.get("observations"):
            broken.append(f"{key}: answered with no observations")

    assert not broken, "series whose shape callers cannot rely on:\n  " + "\n  ".join(broken)


def test_the_staleness_verdict_agrees_with_the_date_beside_it():
    """The invariant the euro-HICP defect broke.

    Not "nothing is stale" — series get discontinued and that is the world's
    business, not a bug. The bug is a series being stale and *not being marked*
    stale, because that is the one that puts an eight-month-old number in front
    of somebody as though it were today's.
    """
    try:
        rows = cb.fetch_series(list(cb.SERIES), observations=24)
    except Exception as exc:
        _unreachable(exc)

    today = datetime.date.today()
    lying, flagged = [], []
    for key, row in rows.items():
        if row.get("error") or not row.get("latest"):
            continue
        seen = cb._as_date(row["latest"]["date"])
        if seen is None:
            lying.append(f"{key}: date {row['latest']['date']!r} could not be read at all")
            continue
        age = (today - seen).days
        tolerance = max((row.get("cadence_days") or 30) * 3, 10)
        really_stale = age > tolerance
        if really_stale and not row["stale"]:
            lying.append(
                f"{key}: {age}d old against a {row.get('cadence_days')}d cadence "
                f"and reported fresh"
            )
        if row["stale"]:
            flagged.append(f"{key} ({age}d)")

    if flagged:
        print("\n  correctly flagged stale: " + ", ".join(flagged))
    assert not lying, "a number presented as current that is not:\n  " + "\n  ".join(lying)


def test_no_series_reports_an_age_it_did_not_compute():
    """`age_days is None` was how the dead series slipped through.

    It is allowed — a source can answer with a label nobody can parse — but it
    must then be stale, never fresh. Unknown is not fresh.
    """
    try:
        rows = cb.fetch_series(list(cb.SERIES), observations=24)
    except Exception as exc:
        _unreachable(exc)

    wrong = [
        key
        for key, row in rows.items()
        if not row.get("error") and row.get("age_days") is None and not row.get("stale")
    ]
    assert not wrong, f"no age computed and reported fresh anyway: {wrong}"


# ---------------------------------------------------------------------------
# BLS: the numbers and the calendar that says when they land.


def test_the_bls_series_this_tool_publishes_all_answer():
    try:
        data = ec.fetch_bls_series(list(ec.BLS_SERIES))
    except Exception as exc:
        _unreachable(exc)

    missing = [key for key in ec.BLS_SERIES if key not in data or not data.get(key)]
    assert not missing, (
        f"BLS answered without these series, which the release join depends on: {missing}"
    )


def test_the_release_calendar_parses_into_dates():
    """A scraped HTML schedule is the most fragile thing here.

    It is scraped because BLS publishes no API for it. If the page layout
    changes, `upcoming_releases` returns nothing and the watcher waits forever
    for a release that never appears to be due — silently, which is the part
    that matters.
    """
    try:
        found, failed = ec.upcoming_releases(days_ahead=120, days_back=30)
    except Exception as exc:
        _unreachable(exc)

    if failed:
        print("\n  schedules that would not load: " + "; ".join(failed))
    assert found, (
        "no BLS release parsed out of a 150-day window. Either every schedule "
        f"page failed to load, or the parser no longer matches the page. {failed}"
    )
    for entry in found[:5]:
        assert isinstance(entry.get("date"), datetime.date), entry
        assert entry.get("release"), entry
    print(f"\n  {len(found)} releases in a 150-day window; next is "
          f"{found[0]['date']} {found[0]['release']}")


# ---------------------------------------------------------------------------
# The watched sources: SEC and Treasury.


def test_edgar_answers_rather_than_refusing_the_user_agent():
    """The 403 case, made loud.

    SEC refuses traffic that does not declare a contact address, and the
    watcher swallowed that refusal for as long as it existed. A 403 here means
    SEC_USER_AGENT is missing or rejected — which is a configuration failure,
    not an outage, and the two must not look the same.
    """
    rows = watchers.edgar_filings("8-K", count=10)
    errors = [r["error"] for r in rows if "error" in r]
    refused = [e for e in errors if "403" in e]
    assert not refused, (
        "SEC refused the request. SEC_USER_AGENT must be a contact address; "
        f"without one every filing poll fails silently. {refused}"
    )
    if errors:
        _unreachable(errors[0])

    assert rows, "EDGAR answered with no entries at all"
    for row in rows:
        assert row["key"].startswith("edgar:"), row
        assert row["kind"] == watchers.events.FILING, row
        assert row["title"], row
    # The accession number is the only stable identity a filing has. Falling
    # back to the link means restarts re-notify, so it is worth knowing.
    fallback = [r for r in rows if not r["key"][6:].replace("-", "").isdigit()]
    assert not fallback, (
        "filings keyed on a link rather than an accession number, which makes "
        f"deduplication across restarts unreliable: {[r['key'] for r in fallback][:3]}"
    )


def test_treasury_buybacks_answer_and_distinguish_announced_from_settled():
    rows = watchers.treasury_buybacks(count=8)
    errors = [r["error"] for r in rows if "error" in r]
    if errors:
        _unreachable(errors[0])

    assert rows, "FiscalData answered with no operations"
    stages = set()
    for row in rows:
        assert row["key"].startswith("treasury:"), row
        stages.add(row["key"].rsplit(":", 1)[1])
    assert stages <= {"announced", "results"}, f"unexpected stage in key: {stages}"
    print(f"\n  {len(rows)} buyback operations, stages seen: {sorted(stages)}")


def test_a_poll_of_every_watched_source_reports_no_errors():
    """The whole watcher path, end to end, against the real feeds.

    Notifier is None, so nothing is shown; the log is redirected, so the real
    one is not touched. What is being checked is that a full pass comes back
    with an empty error list — the condition the loop now surfaces and used to
    discard.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "events.jsonl")
        open(path, "w", encoding="utf-8").close()
        before = os.environ.get("FINANCE_EVENTS_PATH")
        os.environ["FINANCE_EVENTS_PATH"] = path
        try:
            first = watchers.poll_once(notifier=None)
            assert not first["errors"], f"a live poll reported errors: {first['errors']}"
            assert first["new"], "a poll of an empty log recorded nothing at all"

            # And the dedupe that makes restarts survivable.
            second = watchers.poll_once(notifier=None)
            assert not second["errors"], second["errors"]
            assert not second["new"], (
                f"{len(second['new'])} events counted as new on an immediate "
                "second poll, so every restart would re-notify"
            )
            print(f"\n  {len(first['new'])} events on the first poll, "
                  f"{len(second['new'])} on the second")
        finally:
            if before is None:
                os.environ.pop("FINANCE_EVENTS_PATH", None)
            else:
                os.environ["FINANCE_EVENTS_PATH"] = before
