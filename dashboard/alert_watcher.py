import time
import os
import sys
import json
import datetime
import subprocess

# Ensure local path is accessible
sys.path.append("C:/mcp-servers")
import dashboard.webull_client as webull_client
import dashboard.indicators as indicators

ALERTS_FILE = "C:/mcp-servers/dashboard/alerts.json"
CHECK_INTERVAL_SECONDS = 60      # Poll once every 60 seconds
ALERT_COOLDOWN_SECONDS = 1800    # 30-minute cooldown per triggered alert to prevent spam

def send_windows_notification(title: str, message: str):
    """
    Sends a native Windows balloon/toast notification using built-in System.Windows.Forms.
    Requires zero third-party pip dependencies.
    """
    try:
        title_clean = title.replace('"', "'")
        msg_clean = message.replace('"', "'")
        ps_cmd = f'''
        [reflection.assembly]::loadwithpartialname("System.Windows.Forms") | Out-Null
        $notification = New-Object System.Windows.Forms.NotifyIcon
        $notification.Icon = [System.Drawing.SystemIcons]::Information
        $notification.BalloonTipTitle = "{title_clean}"
        $notification.BalloonTipText = "{msg_clean}"
        $notification.Visible = $True
        $notification.ShowBalloonTip(5000)
        '''
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5)
    except Exception as e:
        print(f"Failed to send Windows notification: {str(e)}", file=sys.stderr)

def evaluate_condition(condition: str, target_val: float, latest_bar: dict, df_all) -> tuple[bool, str]:
    """
    Evaluates an alert condition against live indicator data.
    Returns: (is_triggered, current_value_str)
    """
    close = float(latest_bar.get("close", 0))
    rsi = float(latest_bar.get("rsi_14", 50))
    macd = float(latest_bar.get("macd", 0))
    macd_sig = float(latest_bar.get("macd_signal", 0))
    
    cond_upper = condition.upper()
    
    if cond_upper == "PRICE_ABOVE":
        return close > target_val, f"Price: ${close:.2f} (Target > ${target_val:.2f})"
    elif cond_upper == "PRICE_BELOW":
        return close < target_val, f"Price: ${close:.2f} (Target < ${target_val:.2f})"
    elif cond_upper == "RSI_BELOW":
        return rsi < target_val, f"RSI: {rsi:.1f} (Target < {target_val:.1f})"
    elif cond_upper == "RSI_ABOVE":
        return rsi > target_val, f"RSI: {rsi:.1f} (Target > {target_val:.1f})"
    elif cond_upper == "MACD_CROSS_BULL":
        return macd > macd_sig, f"MACD: {macd:.2f} > Signal: {macd_sig:.2f}"
    elif cond_upper == "MACD_CROSS_BEAR":
        return macd < macd_sig, f"MACD: {macd:.2f} < Signal: {macd_sig:.2f}"
    else:
        # Generic price check fallback
        return close > target_val, f"Price: ${close:.2f}"

def run_watcher_loop():
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Starting Replicant Alert Watcher Daemon...")
    print(f"Polling Interval: {CHECK_INTERVAL_SECONDS}s | Alert Cooldown: {ALERT_COOLDOWN_SECONDS // 60}m")
    
    while True:
        try:
            if os.path.exists(ALERTS_FILE):
                with open(ALERTS_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    alerts = json.loads(content) if content else []
                    
                updated = False
                now_ts = time.time()
                
                for alert in alerts:
                    status = alert.get("status", "ACTIVE")
                    last_triggered = alert.get("last_triggered_time", 0)
                    
                    # Respect 30-minute cooldown for triggered alerts
                    if status == "TRIGGERED" and (now_ts - last_triggered) < ALERT_COOLDOWN_SECONDS:
                        continue
                        
                    symbol = alert.get("symbol", "").upper()
                    condition = alert.get("condition", "").upper()
                    target_val = float(alert.get("target_value", 0))
                    note = alert.get("note", "")
                    
                    if not symbol:
                        continue
                        
                    try:
                        # Fetch data (uses 60s in-memory cache to prevent over-pinging)
                        df, source = webull_client.fetch_data(symbol, "D", 100)
                        res_df = indicators.calculate_all_indicators(df)
                        latest_bar = res_df.iloc[-1].to_dict()
                        
                        is_triggered, val_str = evaluate_condition(condition, target_val, latest_bar, res_df)
                        
                        if is_triggered:
                            # Which bar actually satisfied the condition. Without this an
                            # alert reads as "fired now" regardless of the bar's age --
                            # these were firing on ten-month-old bars and looked current.
                            bar_time = str(latest_bar.get("time", "unknown"))
                            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ALERT TRIGGERED for {symbol}: "
                                  f"{condition} {target_val} (bar {bar_time}, {source})")

                            title = f"🚨 TRADE ALERT: {symbol} ({condition})"
                            msg = f"{val_str}\nBar: {bar_time}\nNote: {note}\nSource: {source}"
                            send_windows_notification(title, msg)

                            alert["status"] = "TRIGGERED"
                            alert["last_triggered_time"] = now_ts
                            alert["last_triggered_str"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            alert["triggered_on"] = {
                                "bar_time": bar_time,
                                "source": webull_client.base_source(source),
                                "value": val_str,
                            }
                            updated = True
                    except Exception as ex:
                        print(f"Error checking alert for {symbol}: {str(ex)}", file=sys.stderr)
                        
                if updated:
                    with open(ALERTS_FILE, "w", encoding="utf-8") as f:
                        json.dump(alerts, f, indent=4, ensure_ascii=False)
                        
        except Exception as e:
            print(f"Error in watcher loop: {str(e)}", file=sys.stderr)
            
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    run_watcher_loop()
