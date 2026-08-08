"""
Background alert daemon.

The loop is deliberately thin: everything that decides *whether* an alert fires
lives in `evaluate_condition` / `check_alerts`, which take their data and their
clock as arguments so the whole thing is testable without a network, a broker
session, or a 60-second wait.
"""
import time
import os
import sys
import json
import math
import datetime
import subprocess

# Ensure local path is accessible
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard.webull_client as webull_client
import dashboard.indicators as indicators

ALERTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.json")
CHECK_INTERVAL_SECONDS = 60      # Poll once every 60 seconds
ALERT_COOLDOWN_SECONDS = 1800    # 30-minute cooldown per triggered alert to prevent spam

CONDITIONS = ("PRICE_ABOVE", "PRICE_BELOW", "RSI_BELOW", "RSI_ABOVE",
              "MACD_CROSS_BULL", "MACD_CROSS_BEAR")


class AlertEvaluationError(ValueError):
    """The alert cannot be evaluated -- unknown condition, or missing/NaN input."""


def send_windows_notification(title: str, message: str):
    """
    Sends a native Windows balloon/toast notification using built-in System.Windows.Forms.
    Requires zero third-party pip dependencies.

    Title and body are handed over as environment variables rather than
    interpolated into the script text. Alert notes are free text typed into the
    dashboard, and a note containing `$(...)` or a backtick would otherwise be
    executed by PowerShell rather than displayed.
    """
    ps_cmd = '''
    [reflection.assembly]::loadwithpartialname("System.Windows.Forms") | Out-Null
    $notification = New-Object System.Windows.Forms.NotifyIcon
    $notification.Icon = [System.Drawing.SystemIcons]::Information
    $notification.BalloonTipTitle = $env:FINMCP_ALERT_TITLE
    $notification.BalloonTipText = $env:FINMCP_ALERT_BODY
    $notification.Visible = $True
    $notification.ShowBalloonTip(5000)
    '''
    try:
        env = dict(os.environ)
        env["FINMCP_ALERT_TITLE"] = str(title)
        env["FINMCP_ALERT_BODY"] = str(message)
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                       capture_output=True, timeout=5, env=env)
    except Exception as e:
        print(f"Failed to send Windows notification: {str(e)}", file=sys.stderr)


def _finite(value, field: str) -> float:
    """
    Coerce an indicator to a float, refusing NaN.

    An indicator still in its warm-up window is NaN, and every comparison
    against NaN is False -- so a broken feed reads as "condition not met"
    rather than "cannot evaluate". Those are not the same answer.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise AlertEvaluationError(f"{field} is missing from the indicator frame")
    if not math.isfinite(out):
        raise AlertEvaluationError(f"{field} is not yet available (NaN) -- "
                                   f"not enough bars for warm-up")
    return out


def evaluate_condition(condition: str, target_val: float, latest_bar: dict,
                       df_all=None) -> tuple[bool, str]:
    """
    Evaluate an alert condition against live indicator data.

    Returns: (is_triggered, current_value_str)
    Raises AlertEvaluationError if the condition is unknown or its inputs are
    unavailable -- an unrecognised condition used to fall through to a plain
    price-above check, so a typo silently became a different alert.
    """
    cond = (condition or "").strip().upper()

    if cond in ("PRICE_ABOVE", "PRICE_BELOW"):
        close = _finite(latest_bar.get("close"), "close")
        if cond == "PRICE_ABOVE":
            return close > target_val, f"Price: ${close:.2f} (Target > ${target_val:.2f})"
        return close < target_val, f"Price: ${close:.2f} (Target < ${target_val:.2f})"

    if cond in ("RSI_BELOW", "RSI_ABOVE"):
        rsi = _finite(latest_bar.get("rsi_14"), "rsi_14")
        if cond == "RSI_BELOW":
            return rsi < target_val, f"RSI: {rsi:.1f} (Target < {target_val:.1f})"
        return rsi > target_val, f"RSI: {rsi:.1f} (Target > {target_val:.1f})"

    if cond in ("MACD_CROSS_BULL", "MACD_CROSS_BEAR"):
        macd = _finite(latest_bar.get("macd"), "macd")
        macd_sig = _finite(latest_bar.get("macd_signal"), "macd_signal")
        prev = _previous_bar(df_all)
        if prev is None:
            raise AlertEvaluationError(
                "MACD cross needs the previous bar; only one bar was supplied")
        prev_macd = _finite(prev.get("macd"), "macd (previous bar)")
        prev_sig = _finite(prev.get("macd_signal"), "macd_signal (previous bar)")

        # A cross is a change of side between two consecutive bars. The old
        # check was `macd > signal`, which is a *state*: it stayed true for as
        # long as the trend held, so the alert re-fired every cooldown window
        # for days after the one crossing it was meant to catch.
        if cond == "MACD_CROSS_BULL":
            fired = prev_macd <= prev_sig and macd > macd_sig
            arrow = ">"
        else:
            fired = prev_macd >= prev_sig and macd < macd_sig
            arrow = "<"
        return fired, (f"MACD: {macd:.2f} {arrow} Signal: {macd_sig:.2f} "
                       f"(prev {prev_macd:.2f} vs {prev_sig:.2f})")

    raise AlertEvaluationError(
        f"Unknown condition '{condition}'. Supported: {', '.join(CONDITIONS)}")


def _previous_bar(df_all):
    """The bar before the latest one, as a dict, or None if there isn't one."""
    if df_all is None or len(df_all) < 2:
        return None
    return df_all.iloc[-2].to_dict()


