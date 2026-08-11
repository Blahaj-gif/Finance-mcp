"""
Alert manager tests.

Offline: no broker session, no network, no sleeping. The manager's whole job is
deciding *whether* to interrupt someone, so the properties worth pinning are
the ones that make it cry wolf or stay silent when it shouldn't.
"""
import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import alert_manager as am
from dashboard.alert_manager import AlertEvaluationError


def bars(macd_pairs=None, close=100.0, rsi=50.0):
    """A minimal indicator frame. macd_pairs is [(macd, signal), ...] oldest first."""
    macd_pairs = macd_pairs or [(0.0, 0.0), (0.0, 0.0)]
    return pd.DataFrame([
        {"time": f"2026-08-0{i + 1}", "close": close, "rsi_14": rsi,
         "macd": m, "macd_signal": s}
        for i, (m, s) in enumerate(macd_pairs)
    ])


def alert(**kw):
    base = {"symbol": "AAPL", "condition": "PRICE_ABOVE", "target_value": 90.0,
            "note": "test", "status": "ACTIVE"}
    base.update(kw)
    return base


def stub_fetch(df, source="Webull OpenAPI"):
    return lambda symbol: (df, source)


# =====================================================================
# Condition evaluation
# =====================================================================

@pytest.mark.parametrize("condition,target,close,expected", [
    ("PRICE_ABOVE", 90.0, 100.0, True),
    ("PRICE_ABOVE", 110.0, 100.0, False),
    ("PRICE_BELOW", 110.0, 100.0, True),
    ("PRICE_BELOW", 90.0, 100.0, False),
])
def test_price_conditions(condition, target, close, expected):
    fired, text = am.evaluate_condition(condition, target, {"close": close}, bars())
    assert fired is expected
    assert f"${close:.2f}" in text


@pytest.mark.parametrize("condition,target,rsi,expected", [
    ("RSI_BELOW", 30.0, 22.0, True),
    ("RSI_BELOW", 30.0, 45.0, False),
    ("RSI_ABOVE", 70.0, 81.0, True),
    ("RSI_ABOVE", 70.0, 45.0, False),
])
def test_rsi_conditions(condition, target, rsi, expected):
    fired, _ = am.evaluate_condition(condition, target, {"rsi_14": rsi}, bars())
    assert fired is expected


def test_conditions_are_case_and_whitespace_insensitive():
    fired, _ = am.evaluate_condition("  price_above  ", 90.0, {"close": 100.0}, bars())
    assert fired is True


def test_an_unknown_condition_raises_instead_of_guessing():
    """
    It used to fall through to a price-above check, so a typo in the condition
    became a *different alert* that still fired -- silently.
    """
    with pytest.raises(AlertEvaluationError, match="Unknown condition"):
        am.evaluate_condition("RSI_BELO", 30.0, {"close": 100.0, "rsi_14": 20.0}, bars())


def test_every_condition_offered_by_the_dashboard_is_supported():
    """The alert form's dropdown and the manager's vocabulary must not drift."""
    app = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "dashboard", "app.py"), encoding="utf-8").read()
    for cond in am.CONDITIONS:
        assert cond in app, f"{cond} is not offered anywhere in the dashboard"
    offered = app.split('st.selectbox("Condition Operator", [', 1)[1].split("]", 1)[0]
    for cond in [c.strip().strip('"') for c in offered.split(",")]:
        assert cond in am.CONDITIONS, f"dashboard offers '{cond}', manager cannot evaluate it"


# =====================================================================
# NaN handling -- the difference between "no" and "cannot say"
# =====================================================================

def test_a_nan_indicator_raises_rather_than_reading_as_not_triggered():
    """
    Every comparison against NaN is False, so an indicator still in warm-up
    used to be indistinguishable from a condition that genuinely wasn't met.
    """
    with pytest.raises(AlertEvaluationError, match="NaN"):
        am.evaluate_condition("RSI_BELOW", 30.0, {"rsi_14": float("nan")}, bars())


def test_a_missing_indicator_column_raises():
    with pytest.raises(AlertEvaluationError, match="missing"):
        am.evaluate_condition("RSI_ABOVE", 70.0, {"close": 100.0}, bars())


# =====================================================================
# MACD: a cross is an event, not a state
# =====================================================================

