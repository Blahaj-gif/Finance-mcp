"""
Alert daemon tests.

Offline: no broker session, no network, no sleeping. The daemon's whole job is
deciding *whether* to interrupt someone, so the properties worth pinning are
the ones that make it cry wolf or stay silent when it shouldn't.
"""
import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import alert_watcher as aw
from dashboard.alert_watcher import AlertEvaluationError


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
    fired, text = aw.evaluate_condition(condition, target, {"close": close}, bars())
    assert fired is expected
    assert f"${close:.2f}" in text


@pytest.mark.parametrize("condition,target,rsi,expected", [
    ("RSI_BELOW", 30.0, 22.0, True),
    ("RSI_BELOW", 30.0, 45.0, False),
    ("RSI_ABOVE", 70.0, 81.0, True),
    ("RSI_ABOVE", 70.0, 45.0, False),
])
def test_rsi_conditions(condition, target, rsi, expected):
    fired, _ = aw.evaluate_condition(condition, target, {"rsi_14": rsi}, bars())
    assert fired is expected


def test_conditions_are_case_and_whitespace_insensitive():
    fired, _ = aw.evaluate_condition("  price_above  ", 90.0, {"close": 100.0}, bars())
    assert fired is True


def test_an_unknown_condition_raises_instead_of_guessing():
    """
    It used to fall through to a price-above check, so a typo in the condition
    became a *different alert* that still fired -- silently.
    """
    with pytest.raises(AlertEvaluationError, match="Unknown condition"):
        aw.evaluate_condition("RSI_BELO", 30.0, {"close": 100.0, "rsi_14": 20.0}, bars())


def test_every_condition_offered_by_the_dashboard_is_supported():
    """The alert form's dropdown and the daemon's vocabulary must not drift."""
    app = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "dashboard", "app.py"), encoding="utf-8").read()
    for cond in aw.CONDITIONS:
        assert cond in app, f"{cond} is not offered anywhere in the dashboard"
    offered = app.split('st.selectbox("Condition Operator", [', 1)[1].split("]", 1)[0]
    for cond in [c.strip().strip('"') for c in offered.split(",")]:
        assert cond in aw.CONDITIONS, f"dashboard offers '{cond}', daemon cannot evaluate it"


# =====================================================================
# NaN handling -- the difference between "no" and "cannot say"
# =====================================================================

def test_a_nan_indicator_raises_rather_than_reading_as_not_triggered():
    """
    Every comparison against NaN is False, so an indicator still in warm-up
    used to be indistinguishable from a condition that genuinely wasn't met.
    """
    with pytest.raises(AlertEvaluationError, match="NaN"):
        aw.evaluate_condition("RSI_BELOW", 30.0, {"rsi_14": float("nan")}, bars())


def test_a_missing_indicator_column_raises():
    with pytest.raises(AlertEvaluationError, match="missing"):
        aw.evaluate_condition("RSI_ABOVE", 70.0, {"close": 100.0}, bars())


# =====================================================================
# MACD: a cross is an event, not a state
# =====================================================================

def test_macd_bull_fires_on_the_bar_that_crosses():
    df = bars([(-0.5, 0.1), (0.4, 0.2)])          # below -> above
    fired, text = aw.evaluate_condition("MACD_CROSS_BULL", 0, df.iloc[-1].to_dict(), df)
    assert fired is True
    assert "prev" in text


def test_macd_bull_does_not_re_fire_while_merely_above():
    """
    The old check was `macd > signal` -- a state that stays true for as long as
    the trend holds, so the alert re-fired every cooldown window for days after
    the single crossing it was meant to catch.
    """
    df = bars([(0.4, 0.2), (0.6, 0.3)])           # above, and stays above
    fired, _ = aw.evaluate_condition("MACD_CROSS_BULL", 0, df.iloc[-1].to_dict(), df)
    assert fired is False


def test_macd_bear_fires_on_the_bar_that_crosses_down():
    df = bars([(0.5, 0.1), (-0.2, 0.1)])
    fired, _ = aw.evaluate_condition("MACD_CROSS_BEAR", 0, df.iloc[-1].to_dict(), df)
    assert fired is True


def test_macd_bear_does_not_re_fire_while_merely_below():
    df = bars([(-0.5, 0.1), (-0.7, 0.1)])
    fired, _ = aw.evaluate_condition("MACD_CROSS_BEAR", 0, df.iloc[-1].to_dict(), df)
    assert fired is False


def test_a_touch_then_break_counts_as_a_cross():
    """Equality on the previous bar is 'not yet above', so the break still fires."""
    df = bars([(0.2, 0.2), (0.5, 0.2)])
    fired, _ = aw.evaluate_condition("MACD_CROSS_BULL", 0, df.iloc[-1].to_dict(), df)
    assert fired is True