def is_in_cooldown(alert: dict, now_ts: float) -> bool:
    """True while a triggered alert is still inside its no-repeat window."""
    if alert.get("status") != "TRIGGERED":
        return False
    last = alert.get("last_triggered_time")
    if last in (None, ""):
        # Hand-edited or pre-upgrade file: TRIGGERED with no timestamp. Treat it
        # as re-armed rather than suppressed forever.
        return False
    return (now_ts - float(last)) < ALERT_COOLDOWN_SECONDS


def _default_fetch(symbol: str):
    df, source = webull_client.fetch_data(symbol, "D", 100)
    return indicators.calculate_all_indicators(df), source


def check_alerts(alerts, now_ts, fetch=_default_fetch,
                 notify=send_windows_notification, log=None) -> bool:
    """
    Evaluate every alert once. Mutates triggered alerts in place.

    Returns True if any alert changed and the file should be rewritten.

    Each alert is isolated: a malformed `target_value` used to raise outside the
    per-alert handler, which aborted the whole pass -- one bad row silently
    stopped every other alert from being checked.
    """
    log = log or (lambda msg: print(msg, file=sys.stderr))
    updated = False

    for alert in alerts:
        try:
            if is_in_cooldown(alert, now_ts):
                continue

            symbol = str(alert.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            condition = str(alert.get("condition", "")).strip().upper()
            target_val = float(alert.get("target_value", 0))
            note = alert.get("note", "")

            res_df, source = fetch(symbol)
            latest_bar = res_df.iloc[-1].to_dict()

            is_triggered, val_str = evaluate_condition(condition, target_val,
                                                       latest_bar, res_df)
            if not is_triggered:
                continue

            # Which bar actually satisfied the condition. Without this an
            # alert reads as "fired now" regardless of the bar's age --
            # these were firing on ten-month-old bars and looked current.
            bar_time = str(latest_bar.get("time", "unknown"))
            stamp = datetime.datetime.now()
            log(f"[{stamp.strftime('%H:%M:%S')}] ALERT TRIGGERED for {symbol}: "
                f"{condition} {target_val} (bar {bar_time}, {source})")

            notify(f"TRADE ALERT: {symbol} ({condition})",
                   f"{val_str}\nBar: {bar_time}\nNote: {note}\nSource: {source}")

            alert["status"] = "TRIGGERED"
            alert["last_triggered_time"] = now_ts
            alert["last_triggered_str"] = stamp.strftime("%Y-%m-%d %H:%M:%S")
            alert["triggered_on"] = {
                "bar_time": bar_time,
                "source": webull_client.base_source(source),
                "value": val_str,
            }
            updated = True
        except Exception as ex:
            log(f"Error checking alert for {alert.get('symbol', '?')}: {ex}")

    return updated


def load_alerts(path=None) -> list:
    path = path or ALERTS_FILE
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    return json.loads(content) if content else []


def save_alerts(alerts, path=None):
    path = path or ALERTS_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(alerts, f, indent=4, ensure_ascii=False)


def run_watcher_loop():
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Starting Finance MCP Alert Watcher Daemon...")
    print(f"Polling Interval: {CHECK_INTERVAL_SECONDS}s | Alert Cooldown: {ALERT_COOLDOWN_SECONDS // 60}m")

    while True:
        try:
            alerts = load_alerts()
            if alerts and check_alerts(alerts, time.time()):
                save_alerts(alerts)
        except Exception as e:
            print(f"Error in watcher loop: {str(e)}", file=sys.stderr)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_watcher_loop()