def test_macd_bull_fires_on_the_bar_that_crosses():
    df = bars([(-0.5, 0.1), (0.4, 0.2)])          # below -> above
    fired, text = am.evaluate_condition("MACD_CROSS_BULL", 0, df.iloc[-1].to_dict(), df)
    assert fired is True
    assert "prev" in text


def test_macd_bull_does_not_re_fire_while_merely_above():
    """
    The old check was `macd > signal` -- a state that stays true for as long as
    the trend holds, so the alert re-fired every cooldown window for days after
    the single crossing it was meant to catch.
    """
    df = bars([(0.4, 0.2), (0.6, 0.3)])           # above, and stays above
    fired, _ = am.evaluate_condition("MACD_CROSS_BULL", 0, df.iloc[-1].to_dict(), df)
    assert fired is False


def test_macd_bear_fires_on_the_bar_that_crosses_down():
    df = bars([(0.5, 0.1), (-0.2, 0.1)])
    fired, _ = am.evaluate_condition("MACD_CROSS_BEAR", 0, df.iloc[-1].to_dict(), df)
    assert fired is True


def test_macd_bear_does_not_re_fire_while_merely_below():
    df = bars([(-0.5, 0.1), (-0.7, 0.1)])
    fired, _ = am.evaluate_condition("MACD_CROSS_BEAR", 0, df.iloc[-1].to_dict(), df)
    assert fired is False


def test_a_touch_then_break_counts_as_a_cross():
    """Equality on the previous bar is 'not yet above', so the break still fires."""
    df = bars([(0.2, 0.2), (0.5, 0.2)])
    fired, _ = am.evaluate_condition("MACD_CROSS_BULL", 0, df.iloc[-1].to_dict(), df)
    assert fired is True


def test_macd_needs_two_bars():
    df = bars([(0.4, 0.2)])
    with pytest.raises(AlertEvaluationError, match="previous bar"):
        am.evaluate_condition("MACD_CROSS_BULL", 0, df.iloc[-1].to_dict(), df)


# =====================================================================
# Cooldown
# =====================================================================

def test_an_active_alert_is_never_in_cooldown():
    assert am.is_in_cooldown(alert(status="ACTIVE"), now_ts=1000.0) is False


def test_a_freshly_triggered_alert_is_suppressed():
    a = alert(status="TRIGGERED", last_triggered_time=1000.0)
    assert am.is_in_cooldown(a, now_ts=1000.0 + am.ALERT_COOLDOWN_SECONDS - 1) is True


def test_the_alert_re_arms_once_the_window_expires():
    a = alert(status="TRIGGERED", last_triggered_time=1000.0)
    assert am.is_in_cooldown(a, now_ts=1000.0 + am.ALERT_COOLDOWN_SECONDS + 1) is False


def test_a_null_last_triggered_time_does_not_crash_the_cooldown():
    a = alert(status="TRIGGERED", last_triggered_time=None)
    assert am.is_in_cooldown(a, now_ts=1000.0) is False


def test_a_suppressed_alert_is_not_even_fetched():
    """Cooldown must short-circuit before the network call, not after it."""
    calls = []

    def counting_fetch(symbol):
        calls.append(symbol)
        return bars(close=100.0), "Webull OpenAPI"

    a = alert(status="TRIGGERED", last_triggered_time=1000.0)
    am.check_alerts([a], now_ts=1000.0, fetch=counting_fetch,
                    notify=lambda *_: None, log=lambda _: None)
    assert calls == []


# =====================================================================
# One pass over a list of alerts
# =====================================================================

def test_a_triggered_alert_is_stamped_and_notified():
    sent = []
    a = alert(condition="PRICE_ABOVE", target_value=90.0)
    changed = am.check_alerts([a], now_ts=5000.0,
                              fetch=stub_fetch(bars(close=101.0)),
                              notify=lambda t, m: sent.append((t, m)),
                              log=lambda _: None)

    assert changed is True
    assert a["status"] == "TRIGGERED"
    assert a["last_triggered_time"] == 5000.0
    assert a["triggered_on"]["bar_time"] == "2026-08-02"
    assert a["triggered_on"]["source"] == "Webull OpenAPI"
    assert len(sent) == 1 and "AAPL" in sent[0][0]


def test_an_untriggered_alert_leaves_the_file_alone():
    a = alert(condition="PRICE_ABOVE", target_value=500.0)
    changed = am.check_alerts([a], now_ts=5000.0,
                              fetch=stub_fetch(bars(close=101.0)),
                              notify=lambda *_: None, log=lambda _: None)
    assert changed is False
    assert a["status"] == "ACTIVE"


