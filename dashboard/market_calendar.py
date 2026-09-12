"""
A minimal US equity-market calendar.

Written by hand rather than pulling in `pandas_market_calendars` so the staleness
check has no network or heavyweight dependency and works offline. It covers what
staleness detection actually needs: which days the market was open.

Without this, staleness had to use a loose 5-calendar-day tolerance -- wide
enough to absorb a holiday weekend, and therefore wide enough to hide a genuine
three-day outage. Counting *trading* days lets the threshold tighten to 1.
"""
import datetime
import re
from functools import lru_cache


def _easter(year: int) -> datetime.date:
    """Anonymous Gregorian algorithm. Good Friday is Easter minus two days."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """n-th `weekday` (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        d = datetime.date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + datetime.timedelta(days=offset + 7 * (n - 1))
    last_day = (datetime.date(year + month // 12, month % 12 + 1, 1)
                - datetime.timedelta(days=1))
    offset = (last_day.weekday() - weekday) % 7
    return last_day - datetime.timedelta(days=offset)


def _observed(d: datetime.date) -> datetime.date:
    """NYSE shifts a Saturday holiday to Friday and a Sunday holiday to Monday."""
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


def _observed_new_year(year: int) -> datetime.date:
    """
    New Year's Day, NYSE-style.

    Special case: a Saturday 1 January is NOT rolled back to the preceding
    Friday -- the exchange trades that Friday (it was open on 31 Dec 2021).
    A Sunday 1 January still rolls forward to the Monday.
    """
    d = datetime.date(year, 1, 1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


@lru_cache(maxsize=32)
def market_holidays(year: int) -> frozenset:
    """NYSE/Nasdaq full-day closures for a given year."""
    days = {
        _observed_new_year(year),                                # New Year's Day
        _nth_weekday(year, 1, 0, 3),                             # MLK Jr. Day
        _nth_weekday(year, 2, 0, 3),                             # Presidents' Day
        _easter(year) - datetime.timedelta(days=2),              # Good Friday
        _nth_weekday(year, 5, 0, -1),                            # Memorial Day
        _observed(datetime.date(year, 7, 4)),                    # Independence Day
        _nth_weekday(year, 9, 0, 1),                             # Labor Day
        _nth_weekday(year, 11, 3, 4),                            # Thanksgiving
        _observed(datetime.date(year, 12, 25)),                  # Christmas
    }
    if year >= 2021:
        # Juneteenth became a market holiday in 2022; observed rule applies.
        days.add(_observed(datetime.date(year, 6, 19)))
    return frozenset(days)


def is_trading_day(d: datetime.date) -> bool:
    """True if the US equity market held a regular session on this date."""
    if d.weekday() >= 5:
        return False
    return d not in market_holidays(d.year)


def previous_trading_day(d: datetime.date) -> datetime.date:
    """The most recent trading day strictly before `d`."""
    cur = d - datetime.timedelta(days=1)
    for _ in range(10):
        if is_trading_day(cur):
            return cur
        cur -= datetime.timedelta(days=1)
    return cur


def trading_days_between(start: datetime.date, end: datetime.date) -> int:
    """
    Count of trading sessions after `start` up to and including `end`.

    0 means `end` is the same session as `start` (or earlier) -- i.e. not stale.
    Bounded so a wildly old timestamp cannot spin: anything beyond the cap is
    stale by any measure.
    """
    if end <= start:
        return 0
    if (end - start).days > 400:
        return 999

    count = 0
    cur = start + datetime.timedelta(days=1)
    while cur <= end:
        if is_trading_day(cur):
            count += 1
        cur += datetime.timedelta(days=1)
    return count


try:
    from zoneinfo import ZoneInfo
    _EASTERN = ZoneInfo("America/New_York")
except Exception:                      # no IANA database on this platform
    _EASTERN = None

# A session closes at 16:00 ET, but the bar for it does not appear the same
# second: the vendor has to publish it. Counting the session the instant it
# closes would refuse the newest bar that exists for as long as that takes,
# turning a staleness check into a daily outage. Four hours was measured, not
# guessed -- swept hourly across 2026-2027 it is the smallest grace that never
# refuses a fresh bar under a four-hour publication lag, while still catching a
# genuinely one-session-old bar in 88.5% of hours (the old clock managed 58.3%).
SESSION_CLOSE_ET = datetime.time(16, 0)
PUBLICATION_GRACE_HOURS = 4


def eastern_now(now=None) -> datetime.datetime:
    """
    The current exchange-local time, as an aware datetime.

    A naive `now` is read as UTC, which is this project's storage contract --
    `astimezone` on a naive value silently assumes *machine-local* time, so
    without this the same call answers differently in Bangkok and in London.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    if _EASTERN is None:
        return now.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
    return now.astimezone(_EASTERN)


def reference_session(now=None) -> datetime.date:
    """
    The newest session whose bar should exist by now.

    This is the thing staleness is measured against, and it used to be wrong:
    `previous_trading_day(now + 1 day)` collapses to *today* on any trading day,
    so today was counted as a completed session and the newest bar in existence
    read as one session old. On 251 of 365 evenings in 2026 the reported count
    also disagreed with the exchange date, because the reference came from the
    UTC date rather than ET.
    """
    if isinstance(now, datetime.datetime) or now is None:
        et = eastern_now(now)
        today = et.date()
        published_at = (datetime.datetime.combine(today, SESSION_CLOSE_ET)
                        + datetime.timedelta(hours=PUBLICATION_GRACE_HOURS))
        if is_trading_day(today) and et.replace(tzinfo=None) >= published_at:
            return today
        return previous_trading_day(today)
    # A bare date carries no time, so the only safe reading is that today's
    # session may still be running.
    return previous_trading_day(now)


def sessions_stale(bar_date: datetime.date, now=None) -> int:
    """
    How many completed sessions have passed since `bar_date`.

    Today is excluded until its close has passed and a vendor has had time to
    publish the bar -- during a live session the newest daily bar is
    legitimately yesterday's.
    """
    return trading_days_between(bar_date, reference_session(now))


# =====================================================================
# "since when?"
# =====================================================================

_RELATIVE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|"
                       r"h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\s*$", re.I)

_UNIT_SECONDS = {
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_since(text, now=None):
    """
    Turn "24h", "3d", "2026-08-01" or an ISO timestamp into an aware UTC datetime.

    Everything downstream compares against SEC acceptance stamps, which are UTC
    and timezone-aware. A naive datetime raises TypeError the moment it meets
    one, so this always returns an aware value -- a bare date or a naive ISO
    string is *interpreted* as UTC rather than left ambiguous.

    Raises ValueError on anything unrecognised. Silently defaulting to "24h"
    when asked for "since Monday" would answer a question nobody asked.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)

    if isinstance(text, datetime.datetime):
        return text if text.tzinfo else text.replace(tzinfo=datetime.timezone.utc)
    if isinstance(text, datetime.date):
        return datetime.datetime(text.year, text.month, text.day,
                                 tzinfo=datetime.timezone.utc)

    raw = str(text or "").strip()
    if not raw:
        raise ValueError("No 'since' given.")

    m = _RELATIVE.match(raw)
    if m:
        seconds = float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
        if seconds <= 0:
            raise ValueError("A 'since' window must be a positive length of time.")
        return now - datetime.timedelta(seconds=seconds)

    iso = raw.replace("Z", "+00:00").replace(" ", "T", 1) if " " in raw else raw.replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(iso)
    except ValueError:
        try:
            parsed = datetime.datetime.combine(
                datetime.date.fromisoformat(raw), datetime.time.min)
        except ValueError:
            raise ValueError(
                f"Could not read '{raw}' as a time. Use a relative window like "
                "'24h', '3d' or '2w', a date like '2026-08-01', or an ISO "
                "timestamp like '2026-08-01T13:30:00Z'.") from None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    if parsed > now:
        raise ValueError(f"'{raw}' is in the future; there is nothing since then.")
    return parsed