def test_macd_needs_two_bars():
    df = bars([(0.4, 0.2)])
    with pytest.raises(AlertEvaluationError, match="previous bar"):
        aw.evaluate_condition("MACD_CROSS_BULL", 0, df.iloc[-1].to_dict(), df)


# =====================================================================
# Cooldown
# =====================================================================

def test_an_active_alert_is_never_in_cooldown():
    assert aw.is_in_cooldown(alert(status="ACTIVE"), now_ts=1000.0) is False


def test_a_freshly_triggered_alert_is_suppressed():
    a = alert(status="TRIGGERED", last_triggered_time=1000.0)
    assert aw.is_in_cooldown(a, now_ts=1000.0 + aw.ALERT_COOLDOWN_SECONDS - 1) is True


def test_the_alert_re_arms_once_the_window_expires():
    a = alert(status="TRIGGERED", last_triggered_time=1000.0)
    assert aw.is_in_cooldown(a, now_ts=1000.0 + aw.ALERT_COOLDOWN_SECONDS + 1) is False


def test_a_null_last_triggered_time_does_not_crash_the_cooldown():
    a = alert(status="TRIGGERED", last_triggered_time=None)
    assert aw.is_in_cooldown(a, now_ts=1000.0) is False


def test_a_suppressed_alert_is_not_even_fetched():
    """Cooldown must short-circuit before the network call, not after it."""
    calls = []

    def counting_fetch(symbol):
        calls.append(symbol)
        return bars(close=100.0), "Webull OpenAPI"

    a = alert(status="TRIGGERED", last_triggered_time=1000.0)
    aw.check_alerts([a], now_ts=1000.0, fetch=counting_fetch,
                    notify=lambda *_: None, log=lambda _: None)
    assert calls == []


# =====================================================================
# One pass over a list of alerts
# =====================================================================

def test_a_triggered_alert_is_stamped_and_notified():
    sent = []
    a = alert(condition="PRICE_ABOVE", target_value=90.0)
    changed = aw.check_alerts([a], now_ts=5000.0,
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
    changed = aw.check_alerts([a], now_ts=5000.0,
                              fetch=stub_fetch(bars(close=101.0)),
                              notify=lambda *_: None, log=lambda _: None)
    assert changed is False
    assert a["status"] == "ACTIVE"


def test_the_notification_reports_the_bar_it_fired_on_not_the_wall_clock():
    """A ten-month-old bar satisfying the condition must not read as 'now'."""
    sent = []
    aw.check_alerts([alert(target_value=90.0)], now_ts=5000.0,
                    fetch=stub_fetch(bars(close=101.0)),
                    notify=lambda t, m: sent.append(m), log=lambda _: None)
    assert "Bar: 2026-08-02" in sent[0]


def test_the_fallback_source_is_recorded_on_the_alert():
    a = alert(target_value=90.0)
    aw.check_alerts([a], now_ts=5000.0,
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

    changed = aw.check_alerts([bad, good], now_ts=5000.0,
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
    aw.check_alerts([dead, good], now_ts=5000.0, fetch=flaky,
                    notify=lambda *_: None, log=logged.append)

    assert good["status"] == "TRIGGERED"
    assert dead["status"] == "ACTIVE"
    assert any("DEAD" in line for line in logged)


def test_an_alert_with_no_symbol_is_skipped_quietly():
    a = alert(symbol="")
    assert aw.check_alerts([a], now_ts=5000.0, fetch=stub_fetch(bars()),
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
        return None

    payload = '$(Remove-Item C:\\ -Recurse); `whoami`; "quoted"'
    import subprocess
    real_run = subprocess.run
    subprocess.run = fake_run
    try:
        aw.send_windows_notification("title", payload)
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
    aw.save_alerts(original, path=str(path))
    assert aw.load_alerts(path=str(path)) == original


def test_a_missing_or_empty_alerts_file_reads_as_no_alerts(tmp_path):
    assert aw.load_alerts(path=str(tmp_path / "nope.json")) == []
    empty = tmp_path / "empty.json"
    empty.write_text("   ", encoding="utf-8")
    assert aw.load_alerts(path=str(empty)) == []


def test_the_written_file_is_what_the_dashboard_reads_back(tmp_path):
    """The dashboard loads alerts.json straight into a DataFrame."""
    path = tmp_path / "alerts.json"
    a = alert(target_value=90.0)
    aw.check_alerts([a], now_ts=5000.0, fetch=stub_fetch(bars(close=101.0)),
                    notify=lambda *_: None, log=lambda _: None)
    aw.save_alerts([a], path=str(path))
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    pd.DataFrame(reloaded)          # must not raise
    assert reloaded[0]["status"] == "TRIGGERED"


def test_the_alerts_path_is_not_pinned_to_a_hardcoded_drive():
    """It was C:/mcp-servers/..., which broke the moment the repo moved."""
    assert "C:/mcp-servers" not in aw.ALERTS_FILE
    assert aw.ALERTS_FILE.endswith("alerts.json")