def test_the_notification_reports_the_bar_it_fired_on_not_the_wall_clock():
    """A ten-month-old bar satisfying the condition must not read as 'now'."""
    sent = []
    am.check_alerts([alert(target_value=90.0)], now_ts=5000.0,
                    fetch=stub_fetch(bars(close=101.0)),
                    notify=lambda t, m: sent.append(m), log=lambda _: None)
    assert "Bar: 2026-08-02" in sent[0]


def test_the_fallback_source_is_recorded_on_the_alert():
    a = alert(target_value=90.0)
    am.check_alerts([a], now_ts=5000.0,
                    fetch=stub_fetch(bars(close=101.0), "Yahoo Finance (Fallback) (Cached)"),
                    notify=lambda *_: None, log=lambda _: None)
    assert a["triggered_on"]["source"] == "Yahoo Finance (Fallback)"


# =====================================================================
# Isolation -- one bad alert must not silence the rest
# =====================================================================

def test_a_malformed_target_value_does_not_abort_the_pass():
    """
    float(target_value) used to run outside the per-alert handler, so a single
    unparseable row aborted the whole sweep and every later alert went unchecked
    -- with only one line in stderr to say so.
    """
    bad = alert(symbol="BAD", target_value="not-a-number")
    good = alert(symbol="GOOD", target_value=90.0)
    logged = []

    changed = am.check_alerts([bad, good], now_ts=5000.0,
                              fetch=stub_fetch(bars(close=101.0)),
                              notify=lambda *_: None, log=logged.append)

    assert changed is True
    assert good["status"] == "TRIGGERED"
    assert bad["status"] == "ACTIVE"
    assert any("BAD" in line for line in logged)


def test_a_fetch_failure_on_one_symbol_does_not_abort_the_pass():
    def flaky(symbol):
        if symbol == "DEAD":
            raise RuntimeError("no source returned fresh data")
        return bars(close=101.0), "Webull OpenAPI"

    dead, good = alert(symbol="DEAD", target_value=90.0), alert(symbol="GOOD", target_value=90.0)
    logged = []
    am.check_alerts([dead, good], now_ts=5000.0, fetch=flaky,
                    notify=lambda *_: None, log=logged.append)

    assert good["status"] == "TRIGGERED"
    assert dead["status"] == "ACTIVE"
    assert any("DEAD" in line for line in logged)


def test_an_alert_with_no_symbol_is_skipped_quietly():
    a = alert(symbol="")
    assert am.check_alerts([a], now_ts=5000.0, fetch=stub_fetch(bars()),
                           notify=lambda *_: None, log=lambda _: None) is False


# =====================================================================
# Notification shell safety
# =====================================================================

def test_the_note_is_never_interpolated_into_the_powershell_script():
    """
    Notes are free text typed into the dashboard. Interpolated into a
    double-quoted PowerShell string, `$(...)` and backticks execute.
    """
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env", {})
        return type("R", (), {"returncode": 0, "stderr": b""})()

    payload = '$(Remove-Item C:\\ -Recurse); `whoami`; "quoted"'
    import subprocess
    import sys as _s
    import unittest.mock as _m

    real_run = subprocess.run
    subprocess.run = fake_run
    try:
        # Pin the platform and the notifier lookup. This assumed the host was
        # Windows, so once notifications learned to dispatch per platform it
        # returned before subprocess.run on a Linux CI runner and died on an
        # empty capture rather than on the thing it checks.
        with _m.patch.object(_s, "platform", "win32"), \
             _m.patch.object(am.shutil, "which", return_value=r"C:\powershell.exe"):
            am.send_windows_notification("title", payload)
    finally:
        subprocess.run = real_run

    script = captured["argv"][-1]
    assert "Remove-Item" not in script
    assert "whoami" not in script
    assert captured["env"]["FINMCP_ALERT_BODY"] == payload


# =====================================================================
# Persistence
# =====================================================================

def test_alerts_round_trip_through_disk(tmp_path):
    path = tmp_path / "alerts.json"
    original = [alert(symbol="MU"), alert(symbol="TSM", status="TRIGGERED")]
    am.save_alerts(original, path=str(path))
    assert am.load_alerts(path=str(path)) == original


