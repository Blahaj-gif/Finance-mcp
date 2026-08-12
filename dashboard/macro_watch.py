"""
Watch a macro release so the print is already in hand when someone asks.

The cache-TTL work made the answer *fresh whenever you ask*. This makes it
*already there*, which is a different and better property: nobody waits on a
round trip to BLS at the moment they want the number.

**Why this is not a burst.** The tempting design is 40 concurrent calls across
the release instant. Two things are wrong with it, and only one is the rate
limit:

  * BLS documents 50 requests per 10 seconds. 40 calls in 5 seconds is 80 per
    10 -- over the limit against a government API whose penalty is being cut
    off, on the one morning of the month you need it.

  * More fundamentally, **concurrency is the wrong tool for waiting on a
    scheduled event.** Firing forty requests at once does not make BLS publish
    sooner; every one of them returns the same stale payload. You are not
    throughput-limited, you are waiting, and the only thing that shortens the
    wait is asking again *after* the state changed. Forty parallel questions
    asked at 08:30:00.0 all get yesterday's answer. One question at 08:30:01
    gets today's.

So this polls **serially**, densest exactly where publication is most likely:

    announced instant - 10s ... +20s     every  1s   (10 req/10s)
                        +20s ... +2min   every  3s
                        +2min ... +20min every 30s

Peak is a fifth of the documented ceiling, and the loop stops the instant the
print lands -- so a punctual release costs one or two calls, not forty.
"""
import datetime
import os
import sys
import threading
import time

try:
    from dashboard import econ_calendar as ec
except ImportError:  # imported as a top-level module from dashboard/
    import econ_calendar as ec

#: Cadence tiers: (seconds after the announced instant, poll interval).
#: Densest where the event is, rather than uniformly fast -- which is the
#: defensible half of "burst at the release".
CADENCE = ((20, 1.0), (120, 3.0), (1200, 30.0))

#: How long before the announced instant to start asking. Deliberately short:
#: a publication cannot be detected before it happens, so every call made
#: earlier is a guaranteed-stale request. Ninety seconds of lead at a one-second
#: cadence was ninety such calls -- it took the worst case from 90 to 179 and
#: bought nothing but tolerance for a clock that is wrong by a minute and a
#: half. Ten seconds covers ordinary skew and BLS being a moment early.
LEAD = datetime.timedelta(seconds=10)

#: Never wake more than this far ahead; a process that sleeps for a week is a
#: process that has stopped being restartable in any useful sense.
MAX_SLEEP = 15 * 60

WATCH_STATE = {"running": False, "watching": None, "last_landed": None,
               "calls": 0, "windows": 0, "last_error": None}


def poll_interval(seconds_since_release: float) -> float:
    """The gap before the next request, given how long since the instant."""
    for edge, interval in CADENCE:
        if seconds_since_release <= edge:
            return interval
    return None          # window closed


def peak_requests_per_10s() -> float:
    """For anyone checking this against BLS's documented 50."""
    return 10.0 / min(interval for _, interval in CADENCE)


def worst_case_calls() -> int:
    """
    Every call a single window can cost if the release never arrives. The
    realistic cost is one or two: the loop stops the moment the print lands.
    """
    total, previous = 0.0, -LEAD.total_seconds()
    for edge, interval in CADENCE:
        total += (edge - previous) / interval
        previous = edge
    return int(total)


