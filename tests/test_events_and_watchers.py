"""The event log, and the watchers that write to it.

Nothing here touches the network. Both watchers take a `fetch` callable so the
feed can be a string in the test, which is the only way to assert on a filing
that landed at a particular moment.
"""
import json
import os

import pytest

from dashboard import events, watchers


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    """Never write to the real log."""
    monkeypatch.setenv("FINANCE_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    yield


def test_an_event_is_recorded_once_however_often_it_is_seen():
    assert events.record(events.FILING, "8-K filed", key="edgar:0001") is True
    assert events.record(events.FILING, "8-K filed", key="edgar:0001") is False
    assert len(events.recent()) == 1


def test_the_notifier_fires_for_a_new_event_and_not_for_a_repeat():
    shown = []
    events.record(events.MACRO, "CPI printed", key="macro:cpi:2026-07",
                  notifier=lambda t, m: shown.append(t))
    events.record(events.MACRO, "CPI printed", key="macro:cpi:2026-07",
                  notifier=lambda t, m: shown.append(t))
    assert shown == ["CPI printed"]


def test_a_broken_notifier_does_not_lose_the_event():
    """The write happens first, on purpose.

    A machine with no notification daemon is the normal case on a server, and
    an event that reached the log but not the screen is recoverable. The other
    way round is not.
    """
    def explode(title, message):
        raise RuntimeError("no notification daemon")

    assert events.record(events.FILING, "8-K filed", key="k", notifier=explode) is True
    assert len(events.recent()) == 1


def test_a_half_written_last_line_does_not_hide_the_events_before_it():
    events.record(events.FILING, "first", key="a")
    with open(events.path(), "a", encoding="utf-8") as handle:
        handle.write('{"kind": "filing", "key": "b"')      # killed mid-write
    assert [r["key"] for r in events.recent()] == ["a"]
    # And the log still accepts writes afterwards.
    assert events.record(events.FILING, "third", key="c") is True


def test_recent_filters_by_time_and_kind():
    events.record(events.FILING, "f", key="1", at=_when("2026-01-01T00:00:00+00:00"))
    events.record(events.MACRO, "m", key="2", at=_when("2026-06-01T00:00:00+00:00"))
    assert len(events.recent(since="2026-03-01T00:00:00+00:00")) == 1
    assert len(events.recent(kinds=[events.MACRO])) == 1


def _when(text):
    import datetime
    return datetime.datetime.fromisoformat(text)


ATOM = """<feed>
 <entry>
  <title>SC TO-I - EXAMPLE CORP (0001234567) (Subject)</title>
  <link rel="alternate" href="https://www.sec.gov/x?accession-number=0001234567-26-000001"/>
  <updated>2026-08-20T10:00:00-04:00</updated>
 </entry>
 <entry>
  <title>8-K - OTHER CO (0007654321) (Filer)</title>
  <link rel="alternate" href="https://www.sec.gov/y?accession-number=0007654321-26-000002"/>
  <updated>2026-08-20T10:01:00-04:00</updated>
 </entry>
</feed>"""


def test_edgar_entries_become_events_keyed_on_the_accession_number():
    rows = watchers.edgar_filings(fetch=lambda url: ATOM)
    assert [r["key"] for r in rows] == [
        "edgar:0001234567-26-000001",
        "edgar:0007654321-26-000002",
    ]
    assert rows[0]["title"] == "SC TO-I filed"
    assert "EXAMPLE CORP" in rows[0]["detail"]


def test_a_filing_seen_twice_across_restarts_notifies_once():
    """The feed still holds it next minute. The key is what makes that safe."""
    shown = []
    for _ in range(3):
        watchers.poll_once(form_types=("8-K",),
                           notifier=lambda t, m: shown.append(t),
                           fetch=lambda url: ATOM if "browse-edgar" in url else "{}")
    assert len(shown) == 2


BUYBACK = json.dumps({"data": [{
    "operation_date": "2026-08-20",
    "operation_start_time_est": "01:40 PM",
    "operation_close_time_est": "02:00 PM",
    "operation_type": "Liquidity Support",
    "maturity_bucket": "3Y to 5Y",
    "max_par_amt_redeemed": "4000000000",
    "results_xml": "null",
    "nbr_issues_accepted": "null",
}]})


def test_a_buyback_announcement_and_its_results_are_two_events():
    announced = watchers.treasury_buybacks(fetch=lambda url: BUYBACK)
    assert announced[0]["key"] == "treasury:2026-08-20:announced"
    assert "Liquidity Support" in announced[0]["title"]
    assert "$4,000,000,000" in announced[0]["detail"]

    settled = watchers.treasury_buybacks(
        fetch=lambda url: BUYBACK.replace('"results_xml": "null"',
                                          '"results_xml": "BBR_2026.xml"'))
    assert settled[0]["key"] == "treasury:2026-08-20:results"


def test_the_string_null_is_not_a_result():
    """FiscalData sends the *word* null for an absent field, which is truthy."""
    assert watchers._real("null") is False
    assert watchers._real("None") is False
    assert watchers._real("") is False
    assert watchers._real("BBR_2026.xml") is True


def test_a_source_that_fails_is_reported_and_does_not_stop_the_others():
    def fetch(url):
        if "browse-edgar" in url:
            raise TimeoutError("SEC timed out")
        return BUYBACK

    out = watchers.poll_once(form_types=("8-K",), fetch=fetch)
    assert out["errors"] and "timed out" in out["errors"][0]
    assert [r["kind"] for r in out["new"]] == [events.BUYBACK]


def test_describe_renders_without_json():
    events.record(events.BUYBACK, "Treasury buyback announced", key="t:1",
                  detail="3Y to 5Y")
    text = events.describe(events.recent())
    assert "Treasury buyback announced" in text and "3Y to 5Y" in text


def test_the_loop_polls_filings_while_it_waits_for_a_release(monkeypatch):
    """The bug this covers: watchers that were built, tested and never called.

    `poll_once` existed and passed its own tests while nothing in the running
    server invoked it, so a filing was only ever noticed if a macro release
    happened to be due. A watcher that only wakes on a calendar is a scheduler.
    """
    from dashboard import macro_watch

    polls, slept = [], []
    monkeypatch.setattr(macro_watch, "FILING_POLL", 60)
    monkeypatch.setattr(watchers, "poll_once",
                        lambda **kw: polls.append(kw) or {"new": [], "errors": []})
    macro_watch._sleep_in_pieces(180, lambda s: slept.append(s))

    assert slept == [60, 60, 60], "should wait in filing-sized pieces"
    assert len(polls) == 3, "and ask the sources after each one"


def test_a_failing_filing_source_does_not_break_the_wait(monkeypatch):
    from dashboard import macro_watch

    monkeypatch.setattr(macro_watch, "FILING_POLL", 30)
    monkeypatch.setattr(watchers, "poll_once",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("SEC 503")))
    slept = []
    macro_watch._sleep_in_pieces(60, lambda s: slept.append(s))
    assert slept == [30, 30]
    assert "SEC 503" in macro_watch.WATCH_STATE["last_error"]