def test_a_missing_or_empty_alerts_file_reads_as_no_alerts(tmp_path):
    assert am.load_alerts(path=str(tmp_path / "nope.json")) == []
    empty = tmp_path / "empty.json"
    empty.write_text("   ", encoding="utf-8")
    assert am.load_alerts(path=str(empty)) == []


def test_the_written_file_is_what_the_dashboard_reads_back(tmp_path):
    """The dashboard loads alerts.json straight into a DataFrame."""
    path = tmp_path / "alerts.json"
    a = alert(target_value=90.0)
    am.check_alerts([a], now_ts=5000.0, fetch=stub_fetch(bars(close=101.0)),
                    notify=lambda *_: None, log=lambda _: None)
    am.save_alerts([a], path=str(path))
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    pd.DataFrame(reloaded)          # must not raise
    assert reloaded[0]["status"] == "TRIGGERED"


def test_the_alerts_path_is_not_pinned_to_a_hardcoded_drive():
    """It was C:/mcp-servers/..., which broke the moment the repo moved."""
    assert "C:/mcp-servers" not in am.ALERTS_FILE
    assert am.ALERTS_FILE.endswith("alerts.json")


# =====================================================================
# Persistence: three writers, one file
# =====================================================================
# alerts.json is written by this manager, by the dashboard form, and by the
# set_alert MCP tool. None of them used to lock or replace atomically.

def test_the_alert_file_is_written_atomically(tmp_path):
    path = str(tmp_path / "alerts.json")
    am.save_alerts([{"symbol": "AAPL", "status": "ACTIVE"}], path=path)
    assert not os.path.exists(path + ".tmp"), "a temp file left behind is a half-written state"
    assert am.load_alerts(path)[0]["symbol"] == "AAPL"


def test_add_alert_appends_without_a_separate_read_and_write(tmp_path):
    path = str(tmp_path / "alerts.json")
    am.add_alert({"symbol": "AAPL", "status": "ACTIVE"}, path=path)
    am.add_alert({"symbol": "NVDA", "status": "ACTIVE"}, path=path)
    assert [a["symbol"] for a in am.load_alerts(path)] == ["AAPL", "NVDA"]


def test_concurrent_appends_do_not_lose_an_alert(tmp_path):
    """
    The dashboard used to read, append and write with no lock while the manager
    did the same. Whichever wrote second discarded the other's change.
    """
    import threading
    path = str(tmp_path / "alerts.json")
    am.save_alerts([], path=path)

    def add(i):
        am.add_alert({"symbol": f"S{i}", "status": "ACTIVE"}, path=path)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(am.load_alerts(path)) == 25


# =====================================================================
# Liveness: the dashboard claims the manager is running
# =====================================================================

def test_a_manager_that_never_started_is_not_reported_as_healthy():
    """
    The Alerts tab states "the alert manager monitors ... in the background".
    Without this, that sentence was printed whether or not it was true.
    """
    saved = dict(am.MANAGER_STATE)
    try:
        am.MANAGER_STATE.update(running=False, last_pass=None, passes=0)
        healthy, msg = am.manager_status()
        assert healthy is False
        assert "NOT running" in msg
    finally:
        am.MANAGER_STATE.update(saved)


def test_a_started_manager_with_no_completed_pass_is_not_yet_healthy():
    saved = dict(am.MANAGER_STATE)
    try:
        am.MANAGER_STATE.update(running=True, last_pass=None, passes=0)
        healthy, msg = am.manager_status()
        assert healthy is False and "not completed a pass" in msg
    finally:
        am.MANAGER_STATE.update(saved)


def test_a_stuck_manager_is_reported_stuck_not_running():
    """A thread alive but wedged fires no alerts, same as a dead one."""
    saved = dict(am.MANAGER_STATE)
    try:
        now = 10_000.0
        am.MANAGER_STATE.update(running=True, passes=9,
                                last_pass=now - am.CHECK_INTERVAL_SECONDS * 10)
        healthy, msg = am.manager_status(now=now)
        assert healthy is False and "stuck" in msg
    finally:
        am.MANAGER_STATE.update(saved)


def test_a_healthy_manager_reports_its_cadence():
    saved = dict(am.MANAGER_STATE)
    try:
        now = 10_000.0
        am.MANAGER_STATE.update(running=True, passes=42, last_pass=now - 5,
                                last_error=None)
        healthy, msg = am.manager_status(now=now)
        assert healthy is True and "42 checks" in msg
    finally:
        am.MANAGER_STATE.update(saved)