def next_release(entries, now=None):
    """The next release worth watching, and when. None if there is not one."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    upcoming = []
    for entry in entries or []:
        if not ec.RELEASE_SERIES.get(entry.get("slug")):
            continue          # we publish none of its series; nothing to warm
        moment = ec.release_moment(entry)
        if moment is not None and moment + ec.WINDOW_CLOSES_AFTER > now:
            upcoming.append((moment, entry))
    if not upcoming:
        return None, None
    upcoming.sort(key=lambda pair: pair[0])
    return upcoming[0]


def watch_release(entry, now=None, sleeper=time.sleep, clock=None):
    """
    Poll one release until its print lands or the window closes.

    Returns (landed, calls). Never raises: a watcher that dies on a transport
    error stops watching every future release too, and nothing would say so.
    """
    clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
    moment = ec.release_moment(entry)
    if moment is None:
        return False, 0

    keys = ec.RELEASE_SERIES.get(entry.get("slug")) or []
    calls = 0
    while True:
        elapsed = (clock() - moment).total_seconds()
        interval = poll_interval(elapsed)
        if interval is None:
            return False, calls

        if elapsed >= -LEAD.total_seconds():
            if not ec.can_afford_window():
                # The quota reserve exists so a release cannot leave every
                # other tool unable to answer for the rest of the day.
                return False, calls
            try:
                data = ec.fetch_bls_series(keys, ttl=ec.TTL_MACRO_LIVE)
                calls += 1
                WATCH_STATE["calls"] += 1
                if ec.has_landed(entry, data):
                    return True, calls
            except Exception as exc:
                WATCH_STATE["last_error"] = str(exc)[:200]
        sleeper(interval)


def run_watcher_loop(sleeper=time.sleep, once=False):
    """Sleep until the next release, watch it, repeat."""
    WATCH_STATE["running"] = True
    print("[macro-watch] started; polls only inside a release window",
          file=sys.stderr)
    while True:
        try:
            entries, _ = ec.upcoming_releases(days_ahead=7, days_back=0)
            moment, entry = next_release(entries)
            if entry is None:
                sleeper(MAX_SLEEP)
                if once:
                    return
                continue

            wait = (moment - LEAD - datetime.datetime.now(
                datetime.timezone.utc)).total_seconds()
            if wait > 0:
                sleeper(min(wait, MAX_SLEEP))
                if wait > MAX_SLEEP:
                    if once:
                        return
                    continue

            WATCH_STATE["watching"] = entry.get("release")
            WATCH_STATE["windows"] += 1
            landed, _ = watch_release(entry)
            WATCH_STATE["watching"] = None
            if landed:
                WATCH_STATE["last_landed"] = {
                    "release": entry.get("release"),
                    "period": entry.get("reference_period"),
                    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
        except Exception as exc:
            WATCH_STATE["last_error"] = str(exc)[:200]
            sleeper(60)
        if once:
            return


def enabled() -> bool:
    """
    Off unless asked for. The on-demand path already gives the freshest answer
    available whenever anyone asks, and spends nothing when nobody does. This
    trades that second property away -- it spends quota on a schedule, for
    someone who may not be there -- so it is a choice rather than a default.
    """
    return os.getenv("FINANCE_MACRO_WATCH", "").strip().lower() in (
        "1", "true", "yes", "on")


def start_watcher_once():
    """Start the watcher thread, at most once per process."""
    if not enabled():
        return False
    if WATCH_STATE["running"]:
        return False
    thread = threading.Thread(target=run_watcher_loop, daemon=True,
                              name="finance-mcp-macro-watch")
    thread.start()
    return True


def status() -> str:
    """One line for get_data_sources and the dashboard."""
    if not WATCH_STATE["running"]:
        return ("Macro watcher: not running — the calendar is still fresh on "
                "demand, just not pre-fetched. Set FINANCE_MACRO_WATCH=1 to "
                "have releases fetched as they publish.")
    watching = WATCH_STATE["watching"]
    landed = WATCH_STATE["last_landed"]
    parts = [f"Macro watcher: running, {WATCH_STATE['windows']} windows, "
             f"{WATCH_STATE['calls']} API calls"]
    if watching:
        parts.append(f"watching {watching} now")
    if landed:
        parts.append(f"last landed {landed['release']} ({landed['period']})")
    if WATCH_STATE["last_error"]:
        parts.append(f"last error {WATCH_STATE['last_error']}")
    return " — ".join(parts)