def test_a_loop_error_is_recorded_not_only_printed():
    """
    stderr from a headless Streamlit run goes to a log nobody reads, so a
    manager failing every pass looked exactly like one with nothing to do.
    """
    saved = dict(am.MANAGER_STATE)
    try:
        am.MANAGER_STATE.update(running=True, passes=3, last_pass=10_000.0 - 5,
                                last_error="feed unreachable")
        healthy, msg = am.manager_status(now=10_000.0)
        assert healthy is True
        assert "feed unreachable" in msg
    finally:
        am.MANAGER_STATE.update(saved)


def test_the_manager_starts_at_most_once_per_process():
    """
    The dashboard guarded this with st.session_state, which is per BROWSER
    session -- a second tab started a second manager in the same process, and
    both fired a notification for the same alert.
    """
    saved = dict(am.MANAGER_STATE)
    try:
        am.MANAGER_STATE["running"] = True      # pretend one is already up
        assert am.start_manager_once() is False
    finally:
        am.MANAGER_STATE.update(saved)


# =====================================================================
# Cross-platform notification
# =====================================================================
# This was Windows-only and named for it. On Linux or macOS every alert fired
# into nothing, with the failure caught and printed to stderr -- an alert
# manager that evaluates correctly and then tells nobody, which looks exactly
# like a market that never moved.

import sys as _sys
import unittest.mock as _mock


@pytest.mark.parametrize("platform,expected", [
    ("win32", "powershell"),
    ("darwin", "osascript"),
    ("linux", "notify-send"),
    ("freebsd13", "notify-send"),      # anything else gets the libnotify path
])
def test_each_platform_gets_a_notifier(platform, expected):
    with _mock.patch.object(_sys, "platform", platform):
        argv, _ = am._notifier_command("Title", "Body")
        assert argv[0] == expected


HOSTILE = "note with $(whoami) and `backtick` and ; rm -rf /"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_alert_text_is_an_argv_element_never_shell_text(platform):
    """
    An alert note is free text somebody typed into the dashboard. Interpolating
    it into a script would execute it; passing it as an argument displays it.
    """
    with _mock.patch.object(_sys, "platform", platform):
        argv, _ = am._notifier_command("Alert", HOSTILE)
        assert HOSTILE in argv, "the note must be its own argv element"
        assert not any(HOSTILE in part for part in argv if part is not HOSTILE)


def test_windows_keeps_the_note_out_of_the_script_body():
    with _mock.patch.object(_sys, "platform", "win32"):
        argv, env = am._notifier_command("Alert", HOSTILE)
        assert HOSTILE not in " ".join(argv)
        assert env["FINMCP_ALERT_BODY"] == HOSTILE


def test_a_missing_notifier_is_reported_rather_than_swallowed():
    """
    A headless Linux box has no notification daemon at all. Saying so beats
    failing silently, and the message names the package to install.
    """
    with _mock.patch.object(_sys, "platform", "linux"), \
         _mock.patch.object(am.shutil, "which", return_value=None):
        available, reason = am.notifier_available()
        assert available is False
        assert "libnotify" in reason
        assert "alerts still evaluate" in reason


def test_send_notification_reports_whether_it_delivered():
    """
    The old function returned None whether or not anything was shown, so no
    caller could tell the difference.
    """
    with _mock.patch.object(_sys, "platform", "linux"), \
         _mock.patch.object(am.shutil, "which", return_value=None):
        assert am.send_notification("t", "b") is False

    with _mock.patch.object(_sys, "platform", "linux"), \
         _mock.patch.object(am.shutil, "which", return_value="/usr/bin/notify-send"), \
         _mock.patch.object(am.subprocess, "run",
                            return_value=_mock.Mock(returncode=0, stderr=b"")):
        assert am.send_notification("t", "b") is True


def test_a_nonzero_exit_from_the_notifier_is_not_treated_as_delivered():
    with _mock.patch.object(_sys, "platform", "linux"), \
         _mock.patch.object(am.shutil, "which", return_value="/usr/bin/notify-send"), \
         _mock.patch.object(am.subprocess, "run",
                            return_value=_mock.Mock(returncode=1, stderr=b"no display")):
        assert am.send_notification("t", "b") is False


def test_the_old_windows_name_still_resolves():
    """Anything that imported send_windows_notification keeps working."""
    assert am.send_windows_notification is am.send_notification
