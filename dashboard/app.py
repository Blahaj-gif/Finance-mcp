import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import html as _html
import sys
import os
import json
import datetime


def render_html(markup: str, target=None):
    """
    Hand Streamlit a block of hand-built HTML, unmangled and unclipped.

    Two separate Streamlit behaviours had to be routed around.

    Markdown first: `st.markdown` runs the parser even with unsafe_allow_html,
    and markdown has two rules that destroy raw HTML -- a blank line ends an
    HTML block, and four leading spaces start a code block. The signals table
    was built by concatenating f-strings, which left a whitespace-only line
    between the <tbody> and the first <tr>, so every row rendered as escaped
    source. Stripping each line to column zero and dropping blank lines keeps
    the fragment inside one HTML block.

    Sizing second: the markdown element wrapper reserves the height of a single
    line of body text, about 17px, whatever it actually contains. Anything
    taller overflows it while the wrapper still only claims 17px, so the *next*
    element starts too high and paints over the bottom -- which is why the
    masthead rule cut through the ticker. `st.html` has no such wrapper, so
    prefer it and keep the markdown path only as a fallback.
    """
    cleaned = "\n".join(line.strip() for line in markup.splitlines() if line.strip())
    sink = target or st
    if hasattr(sink, "html"):
        sink.html(cleaned)
    else:
        sink.markdown(cleaned, unsafe_allow_html=True)


def as_html_text(value) -> str:
    """Escape free text for interpolation into markup, preserving line breaks."""
    return _html.escape(str(value)).replace("\n", "<br>")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

import dashboard.webull_client as webull_client
from dashboard import theme as fm_theme
from dashboard import market_calendar
from dashboard import portfolio_history
from dashboard import econ_calendar
import indicators
import backtester
import forecaster

# Set Page Config
st.set_page_config(
    page_title="Finance MCP — Market Intelligence Dashboard",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ------------------------------------------------------------------
# Appearance
# ------------------------------------------------------------------
# Read before anything renders: the stylesheet has to be injected at the top of
# the script, but the control that changes it lives in the masthead further
# down. Streamlit reruns the whole script on a widget change, so the widget
# writes its key into session_state and the next run picks it up here.
st.session_state.setdefault("ui_theme", fm_theme.DEFAULT)
st.session_state.setdefault("ui_density", "compact")
st.session_state.setdefault("ui_overlays", "neutral")

ACTIVE_THEME = fm_theme.resolve(st.session_state["ui_theme"])
PALETTE = fm_theme.chart(ACTIVE_THEME, st.session_state["ui_overlays"])
st.markdown(fm_theme.css(ACTIVE_THEME, st.session_state["ui_density"]),
            unsafe_allow_html=True)

# Plotly defaults to box-select on drag and no wheel zoom, which makes a price
# chart a static image. Selection tools are dropped -- there is nothing on this
# chart to select -- and the wheel zooms instead.
CHART_CONFIG = {
    "scrollZoom": True,
    "displaylogo": False,
    "doubleClick": "reset",
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "toggleSpikelines"],
    "displayModeBar": True,
}

# The stylesheet lives in dashboard/theme.py -- one token block per theme
# feeding one shared sheet, injected above before anything renders.

# ------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------
# The wordmark belongs here, at the top-left, not in the masthead: the masthead
# is where the *subject* lives (which symbol, which feed, which bar), and a
# fixed product name competing with the ticker for that space made the ticker
# read as a subtitle.
IS_PAPER = webull_client.is_paper_environment()
render_html(
    f'<div class="fm-wordmark">Finance MCP'
    f'<span class="fm-env {"paper" if IS_PAPER else "live"}">'
    f'{webull_client.environment_label()}</span></div>',
    target=st.sidebar)
render_html('<div class="fm-brand" style="border-bottom:1px solid var(--fm-rule); '
            'padding-bottom:0.3rem;">Controls</div>', target=st.sidebar)

# 1. Market Settings
st.sidebar.markdown("### 1. Market Settings")
symbol = st.sidebar.text_input("Ticker Symbol", value="AAPL", max_chars=10).strip().upper()

# Timeframes. Webull and Yahoo both stop at 1H and 1D; 4H and 1Y are resampled
# from the nearest finer bar the feed will actually serve, rather than being
# offered and silently returning something else.
# `bars` is the default request per timeframe, not a cap. Intraday frames get
# more because 200 one-minute bars is half a session -- not enough to see the
# live candle forming against yesterday's structure. Measured: Webull serves
# 1200 bars in under a second and Yahoo covers 600 at every interval, so this
# costs about 0.13s of indicator time and nothing else.
MAX_BARS = 600
TIMEFRAMES = {
    "1m":  {"label": "1m",  "interval": "M1",  "resample": None,  "bars": 600},
    "5m":  {"label": "5m",  "interval": "M5",  "resample": None,  "bars": 400},
    "15m": {"label": "15m", "interval": "M15", "resample": None,  "bars": 400},
    "30m": {"label": "30m", "interval": "M30", "resample": None,  "bars": 300},
    "1H":  {"label": "1H",  "interval": "H1",  "resample": None,  "bars": 300},
    # Resampled 4:1, so ask for four times what should end up on the chart.
    "4H":  {"label": "4H",  "interval": "H1",  "resample": "4h",  "bars": 600},
    "1D":  {"label": "1D",  "interval": "D",   "resample": None,  "bars": 300},
    "1W":  {"label": "1W",  "interval": "W",   "resample": None,  "bars": 300},
    "1M":  {"label": "1M",  "interval": "M",   "resample": None,  "bars": 300},
    # "YS", not "YE": a year-end label stamps the 2026 bar 2026-12-31, a date
    # that has not happened. Every other bar in the feed is labelled at its
    # start, and a bar dated in the future reads as bad data.
    "1Y":  {"label": "1Y",  "interval": "M",   "resample": "YS",  "bars": 480},
}

INTERVAL_NAMES = {"M1": "1 min", "M5": "5 min", "M15": "15 min", "M30": "30 min",
                  "H1": "1 hour", "D": "daily", "W": "weekly", "M": "monthly"}

st.session_state.setdefault("ui_timeframe", "1D")
if st.session_state["ui_timeframe"] not in TIMEFRAMES:
    st.session_state["ui_timeframe"] = "1D"
timeframe = st.session_state["ui_timeframe"]
tf = TIMEFRAMES[timeframe]
interval = tf["interval"]

count = st.sidebar.slider(
    "Historical Bar Count", min_value=50, max_value=MAX_BARS,
    value=min(tf["bars"], MAX_BARS), step=10, key=f"bars_{timeframe}",
    help="Bars requested from the feed. Each timeframe remembers its own count. "
         "When the timeframe is resampled (4H, 1Y) this is the count *before* "
         "resampling, so the chart shows fewer.")

# 2. Indicator Configuration Expanders
st.sidebar.markdown("### 2. Indicator Parameters")

with st.sidebar.expander("Moving Averages (MA)"):
    sma_fast_len = st.number_input("Fast SMA Period", min_value=1, max_value=100, value=20)
    sma_mid_len = st.number_input("Medium SMA Period", min_value=1, max_value=300, value=50)
    sma_slow_len = st.number_input("Slow SMA Period", min_value=1, max_value=500, value=200)
    ema_fast_len = st.number_input("Fast EMA Period", min_value=1, max_value=100, value=9)
    ema_slow_len = st.number_input("Slow EMA Period", min_value=1, max_value=200, value=21)
    wma_len = st.number_input("WMA Period", min_value=1, max_value=100, value=14)
    hma_len = st.number_input("HMA Period", min_value=1, max_value=100, value=14)
    dema_len = st.number_input("DEMA Period", min_value=1, max_value=100, value=14)
    tema_len = st.number_input("TEMA Period", min_value=1, max_value=100, value=14)

with st.sidebar.expander("MACD Settings"):
    macd_fast = st.number_input("MACD Fast Period", min_value=1, max_value=100, value=12)
    macd_slow = st.number_input("MACD Slow Period", min_value=1, max_value=200, value=26)
    macd_signal = st.number_input("MACD Signal Period", min_value=1, max_value=50, value=9)

with st.sidebar.expander("RSI & Momentum"):
    rsi_len = st.number_input("RSI Period", min_value=1, max_value=100, value=14)
    stoch_k_len = st.number_input("Stochastic %K Period", min_value=1, max_value=100, value=14)
    stoch_d_len = st.number_input("Stochastic %D Period", min_value=1, max_value=50, value=3)
    cci_len = st.number_input("CCI Period", min_value=1, max_value=100, value=20)
    mfi_len = st.number_input("MFI Period", min_value=1, max_value=100, value=14)
    roc_len = st.number_input("ROC Period", min_value=1, max_value=100, value=12)
    tsi_long = st.number_input("TSI Long Period", min_value=1, max_value=100, value=25)
    tsi_short = st.number_input("TSI Short Period", min_value=1, max_value=50, value=13)

with st.sidebar.expander("Bands & Channels"):
    bb_len = st.number_input("Bollinger Bands Period", min_value=1, max_value=100, value=20)
    bb_std = st.number_input("Bollinger Std Dev multiplier", min_value=0.5, max_value=5.0, value=2.0, step=0.1)
    kc_len = st.number_input("Keltner Channels Period", min_value=1, max_value=100, value=20)
    kc_mult = st.number_input("Keltner Channels multiplier", min_value=0.5, max_value=5.0, value=1.5, step=0.1)
    dc_len = st.number_input("Donchian Channels Period", min_value=1, max_value=100, value=20)
    vwap_std = st.number_input("VWAP Band Std Dev", min_value=0.5, max_value=5.0, value=2.0, step=0.1)

# NOTE: a second "SuperTrend & Trend" expander used to sit here with the same
# two widgets as the "SuperTrend & Ichimoku" block below. Streamlit derives a
# widget's identity from its type and parameters, so two identical number_inputs
# collide on the same auto-generated ID and raise StreamlitDuplicateElementId --
# which crashed the whole dashboard on load, before a single tab rendered. The
# duplicate also shadowed st_len/st_mult, so the first pair never took effect.


with st.sidebar.expander("SuperTrend & Ichimoku"):
    st_len = st.number_input("SuperTrend ATR Period", min_value=1, max_value=50, value=10)
    st_mult = st.number_input("SuperTrend Multiplier", min_value=0.5, max_value=10.0, value=3.0, step=0.1)
    ich_conv = st.number_input("Ichimoku Conversion Period", min_value=1, max_value=50, value=9)
    ich_base = st.number_input("Ichimoku Base Period", min_value=1, max_value=100, value=26)
    ich_span_b = st.number_input("Ichimoku Span B Period", min_value=1, max_value=200, value=52)

# A sponsor card used to sit here -- directly beneath the controls that size a
# position. Removed: on a live trading surface it reads as a hobby build, and
# nothing that solicits is worth putting next to an order ticket.


# Load Data
try:
    with st.spinner(f"Fetching {timeframe} data for {symbol}..."):
        df, source = webull_client.fetch_data(symbol, interval, count)
except webull_client.YahooThrottledError as e:
    # Worth separating from a bad ticker: one is our fault and passes, the
    # other is the user's and does not.
    st.error(f"Feed throttled — not a bad symbol. {e}")
    st.stop()
except Exception as e:
    st.error(f"Error fetching data: {str(e)}")
    st.stop()


def _resample_ohlcv(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Aggregate to a coarser bar the feed does not serve natively (4H, 1Y).

    OHLCV does not aggregate uniformly: open is the first, close the last, high
    and low the extremes, volume the sum. Averaging any of them would invent a
    bar that never traded.
    """
    out = frame.copy()
    out["time"] = pd.to_datetime(out["time"])
    agg = out.set_index("time").resample(rule).agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}).dropna(subset=["open"])
    agg = agg.reset_index()
    agg["time"] = agg["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return agg[["time", "open", "high", "low", "close", "volume"]]


if tf["resample"]:
    before = len(df)
    df = _resample_ohlcv(df, tf["resample"])
    source += f" [{timeframe} resampled from {INTERVAL_NAMES[interval]}, "
    source += f"{before} to {len(df)} bars]"

# Compute Indicators Dynamically
with st.spinner("Calculating technical indicators..."):
    # Calculate indicators using updated indicators library
    res = indicators.calculate_all_indicators(df)

# ------------------------------------------------------------------
# Masthead
# ------------------------------------------------------------------
latest_bar = res.iloc[-1]
prev_bar = res.iloc[-2]

col_head, col_set = st.columns([9, 1])
with col_head:
    render_html(f"""
    <div class="fm-head">
        <span class="fm-sym">{as_html_text(symbol)}</span>
        <span class="fm-tf">{as_html_text(timeframe)}</span>
        <span class="fm-meta">{as_html_text(source)}
            · bar {as_html_text(latest_bar['time'])} · {len(res)} bars</span>
    </div>
    """)

with col_set:
    with st.popover("DISPLAY", use_container_width=True):
        render_html('<div class="fm-set-head">Display settings</div>')

        st.radio(
            "Theme",
            options=list(fm_theme.THEMES),
            format_func=lambda k: fm_theme.THEMES[k]["label"],
            key="ui_theme",
        )
        render_html(f'<div class="fm-set-note">{fm_theme.THEMES[ACTIVE_THEME]["blurb"]}</div>')

        st.radio(
            "Chart overlays",
            options=list(fm_theme.OVERLAY_STYLES),
            format_func=lambda k: k.capitalize(),
            key="ui_overlays",
            horizontal=True,
        )
        render_html(f'<div class="fm-set-note">'
                    f'{fm_theme.OVERLAY_STYLES[st.session_state["ui_overlays"]]}</div>')

        st.radio(
            "Row density",
            options=list(fm_theme.DENSITIES),
            format_func=lambda k: fm_theme.DENSITIES[k]["label"],
            key="ui_density",
            horizontal=True,
        )
        render_html('<div class="fm-set-note">Applies to every table and the metric '
                    'strip. Compact is the terminal default.</div>')

# ------------------------------------------------------------------
# Market Snapshot Strip
# ------------------------------------------------------------------

close_val = latest_bar["close"]
open_val = latest_bar["open"]
high_val = latest_bar["high"]
low_val = latest_bar["low"]
vol_val = latest_bar["volume"]
current_regime = latest_bar["regime"]
consensus_score = latest_bar["consensus_score"]

price_change = close_val - prev_bar["close"]
price_pct_change = (price_change / prev_bar["close"]) * 100

# Determine Consensus Verdict
if consensus_score >= 3:
    verdict_text, verdict_tone = "Strong Buy", "fm-up"
elif consensus_score >= 1:
    verdict_text, verdict_tone = "Buy", "fm-up"
elif consensus_score <= -3:
    verdict_text, verdict_tone = "Strong Sell", "fm-dn"
elif consensus_score <= -1:
    verdict_text, verdict_tone = "Sell", "fm-dn"
else:
    verdict_text, verdict_tone = "Neutral", "fm-neu"

# One ruled strip rather than five floating cards. The cards spent ~180px of
# vertical on five numbers; this reads in a single scan and leaves the fold for
# the chart.
price_tone = "fm-up" if price_change >= 0 else "fm-dn"
render_html(f"""
<div class="fm-strip">
    <div class="fm-cell">
        <div class="fm-k">Last</div>
        <div class="fm-v {price_tone}">{close_val:,.2f}</div>
        <div class="fm-d {price_tone}">{price_change:+,.2f} · {price_pct_change:+.2f}%</div>
    </div>
    <div class="fm-cell">
        <div class="fm-k">Volume</div>
        <div class="fm-v">{vol_val:,.0f}</div>
        <div class="fm-d">shares traded</div>
    </div>
    <div class="fm-cell">
        <div class="fm-k">Regime</div>
        <div class="fm-v word fm-am">{as_html_text(current_regime)}</div>
        <div class="fm-d">classifier</div>
    </div>
    <div class="fm-cell">
        <div class="fm-k">Consensus</div>
        <div class="fm-v word {verdict_tone}">{verdict_text}</div>
        <div class="fm-d">{consensus_score:+.1f} of ±5.0</div>
    </div>
    <div class="fm-cell">
        <div class="fm-k">Volatility</div>
        <div class="fm-v">{latest_bar["atr_14"]:,.2f}</div>
        <div class="fm-d">{latest_bar["natr_14"]:.2f}% NATR</div>
    </div>
</div>
""")


# Initialize Background Alert Watcher Thread in Streamlit if not running
import threading
import alert_watcher

if "watcher_thread_started" not in st.session_state:
    try:
        watcher_thread = threading.Thread(target=alert_watcher.run_watcher_loop, daemon=True)
        watcher_thread.start()
        st.session_state["watcher_thread_started"] = True
    except Exception:
        pass

# ------------------------------------------------------------------
# Dashboard Tab Selection
# ------------------------------------------------------------------
# Short labels, not descriptions. The descriptive names measured 1481px of tab
# strip against 1140px of container, so two tabs sat behind a scroll arrow at
# 1600px and four at 1280px -- half the app reachable only by finding an arrow.
# Every tab already states its full name in the heading inside it.
(tab_charts, tab_backtest, tab_journal, tab_signals, tab_execution,
 tab_portfolio, tab_events, tab_alerts, tab_data) = st.tabs([
    "Charts",
    "Backtest",
    "Journal",
    "Signals",
    "Execution",
    "Portfolio",
    "Events",
    "Alerts",
    "Data"
])

# Tab 1: Charts
with tab_charts:
    # Timeframe sits on the chart, where a chart's timeframe belongs -- it was
    # buried in the sidebar under indicator parameters, so switching from daily
    # to hourly meant leaving the chart to find a dropdown. Rendered as a
    # segmented strip by the stylesheet; it is a radio underneath.
    with st.container(key="fm_tf_picker"):
        st.radio(
            "Timeframe", options=list(TIMEFRAMES), key="ui_timeframe",
            horizontal=True, label_visibility="collapsed",
            format_func=lambda k: TIMEFRAMES[k]["label"],
        )

    col_opt1, col_opt2 = st.columns([1, 1])
    with col_opt1:
        selected_overlays = st.multiselect(
            "Main Chart Overlays",
            options=["SMA Fast", "SMA Medium", "SMA Slow", "EMA Fast", "EMA Slow", "Bollinger Bands", "Keltner Channels", "Donchian Channels", "VWAP", "VWAP Bands", "SuperTrend", "Ichimoku Cloud", "Pivot Points"],
            default=["EMA Fast", "EMA Slow", "Bollinger Bands", "VWAP"]
        )
    with col_opt2:
        selected_subplots = st.multiselect(
            "Separate Subplot Indicators (Limit to 3)",
            options=["RSI", "MACD", "Stochastic", "Stoch RSI", "MFI", "OBV", "CMF", "ATR", "ADX", "Ultimate Oscillator", "Awesome Oscillator", "CCI", "TSI"],
            default=["RSI", "MACD"]
        )
        
    col_fc, col_vol = st.columns([3, 1])
    with col_fc:
        show_forecast = st.checkbox("Overlay autoregressive statistical forecast (next 15 periods)", value=True)
    with col_vol:
        show_volume = st.checkbox("Volume pane", value=True)

    # ------------------------------------------------------------------
    # Real time axis
    # ------------------------------------------------------------------
    # `time` is a formatted string, so Plotly treated the axis as *categorical*:
    # every bar equally spaced regardless of the gap before it, a three-day
    # weekend indistinguishable from an overnight, and no way to zoom or pan by
    # date. Real timestamps fix the positioning; rangebreaks then remove the
    # weekend and holiday voids a real axis would otherwise open up.
    XT = pd.to_datetime(res["time"])

    def _rangebreaks(iv, times):
        """Non-trading spans to collapse, so the axis has no empty stretches."""
        if iv in ("W", "M"):
            return []                       # a weekly bar spans the weekend itself
        breaks = [dict(bounds=["sat", "mon"])]
        years = range(int(times.dt.year.min()), int(times.dt.year.max()) + 1)
        holidays = sorted({h.isoformat() for y in years
                           for h in market_calendar.market_holidays(y)})
        if holidays:
            breaks.append(dict(values=holidays))
        if iv in ("M1", "M5", "M15", "M30", "H1"):
            breaks.append(dict(bounds=[16, 9.5], pattern="hour"))   # US cash session
        return breaks

    # Generate Subplots Plotly Chart
    price_h, vol_h = 0.55, 0.12 if show_volume else 0.0
    sub_rows = len(selected_subplots)
    if show_volume:
        row_heights = [price_h, vol_h] + [(1 - price_h - vol_h) / sub_rows] * sub_rows \
            if sub_rows else [0.85, 0.15]
    else:
        row_heights = [price_h] + [(1 - price_h) / sub_rows] * sub_rows if sub_rows else [1.0]
    num_subplots = 1 + (1 if show_volume else 0) + sub_rows

    fig = make_subplots(
        rows=num_subplots,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=row_heights
    )

    # Main Candlestick Chart
    fig.add_trace(
        go.Candlestick(
            x=XT,
            open=res["open"],
            high=res["high"],
            low=res["low"],
            close=res["close"],
            name="Price",
            increasing_line_color=PALETTE["up"], increasing_fillcolor=PALETTE["up_fill"],
            decreasing_line_color=PALETTE["down"], decreasing_fillcolor=PALETTE["down_fill"]
        ),
        row=1, col=1
    )
    
    # Overlays
    if "SMA Fast" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res[f"sma_{sma_fast_len}"], mode="lines", name=f"SMA {sma_fast_len}", line=dict(color=PALETTE["overlay"](0), width=1.2)), row=1, col=1)
    if "SMA Medium" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res[f"sma_{sma_mid_len}"], mode="lines", name=f"SMA {sma_mid_len}", line=dict(color=PALETTE["overlay"](1), width=1.2)), row=1, col=1)
    if "SMA Slow" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res[f"sma_{sma_slow_len}"], mode="lines", name=f"SMA {sma_slow_len}", line=dict(color=PALETTE["overlay"](2), width=1.6)), row=1, col=1)
    if "EMA Fast" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res[f"ema_{ema_fast_len}"], mode="lines", name=f"EMA {ema_fast_len}", line=dict(color=PALETTE["overlay"](3), width=1.2, dash="dot")), row=1, col=1)
    if "EMA Slow" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res[f"ema_{ema_slow_len}"], mode="lines", name=f"EMA {ema_slow_len}", line=dict(color=PALETTE["overlay"](4), width=1.2, dash="dot")), row=1, col=1)
        
    if "Bollinger Bands" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res["bb_upper"], mode="lines", name="BB Upper", line=dict(color=PALETTE["band"], width=1, dash="dash")), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["bb_lower"], mode="lines", name="BB Lower", line=dict(color=PALETTE["band"], width=1, dash="dash"), fill="tonexty", fillcolor=PALETTE["band_faint"]), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["bb_middle"], mode="lines", name="BB Middle", line=dict(color=PALETTE["overlay"](1), width=1)), row=1, col=1)
        
    if "Keltner Channels" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res["kc_upper"], mode="lines", name="KC Upper", line=dict(color=PALETTE["band"], width=1, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["kc_lower"], mode="lines", name="KC Lower", line=dict(color=PALETTE["band"], width=1, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["kc_middle"], mode="lines", name="KC Middle", line=dict(color=PALETTE["overlay"](2), width=1)), row=1, col=1)
        
    if "Donchian Channels" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res["dc_upper"], mode="lines", name="DC Upper", line=dict(color=PALETTE["band"], width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["dc_lower"], mode="lines", name="DC Lower", line=dict(color=PALETTE["band"], width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["dc_middle"], mode="lines", name="DC Middle", line=dict(color=PALETTE["overlay"](3), width=1, dash="dash")), row=1, col=1)
        
    if "VWAP" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res["vwap"], mode="lines", name="VWAP", line=dict(color=PALETTE["accent"], width=1.6)), row=1, col=1)
        
    if "VWAP Bands" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res["vwap_upper"], mode="lines", name="VWAP Upper", line=dict(color=PALETTE["accent_band"], width=1, dash="dash")), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["vwap_lower"], mode="lines", name="VWAP Lower", line=dict(color=PALETTE["accent_band"], width=1, dash="dash")), row=1, col=1)
        
    if "SuperTrend" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res["supertrend"], mode="lines", name="SuperTrend", line=dict(color=PALETTE["overlay"](1), width=1.6)), row=1, col=1)
        
    if "Ichimoku Cloud" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res["ichimoku_conversion"], mode="lines", name="Tenkan-sen (Conversion)", line=dict(color=PALETTE["overlay"](0), width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["ichimoku_base"], mode="lines", name="Kijun-sen (Base)", line=dict(color=PALETTE["overlay"](2), width=1.2)), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["ichimoku_span_a"], mode="lines", name="Senkou Span A", line=dict(color=PALETTE["band"], width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["ichimoku_span_b"], mode="lines", name="Senkou Span B", line=dict(color=PALETTE["band"], width=1), fill="tonexty", fillcolor=PALETTE["band_faint"]), row=1, col=1)
        
    if "Pivot Points" in selected_overlays:
        fig.add_trace(go.Scatter(x=XT, y=res["pivot_pp"], mode="lines", name="PP", line=dict(color=PALETTE["dim"], width=1, dash="dash")), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["pivot_r1"], mode="lines", name="R1", line=dict(color=PALETTE["down"], width=0.8, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=XT, y=res["pivot_s1"], mode="lines", name="S1", line=dict(color=PALETTE["up"], width=0.8, dash="dot")), row=1, col=1)

    # Forecast Overlay
    if show_forecast:
        try:
            fc = forecaster.run_ar_forecast(res)
            FXT = pd.to_datetime(fc["time"])
            
            # Forecast line
            fig.add_trace(go.Scatter(x=FXT, y=fc["forecast_price"], mode="lines", name="Forecast Median", line=dict(color=PALETTE["ink"], width=1.6, dash="dash")), row=1, col=1)
            
            # 68% Confidence interval
            fig.add_trace(go.Scatter(x=FXT, y=fc["upper_68"], mode="lines", name="68% CI Upper", line=dict(width=0), showlegend=False), row=1, col=1)
            fig.add_trace(go.Scatter(x=FXT, y=fc["lower_68"], mode="lines", name="68% CI Lower", line=dict(width=0), fill="tonexty", fillcolor=PALETTE["accent_band_faint"], showlegend=False), row=1, col=1)
            
            # 95% Confidence interval
            fig.add_trace(go.Scatter(x=FXT, y=fc["upper_95"], mode="lines", name="95% CI Upper", line=dict(width=0), showlegend=False), row=1, col=1)
            fig.add_trace(go.Scatter(x=FXT, y=fc["lower_95"], mode="lines", name="95% CI Lower", line=dict(width=0), fill="tonexty", fillcolor=PALETTE["accent_band_faintest"], showlegend=False), row=1, col=1)
        except Exception as fe:
            st.warning(f"Could not calculate forecast: {str(fe)}")

    # ------------------------------------------------------------------
    # Volume pane
    # ------------------------------------------------------------------
    # The chart carried no volume at all -- the strip quoted a number the chart
    # never drew. Bars are the real traded size on their own axis, coloured by
    # the direction of the bar that produced them, so a move on conviction is
    # distinguishable from a move on nothing.
    VOLUME_ROW = 2 if show_volume else None
    if show_volume:
        vol_colors = [PALETTE["up"] if c >= o else PALETTE["down"]
                      for c, o in zip(res["close"], res["open"])]
        fig.add_trace(
            go.Bar(x=XT, y=res["volume"], name="Volume", marker_color=vol_colors,
                   marker_line_width=0, opacity=0.55, showlegend=False,
                   hovertemplate="%{y:,.0f}<extra>Volume</extra>"),
            row=VOLUME_ROW, col=1)
        fig.update_yaxes(title_text=None, tickformat=".2s", row=VOLUME_ROW, col=1)

    # Add Subplots
    first_sub_row = 3 if show_volume else 2
    for idx, sub in enumerate(selected_subplots):
        row_num = idx + first_sub_row

        if sub == "RSI":
            fig.add_trace(go.Scatter(x=XT, y=res[f"rsi_{rsi_len}"], mode="lines", name="RSI", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            fig.add_shape(type="line", x0=XT.iloc[0], y0=70, x1=XT.iloc[-1], y1=70, line=dict(color=PALETTE["down"], width=1, dash="dash"), row=row_num, col=1)
            fig.add_shape(type="line", x0=XT.iloc[0], y0=30, x1=XT.iloc[-1], y1=30, line=dict(color=PALETTE["up"], width=1, dash="dash"), row=row_num, col=1)
            
        elif sub == "MACD":
            fig.add_trace(go.Scatter(x=XT, y=res["macd"], mode="lines", name="MACD", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=XT, y=res["macd_signal"], mode="lines", name="Signal", line=dict(color=PALETTE["overlay"](1), width=1.1, dash="dot")), row=row_num, col=1)
            hist_colors = [PALETTE["up"] if val >= 0 else PALETTE["down"] for val in res["macd_hist"]]
            fig.add_trace(go.Bar(x=XT, y=res["macd_hist"], name="Histogram", marker_color=hist_colors, opacity=0.6), row=row_num, col=1)
            
        elif sub == "Stochastic":
            fig.add_trace(go.Scatter(x=XT, y=res["stoch_k"], mode="lines", name="Stoch %K", line=dict(color=PALETTE["accent"], width=1.2)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=XT, y=res["stoch_d"], mode="lines", name="Stoch %D", line=dict(color=PALETTE["overlay"](1), width=1.1, dash="dot")), row=row_num, col=1)
            
        elif sub == "Stoch RSI":
            fig.add_trace(go.Scatter(x=XT, y=res["stoch_rsi_k"], mode="lines", name="Stoch RSI %K", line=dict(color=PALETTE["accent"], width=1.2)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=XT, y=res["stoch_rsi_d"], mode="lines", name="Stoch RSI %D", line=dict(color=PALETTE["overlay"](1), width=1.1, dash="dot")), row=row_num, col=1)
            
        elif sub == "MFI":
            fig.add_trace(go.Scatter(x=XT, y=res["mfi"], mode="lines", name="MFI", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            
        elif sub == "OBV":
            fig.add_trace(go.Scatter(x=XT, y=res["obv"], mode="lines", name="OBV", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            
        elif sub == "CMF":
            fig.add_trace(go.Scatter(x=XT, y=res["cmf"], mode="lines", name="CMF", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            
        elif sub == "ATR":
            fig.add_trace(go.Scatter(x=XT, y=res["atr"], mode="lines", name="ATR", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            
        elif sub == "ADX":
            fig.add_trace(go.Scatter(x=XT, y=res["adx"], mode="lines", name="ADX", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=XT, y=res["plus_di"], mode="lines", name="+DI", line=dict(color=PALETTE["up"], width=1.0, dash="dot")), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=XT, y=res["minus_di"], mode="lines", name="-DI", line=dict(color=PALETTE["down"], width=1.0, dash="dot")), row=row_num, col=1)
            
        elif sub == "Ultimate Oscillator":
            fig.add_trace(go.Scatter(x=XT, y=res["ultimate_osc"], mode="lines", name="UO", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            
        elif sub == "Awesome Oscillator":
            ao_colors = [PALETTE["up"] if val >= 0 else PALETTE["down"] for val in res["ao"]]
            fig.add_trace(go.Bar(x=XT, y=res["ao"], name="AO", marker_color=ao_colors), row=row_num, col=1)
            
        elif sub == "CCI":
            fig.add_trace(go.Scatter(x=XT, y=res["cci"], mode="lines", name="CCI", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            
        elif sub == "TSI":
            fig.add_trace(go.Scatter(x=XT, y=res["tsi"], mode="lines", name="TSI", line=dict(color=PALETTE["accent"], width=1.4)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=XT, y=res["tsi_signal"], mode="lines", name="TSI Signal", line=dict(color=PALETTE["overlay"](1), width=1.0, dash="dash")), row=row_num, col=1)

    fig.update_layout(
        template="plotly_dark",
        height=430 + (140 * sub_rows) + (90 if show_volume else 0),
        xaxis_rangeslider_visible=False,
        # The legend ran the full width under Plotly's floating modebar, so the
        # zoom and pan icons landed on top of the last three series names. The
        # modebar goes vertical down the right edge; the legend is left-anchored
        # and reserves that column in the right margin.
        margin=dict(l=10, r=56, t=34, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0,
                    font=dict(size=10)),
        modebar=dict(orientation="v", bgcolor=PALETTE["paper"],
                     color=PALETTE["faint"], activecolor=PALETTE["accent"]),
        paper_bgcolor=PALETTE["paper"],
        plot_bgcolor=PALETTE["plot"],
        font=dict(family=PALETTE["font"], color=PALETTE["dim"], size=11),
        bargap=0.1,
        # Drag pans instead of box-zooming. A chart you cannot move across is a
        # picture of a window, not a chart.
        dragmode="pan",
        hovermode="x unified",
        hoverlabel=dict(bgcolor=PALETTE["plot"], font_size=11, font_family=PALETTE["font"]),
        # Streamlit reruns the whole script on any widget change. Without a
        # uirevision the view snapped back to full range every time -- pan
        # somewhere interesting, change any setting, lose your place. Keyed on
        # symbol and interval so it *does* reset when the subject changes.
        # (Adding or removing a pane changes the axis set, which Plotly resets
        # regardless; that is the right answer there.)
        uirevision=f"{symbol}-{interval}",
    )

    # Weekends and holidays are collapsed rather than drawn as blank stretches.
    fig.update_xaxes(showgrid=True, gridcolor=PALETTE["grid"], linecolor=PALETTE["axis"],
                     zeroline=False, rangebreaks=_rangebreaks(interval.upper(), XT),
                     showspikes=True, spikemode="across", spikethickness=1,
                     spikecolor=PALETTE["axis"], spikedash="dot")
    # fixedrange=False on every axis so vertical drag works in each pane too.
    fig.update_yaxes(showgrid=True, gridcolor=PALETTE["grid"], linecolor=PALETTE["axis"],
                     zeroline=False, fixedrange=False)

    st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)
    st.caption("Drag to pan · scroll to zoom · double-click to reset · "
               "drag an axis to scale that axis alone")

# Tab 2: Strategy Backtester
with tab_backtest:
    st.markdown("### Quantitative Consensus-Driven Backtester")
    st.markdown("Backtest a trading system driven by our **Adaptive Consensus Score** on historical data.")
    
    bt_col1, bt_col2 = st.columns([1, 3])
    
    with bt_col1:
        st.markdown("#### Strategy Rules")
        buy_thresh = st.slider("Buy Trigger (Consensus Score)", min_value=0.0, max_value=5.0, value=1.5, step=0.1)
        sell_thresh = st.slider("Sell Trigger (Consensus Score)", min_value=-5.0, max_value=0.0, value=-1.5, step=0.1)
        fee = st.number_input("Transaction Fee Ratio", min_value=0.0, max_value=0.02, value=0.0015, step=0.0001, format="%.4f")
        
        # Execute Backtest. `interval` matters: it sets the Sharpe annualisation,
        # which was previously fixed at 252 bars/year regardless of bar size.
        bt_results = backtester.run_backtest(res, consensus_col="consensus_score",
                                             buy_threshold=buy_thresh, sell_threshold=sell_thresh,
                                             transaction_fee=fee, interval=interval)
        metrics = bt_results["metrics"]

        # Metrics Display
        st.markdown("#### Performance Metrics")
        st.metric("Strategy Return", f"{metrics['total_strategy_return']:.2f}%",
                  delta=f"{metrics['total_strategy_return'] - metrics['total_asset_return']:.2f}% vs Market")
        st.metric("Buy & Hold Return", f"{metrics['total_asset_return']:.2f}%")
        st.metric("Sharpe Ratio", f"{metrics['sharpe_ratio']:.2f}",
                  help=f"Annualised for {metrics['interval']} bars, risk-free rate 0.")
        st.metric("Max Drawdown", f"{metrics['max_drawdown']:.2f}%",
                  delta=f"{metrics['max_drawdown'] - metrics['asset_max_drawdown']:.2f}% vs Buy & Hold",
                  delta_color="inverse")

        if metrics["total_trades"] == 0:
            st.warning("This strategy never traded over the selected window — the metrics above are the flat line.")
        else:
            st.metric("Trade Win Rate", f"{metrics['win_rate']:.1f}%",
                      f"{metrics['total_trades']} trades"
                      + (" (1 still open)" if metrics["open_trade"] else ""))
            st.metric("Profit Factor", f"{metrics['profit_factor']:.2f}",
                      help="Gross wins / gross losses. Above 1.0 is profitable before slippage.")
            st.metric("Time in Market", f"{metrics['exposure_pct']:.0f}%",
                      help="Share of bars holding a position. A high return on low exposure "
                           "is a different claim from the same return held throughout.")
            if metrics["avg_loss_pct"]:
                st.caption(f"Average win {metrics['avg_win_pct']:+.2f}% · "
                           f"average loss {metrics['avg_loss_pct']:+.2f}%")

        st.caption(f"{metrics['bars']} {metrics['interval']} bars. The consensus score is "
                   "undefined for the first 50 bars (indicator warm-up), so the strategy "
                   "stays flat until then.")
        
    with bt_col2:
        # Plot Equity Curve comparison
        bt_df = bt_results["df"]
        fig_bt = go.Figure()
        
        fig_bt.add_trace(go.Scatter(x=bt_df["time"], y=bt_df["cum_strategy_returns"] * 100, mode="lines", name="Consensus Strategy", line=dict(color=PALETTE["accent"], width=2.0)))
        fig_bt.add_trace(go.Scatter(x=bt_df["time"], y=bt_df["cum_asset_returns"] * 100, mode="lines", name="Buy & Hold (Market)", line=dict(color=PALETTE["dim"], width=1.2, dash="dash")))
        
        fig_bt.update_layout(
            template="plotly_dark",
            title="Cumulative Equity Performance comparison (%)",
            height=500,
            xaxis_title="Timeline",
            yaxis_title="Return (%)",
            paper_bgcolor=PALETTE["paper"],
            plot_bgcolor=PALETTE["plot"],
            font=dict(family=PALETTE["font"], color=PALETTE["dim"], size=11)
        )
        
        fig_bt.update_xaxes(showgrid=True, gridcolor=PALETTE["grid"], zeroline=False)
        fig_bt.update_yaxes(showgrid=True, gridcolor=PALETTE["grid"], zeroline=False)
        
        st.plotly_chart(fig_bt, use_container_width=True)

# Tab 3: Local Trading Journal
with tab_journal:
    st.markdown("### Local Trading Journal & Theorem Auditor")
    st.markdown("Inspect and audit trading decisions, hypotheses, and market theorems logged locally by the AI or user.")
    
    journal_file = BASE_DIR + "/dashboard/trading_journal.json"
    
    # Form for manual journal entry
    with st.expander("Log a New Journal Entry / Thesis"):
        j_col1, j_col2 = st.columns(2)
        with j_col1:
            j_symbol = st.text_input("Asset Symbol", value=symbol).upper()
            j_action = st.selectbox("Action", ["BUY", "SELL", "HOLD", "SYSTEM THESIS"])
            j_price = st.number_input("Price", value=float(close_val), format="%.2f")
        with j_col2:
            j_size = st.number_input("Size (Shares)", value=0.0, step=1.0)
            j_confidence = st.slider("Confidence Level", 1, 10, 5)
            
        j_rationale = st.text_area("Thesis Rationale / Theorem Proof")
        
        if st.button("Log Entry"):
            if j_rationale.strip() == "":
                st.error("Please enter a rationale.")
            else:
                try:
                    entries = []
                    if os.path.exists(journal_file):
                        with open(journal_file, "r", encoding="utf-8") as f:
                            content = f.read().strip()
                            if content:
                                entries = json.loads(content)
                                
                    new_e = {
                        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol": j_symbol,
                        "action": j_action,
                        "price": float(j_price),
                        "size": float(j_size),
                        "rationale": j_rationale,
                        "confidence": int(j_confidence)
                    }
                    entries.append(new_e)
                    
                    with open(journal_file, "w", encoding="utf-8") as f:
                        json.dump(entries, f, indent=4, ensure_ascii=False)
                    st.success("Successfully logged trade thesis!")
                except Exception as ex:
                    st.error(f"Failed to log entry: {str(ex)}")
                    
    # Display Existing Entries
    if os.path.exists(journal_file):
        try:
            with open(journal_file, "r", encoding="utf-8") as f:
                raw_content = f.read().strip()
                entries = json.loads(raw_content) if raw_content else []
        except Exception:
            entries = []
            
        if not entries:
            st.info("No journal entries logged yet. Ask Claude to log a thesis or use the form above!")
        else:
            # Statistics
            st.markdown(f"#### Journal Audit Summary ({len(entries)} Entries)")
            avg_conf = np.mean([e["confidence"] for e in entries])
            
            stat_col1, stat_col2 = st.columns(2)
            with stat_col1:
                st.metric("Total Logged Hypotheses", len(entries))
            with stat_col2:
                st.metric("Mean Confidence Level", f"{avg_conf:.1f} / 10")
                
            # Log List
            st.markdown("#### Journal Entries Timeline")
            for e in reversed(entries):
                action_tone = ("fm-up" if e["action"] == "BUY"
                               else "fm-dn" if e["action"] == "SELL" else "fm-neu")
                # Rationale is free text. Interpolated raw it broke the layout on
                # any '<', and a blank line in it ended the HTML block outright --
                # dumping the rest of the card as visible markup.
                rationale = as_html_text(e["rationale"])
                render_html(f"""
                <div class="fm-card">
                    <div class="fm-card-head">
                        <span>{as_html_text(e['timestamp'])}</span>
                        <span>Confidence {as_html_text(e['confidence'])}/10</span>
                    </div>
                    <div class="fm-card-title">
                        {as_html_text(e['symbol'])} · <span class="{action_tone}">{as_html_text(e['action'])}</span>
                        @ {e['price']:,.2f} × {e['size']:,.0f}
                    </div>
                    <div class="fm-card-body">{rationale}</div>
                </div>
                """)
    else:
        st.info("Journal database file not found. Logging your first entry will create it.")

# Tab 4: Signals Consensus
with tab_signals:
    st.markdown("### Dynamic Regime Classifier & Weighting Breakdown")
    st.markdown(f"Current Market Regime: **{current_regime}**")
    
    # Simple table outlining signals
    st.markdown("#### Live Indicators Signal Status")
    
    # Verdict tone is a class, not a hex literal, so it follows the active theme.
    def tone(verdict):
        return {"BUY": "fm-up", "SELL": "fm-dn"}.get(verdict, "fm-neu")

    signals = []
    # RSI
    rsi_val = latest_bar["rsi_14"]
    rsi_verdict = "BUY" if rsi_val < 30 else "SELL" if rsi_val > 70 else "NEUTRAL"
    signals.append(("Momentum · RSI", f"{rsi_val:.2f}", rsi_verdict))

    # MACD
    macd_val = latest_bar["macd"]
    macd_sig = latest_bar["macd_signal"]
    macd_verdict = "BUY" if macd_val > macd_sig else "SELL"
    signals.append(("Trend · MACD crossover", f"{macd_val:.3f} / {macd_sig:.3f}", macd_verdict))

    # SuperTrend
    st_dir = latest_bar["supertrend_dir"]
    st_verdict = "BUY" if st_dir == 1 else "SELL"
    signals.append(("Trend · SuperTrend", f"{latest_bar['supertrend']:,.2f}", st_verdict))

    # Bollinger
    bb_u = latest_bar["bb_upper"]
    bb_l = latest_bar["bb_lower"]
    bb_verdict = "BUY" if close_val <= bb_l else "SELL" if close_val >= bb_u else "NEUTRAL"
    signals.append(("Volatility · Bollinger", f"{bb_l:,.2f} – {bb_u:,.2f}", bb_verdict))

    rows = "".join(
        f'<tr><td class="label">{category}</td><td>{details}</td>'
        f'<td class="{tone(verdict)}">{verdict}</td></tr>'
        for category, details, verdict in signals
    )
    render_html(f"""
    <table class="fm-table">
        <thead><tr><th>Indicator</th><th>Condition</th><th>Verdict</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>
    """)

    # Weighting explanation
    st.markdown("#### Regime Adaptive Consensus Weighting Matrix")
    st.markdown("""
    When the system changes its classification of the market regime, the indicator weights are updated dynamically:
    
    | Market Regime | Trend Indicators (MACD, SuperTrend, EMA) | Oscillator Indicators (RSI, Stochastic, BB) | Primary Focus |
    |---|---|---|---|
    | **Trending** (Bullish/Bearish) | **80% Weight** | 20% Weight | Riding the trend, ignoring early overbought signals. |
    | **Mean-Reverting** (Range-Bound) | 20% Weight | **80% Weight** | Sniping local tops and bottoms, avoiding trend breakout traps. |
    | **Mixed / Volatility Expansion** | 50% Weight | 50% Weight | Equal balance between momentum shifts and structural levels. |
    """)

# Tab 5: Order Execution Desk
with tab_execution:
    st.markdown("### Human-In-The-Loop (HITL) Execution Desk")
    st.markdown("Review and approve orders drafted by the AI. **Claude cannot trade without your explicit physical approval here.**")

    # This is the one screen where a click spends money. Which account surface
    # that click reaches is stated here, not left to be inferred from a .env
    # file the person approving may never have opened.
    if IS_PAPER:
        st.info(f"PAPER — orders are simulated against Webull's sandbox "
                f"({webull_client.SANDBOX_ENDPOINTS[webull_client.WEBULL_REGION_ID.lower()]['api']}). "
                "Nothing here reaches a real account.")
    else:
        st.warning("LIVE — an approved order reaches the real account and spends real "
                   "money. Set `WEBULL_ENVIRONMENT=paper` in `.env` and restart to "
                   "rehearse against Webull's sandbox instead.")
    
    drafts_path = BASE_DIR + "/dashboard/order_drafts.json"
    
    import json
    import os
    
    col_ref1, col_ref2 = st.columns([4, 1])
    with col_ref2:
        if st.button("Refresh Drafts"):
            st.rerun()
            
    try:
        if os.path.exists(drafts_path):
            with open(drafts_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                drafts = json.loads(content) if content else []
        else:
            drafts = []
            
        pending_drafts = [d for d in drafts if d.get("status") == "PENDING_APPROVAL"]
        
        if not pending_drafts:
            st.info("No pending order drafts. Ask Claude to draft a trade.")
        else:
            for draft in pending_drafts:
                limit_label = f"{draft['limit_price']}" if draft["limit_price"] else "MKT"
                # Direction is the one thing that must be unmissable on an order
                # ticket, so it takes the border as well as the text colour.
                side_tone = "fm-up" if draft["action"] == "BUY" else "fm-dn"
                render_html(f"""
                <div class="fm-card" style="border-left-color: var(--fm-{'up' if draft['action'] == 'BUY' else 'down'});">
                    <div class="fm-card-head">
                        <span>Draft {as_html_text(draft['draft_id'])}</span>
                        <span>{as_html_text(draft['timestamp'])}</span>
                    </div>
                    <div class="fm-fields">
                        <div><span class="k">Action</span><strong class="{side_tone}">{as_html_text(draft['action'])}</strong></div>
                        <div><span class="k">Symbol</span><strong>{as_html_text(draft['symbol'])}</strong></div>
                        <div><span class="k">Quantity</span><strong>{as_html_text(draft['quantity'])}</strong></div>
                        <div><span class="k">Type</span><strong>{as_html_text(draft['order_type'])}</strong></div>
                        <div><span class="k">Limit</span><strong>{as_html_text(limit_label)}</strong></div>
                    </div>
                </div>
                """)
                
                preview_key = f"preview_{draft['draft_id']}"
                col_prev, col_exec = st.columns([1, 1])

                # --- Step 1: price the order with the broker (non-binding) ---
                with col_prev:
                    if st.button("1 — Preview with Webull", key=f"btn_{preview_key}", use_container_width=True):
                        with st.spinner("Asking Webull to price this order..."):
                            try:
                                from webull.trade.trade_client import TradeClient
                                import webull_client

                                trade_client = TradeClient(webull_client.get_api_client())
                                account_id = webull_client.get_primary_account_id(trade_client)
                                order = webull_client.build_order(
                                    symbol=draft["symbol"], action=draft["action"],
                                    quantity=draft["quantity"], order_type=draft["order_type"],
                                    limit_price=draft.get("limit_price"),
                                    client_order_id=draft["draft_id"],
                                )
                                quote = webull_client.preview_order(trade_client, account_id, order)
                                st.session_state[preview_key] = {"order": order, "quote": quote,
                                                                 "account_id": account_id}
                            except Exception as e:
                                st.session_state.pop(preview_key, None)
                                st.error(f"Webull refused to preview this order: {e}")

                preview = st.session_state.get(preview_key)
                if preview:
                    q = preview["quote"]
                    st.success(
                        f"Broker preview — estimated cost **${q.get('estimated_cost', '?')}**, "
                        f"fee **${q.get('estimated_transaction_fee', '?')}**"
                    )

                # --- Step 2: submit, only ever after a successful preview ---
                with col_exec:
                    if not preview:
                        st.button("2 — Approve and submit", key=f"exec_{draft['draft_id']}",
                                  use_container_width=True, disabled=True,
                                  help="Preview the order first — we never submit an order the broker has not validated.")
                    elif st.button(f"2 — APPROVE AND SUBMIT {draft['action']} {draft['quantity']} {draft['symbol']}",
                                   key=f"exec_{draft['draft_id']}", use_container_width=True):
                        with st.spinner("Submitting order to Webull..."):
                            try:
                                from webull.trade.trade_client import TradeClient
                                import webull_client

                                trade_client = TradeClient(webull_client.get_api_client())
                                res = webull_client.place_order(
                                    trade_client, preview["account_id"], preview["order"])

                                # Reached only if the broker call genuinely returned. This block
                                # used to run even when the SDK had raised, writing EXECUTED back
                                # to the draft and reporting a fill that never happened.
                                st.success(f"Order submitted. Broker response: {res}")

                                draft["status"] = "EXECUTED"
                                draft["executed_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                draft["client_order_id"] = preview["order"]["client_order_id"]
                                draft["broker_response"] = str(res)
                                with open(drafts_path, "w", encoding="utf-8") as fw:
                                    json.dump(drafts, fw, indent=2)

                                st.session_state.pop(preview_key, None)
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed to submit — the draft remains PENDING: {e}")
                            
    except Exception as e:
        st.error(f"Error reading order drafts: {str(e)}")

# Tab 6: Portfolio Analytics
with tab_portfolio:
    st.markdown("### Portfolio & Analytics")
    st.markdown("Live account balance and open positions straight from Webull.")
    
    col_port1, col_port2 = st.columns([4, 1])
    with col_port2:
        if st.button("Refresh Portfolio"):
            st.rerun()
            
    try:
        from webull.core.client import ApiClient
        from webull.trade.trade_client import TradeClient
        import webull_client
        import sys
        
        # Shared, paced, credential-redacting client — the same one the MCP
        # server uses, rather than a second ApiClient built per page render.
        trade_client = TradeClient(webull_client.get_api_client())

        # Every one of these endpoints is account-scoped. The bare calls that
        # used to be here raised TypeError, so this panel only ever showed
        # "Failed to fetch Webull account data".
        account_id = webull_client.get_primary_account_id(trade_client)
        acc_list = webull_client.unwrap(webull_client.call_webull(
            trade_client.account_v2.get_account_list))
        balances = webull_client.unwrap(webull_client.call_webull(
            trade_client.account_v2.get_account_balance, account_id))
        positions = webull_client.unwrap(webull_client.call_webull(
            trade_client.account_v2.get_account_position, account_id))

        # Balances are reported per currency; `buyingPower` never existed on
        # this API. Net liquidation is market value plus cash.
        net_liq = float(balances.get("total_market_value", 0) or 0) + \
            float(balances.get("total_cash_balance", 0) or 0)
        day_pnl = float(balances.get("total_unrealized_profit_loss", 0) or 0)
        try:
            buying_power = webull_client.get_buying_power(balances, "USD")
        except Exception:
            buying_power = 0.0
        currency = balances.get("total_asset_currency", "")
        
        # The P&L card printed the same number as both value and delta, so the
        # figure appeared twice with an arrow between them. The delta now says
        # what it is a change *against*: the cost basis.
        cost_basis = net_liq - day_pnl
        pnl_pct = (day_pnl / cost_basis * 100) if cost_basis else 0.0
        mcol1, mcol2, mcol3 = st.columns(3)
        with mcol1:
            st.metric(f"Net Liquidation ({currency or 'base'})", f"{net_liq:,.2f}")
        with mcol2:
            # Also the account base currency — it comes from the same balance
            # payload as net liquidation, not from the positions below it.
            st.metric(f"Unrealised P&L ({currency or 'base'})", f"{day_pnl:,.2f}",
                      delta=f"{pnl_pct:+.2f}% on cost", delta_color="normal")
        with mcol3:
            st.metric("Buying Power (USD)", f"{buying_power:,.2f}",
                      help="Buying power is reported per currency; this is the USD line. "
                           "Net liquidation above is in the account's base currency, "
                           "which may differ.")

        # ------------------------------------------------------------------
        # Value over time
        # ------------------------------------------------------------------
        # Two series, deliberately kept apart. The broker returns a snapshot,
        # never a history, so a P&L curve has to come from one of two places:
        # what we wrote down (true, and empty until the second day) or what we
        # can reconstruct by marking today's book back through price history
        # (available now, and NOT a P&L history -- it assumes the current
        # position was held throughout, so any trade inside the window makes it
        # a curve of something that never happened). Same unit, one axis,
        # separate names.
        book = [{"symbol": p.get("symbol"),
                 "quantity": float(p.get("quantity", 0) or 0),
                 "cost": float(p.get("cost_price", 0) or 0),
                 "last": float(p.get("last_price", 0) or 0),
                 "currency": p.get("currency") or "?"} for p in (positions or [])]

        # Positions are priced in their OWN currency, which is not the account's
        # base. This account holds USD stock inside a THB account, so labelling
        # position marks with `total_asset_currency` was wrong by the FX rate --
        # a ~35x misread. Derive the currency from the positions themselves, and
        # refuse to sum across currencies rather than adding USD to THB.
        pos_currencies = sorted({b["currency"] for b in book if b["currency"] != "?"})
        pos_ccy = pos_currencies[0] if len(pos_currencies) == 1 else None
        mixed_currency = len(pos_currencies) > 1

        try:
            portfolio_history.record_snapshot(
                net_liquidation=net_liq, gross_exposure=sum(b["last"] * b["quantity"] for b in book),
                unrealised_pnl=day_pnl, currency=currency,
                positions=[{k: b[k] for k in ("symbol", "quantity", "cost")} for b in book])
        except Exception:
            pass          # a history write must never take the live panel down

        st.markdown("#### Portfolio Value")
        if mixed_currency:
            st.warning(
                f"Positions span {', '.join(pos_currencies)}. Marks in different "
                "currencies are not summed here — there is no FX rate in this feed, "
                "and adding them would produce a number that means nothing. Per-position "
                "P&L below is still correct within each currency.")
        pnl_window = st.select_slider("History window", options=[30, 60, 90, 180, 365],
                                      value=90, format_func=lambda d: f"{d}d",
                                      key="pnl_window", label_visibility="collapsed")

        price_hist, missing = {}, []
        for b in book:
            try:
                h, _src = webull_client.fetch_data(b["symbol"], "D", min(pnl_window + 10, MAX_BARS))
                price_hist[b["symbol"]] = list(zip(h["time"].str[:10], h["close"].astype(float)))
            except Exception:
                missing.append(b["symbol"])

        # Only reconstruct when every position shares one currency (see above).
        recon, coverage = (([], {"used": [], "dropped": [b["symbol"] for b in book]})
                           if mixed_currency
                           else portfolio_history.reconstruct_series(book, price_hist))
        recorded = portfolio_history.recorded_series()

        if not recon and not recorded:
            st.info("No value history yet — no position has enough price history to "
                    "reconstruct from, and no snapshot has been recorded.")
        else:
            fig_pnl = go.Figure()
            if recon:
                recon = recon[-pnl_window:]
                rx = pd.to_datetime([r["date"] for r in recon])
                ry = [r["value"] for r in recon]
                fig_pnl.add_trace(go.Scatter(
                    x=rx, y=ry, mode="lines", name="Current book, marked back",
                    line=dict(color=PALETTE["accent"], width=2),
                    fill="tozeroy", fillcolor=PALETTE["accent_band_faintest"],
                    hovertemplate="%{x|%Y-%m-%d}<br>%{y:,.2f}<extra></extra>"))
                # The endpoint only. A number on every point is unreadable and
                # the axis plus the hover carry the rest.
                fig_pnl.add_trace(go.Scatter(
                    x=[rx[-1]], y=[ry[-1]], mode="markers+text",
                    text=[f"{ry[-1]:,.0f}"], textposition="middle left",
                    textfont=dict(color=PALETTE["ink"]),
                    marker=dict(color=PALETTE["accent"], size=8),
                    showlegend=False, hoverinfo="skip"))
            if len(recorded) > 1:
                fig_pnl.add_trace(go.Scatter(
                    x=pd.to_datetime([r["date"] for r in recorded]),
                    y=[r["net_liquidation"] for r in recorded],
                    mode="lines+markers", name="Recorded net liquidation",
                    line=dict(color=PALETTE["ink"], width=1.5),
                    marker=dict(size=5),
                    hovertemplate="%{x|%Y-%m-%d}<br>%{y:,.2f}<extra></extra>"))

            fig_pnl.update_layout(
                template="plotly_dark", height=300,
                margin=dict(l=10, r=56, t=34, b=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left",
                            x=0, font=dict(size=10)),
                paper_bgcolor=PALETTE["paper"], plot_bgcolor=PALETTE["plot"],
                font=dict(family=PALETTE["font"], color=PALETTE["dim"], size=11),
                dragmode="pan", hovermode="x unified",
                modebar=dict(orientation="v", bgcolor=PALETTE["paper"],
                             color=PALETTE["faint"], activecolor=PALETTE["accent"]),
                uirevision=f"pnl-{pnl_window}")
            fig_pnl.update_xaxes(showgrid=True, gridcolor=PALETTE["grid"],
                                 linecolor=PALETTE["axis"], zeroline=False)
            fig_pnl.update_yaxes(showgrid=True, gridcolor=PALETTE["grid"],
                                 linecolor=PALETTE["axis"], zeroline=False,
                                 title_text=f"Position marks ({pos_ccy or 'mixed'})")
            st.plotly_chart(fig_pnl, use_container_width=True, config=CHART_CONFIG)

            caveats = []
            if recon:
                caveats.append(
                    f"**Current book, marked back** prices today's holdings over past "
                    f"closes, in **{pos_ccy or 'the position currency'}** — the "
                    f"currency the positions are quoted in, which is not the account's "
                    f"base ({currency or 'unknown'}). It is not your realised P&L "
                    f"history either: it assumes this exact position was held for the "
                    f"whole window, so any buy, trim or exit inside it did not happen "
                    f"on this line.")
            if len(recorded) <= 1:
                caveats.append(
                    f"**Recorded net liquidation** is the true series and needs a "
                    f"second day to draw — {len(recorded)} snapshot(s) so far, one "
                    "written per day the dashboard is opened.")
            if coverage.get("dropped") or missing:
                gone = sorted(set(coverage.get("dropped", [])) | set(missing))
                caveats.append(f"Excluded from the reconstruction (no price history): "
                               f"{', '.join(gone)}.")
            for c in caveats:
                st.caption(c)

        # ------------------------------------------------------------------
        # Which position did it
        # ------------------------------------------------------------------
        contribs = portfolio_history.position_contributions(book)
        if contribs:
            st.markdown("#### P&L by Position")
            # Bars, not lines: this is magnitude by identity, and colouring by
            # sign is a status use (gain/loss), not a value ramp on nominal
            # categories. One axis, one unit.
            fig_c = go.Figure(go.Bar(
                x=[c["pnl"] for c in contribs],
                y=[c["symbol"] for c in contribs],
                orientation="h",
                marker_color=[PALETTE["up"] if c["pnl"] >= 0 else PALETTE["down"]
                              for c in contribs],
                marker_line_width=0,
                text=[f"{c['pnl']:+,.2f}" for c in contribs],
                textposition="outside",
                textfont=dict(color=PALETTE["ink"]),
                hovertemplate="%{y}: %{x:,.2f}<extra></extra>"))
            # Pad the value axis so a bar never runs under the modebar column,
            # and so an outside label at the extreme has somewhere to sit.
            span = max((abs(c["pnl"]) for c in contribs), default=1.0) or 1.0
            lo = min(0.0, min(c["pnl"] for c in contribs)) - span * 0.25
            hi = max(0.0, max(c["pnl"] for c in contribs)) + span * 0.25

            fig_c.update_layout(
                template="plotly_dark", height=max(150, 54 * len(contribs)),
                margin=dict(l=10, r=64, t=10, b=44), showlegend=False,
                paper_bgcolor=PALETTE["paper"], plot_bgcolor=PALETTE["plot"],
                font=dict(family=PALETTE["font"], color=PALETTE["dim"], size=11),
                # Thin marks: a saturated block spanning the pane reads loud and
                # says nothing the bar length does not already say.
                bargap=0.62,
                modebar=dict(orientation="v", bgcolor=PALETTE["paper"],
                             color=PALETTE["faint"], activecolor=PALETTE["accent"]))
            fig_c.update_xaxes(showgrid=True, gridcolor=PALETTE["grid"],
                               linecolor=PALETTE["axis"], zeroline=True,
                               zerolinecolor=PALETTE["axis"], range=[lo, hi],
                               title_text=f"Unrealised P&L ({pos_ccy or 'position currency'})")
            fig_c.update_yaxes(showgrid=False, linecolor=PALETTE["axis"],
                               autorange="reversed")
            st.plotly_chart(fig_c, use_container_width=True, config=CHART_CONFIG)

        st.markdown("#### Open Positions")
        if not positions:
            st.info("No open positions in this account.")
        else:
            # A table beats a JSON dump for something read at a glance.
            prows = []
            for p in positions:
                qty = float(p.get("quantity", 0) or 0)
                cost = float(p.get("cost_price", 0) or 0)
                last = float(p.get("last_price", 0) or 0)
                prows.append({
                    "Symbol": p.get("symbol", "—"),
                    "Quantity": qty,
                    "Cost": cost,
                    "Last": last,
                    "Value": qty * last,
                    "P&L": (last - cost) * qty,
                    "P&L %": ((last - cost) / cost * 100) if cost else 0.0,
                })
            # Column headers carry the currency. The account base is THB here
            # while the positions are quoted in USD, so unlabelled money columns
            # sat directly under a THB net-liquidation figure and read as the
            # same unit.
            money = f" ({pos_ccy})" if pos_ccy else ""
            for row, p in zip(prows, positions):
                row["Ccy"] = p.get("currency") or "?"
            # Explicit column formats: raw floats rendered as 0.3 / 908.69 / 880
            # / -3.1573 in the same table, so nothing lined up and the percent
            # column read as a price.
            st.dataframe(
                pd.DataFrame(prows), use_container_width=True, hide_index=True,
                column_config={
                    "Quantity": st.column_config.NumberColumn(format="%.4f"),
                    "Cost":     st.column_config.NumberColumn(f"Cost{money}", format="%.2f"),
                    "Last":     st.column_config.NumberColumn(f"Last{money}", format="%.2f"),
                    "Value":    st.column_config.NumberColumn(f"Value{money}", format="%.2f"),
                    "P&L":      st.column_config.NumberColumn(f"P&L{money}", format="%+.2f"),
                    "Ccy":      st.column_config.TextColumn(width="small"),
                    "P&L %":    st.column_config.NumberColumn(format="%+.2f%%"),
                })
            with st.expander("Raw broker payload"):
                st.json(positions)
            
    except Exception as e:
        st.error(f"Failed to fetch Webull account data: {str(e)}")

# Tab 7: Live Alerts & Daemon
with tab_events:
    st.markdown("### Economic Calendar & Filings")
    st.markdown("Scheduled macro releases, and SEC filings for the symbols you are watching. "
                "Both are dated at the source — nothing here is inferred.")

    ev_col1, ev_col2 = st.columns([1, 1])
    with ev_col1:
        watch_raw = st.text_input(
            "Watchlist (comma separated)", value=symbol,
            help="Filings are fetched per symbol from SEC EDGAR. The ticker in the "
                 "sidebar is the default.")
    with ev_col2:
        days_ahead = st.select_slider("Look ahead", options=[7, 14, 30, 60, 90], value=30,
                                      format_func=lambda d: f"{d}d")

    watchlist = [t.strip().upper() for t in watch_raw.split(",") if t.strip()][:8]

    # ---------------------------------------------------------------
    # Macro releases
    # ---------------------------------------------------------------
    st.markdown("#### Scheduled Macro Releases")
    with st.spinner("Reading BLS release schedules..."):
        try:
            releases, failed = econ_calendar.upcoming_releases(days_ahead=days_ahead, days_back=7)
        except Exception as e:
            releases, failed = [], [str(e)]

    if releases:
        today = datetime.date.today()
        rows = []
        for ev in releases:
            delta = (ev["date"] - today).days
            rows.append({
                "Date": ev["date"].isoformat(),
                "When": ("today" if delta == 0 else
                         f"in {delta}d" if delta > 0 else f"{-delta}d ago"),
                "Release": ev["release"],
                "Period": ev.get("period", ""),
                "Time": ev.get("time", ""),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                     column_config={"Date": st.column_config.TextColumn(width="small"),
                                    "When": st.column_config.TextColumn(width="small")})
        nxt = next((r for r in rows if not r["When"].endswith("ago")), None)
        if nxt:
            st.caption(f"Next: **{nxt['Release']}** {nxt['When']} ({nxt['Date']}).")
    else:
        st.info("No scheduled releases in this window.")
    if failed:
        # The schedules are scraped HTML pages. When one is unreachable the
        # calendar is incomplete, and an incomplete calendar that looks complete
        # is worse than one that says so.
        st.warning("Some release schedules could not be read, so this calendar is "
                   "incomplete: " + "; ".join(f"`{f}`" for f in failed[:4]))

    # ---------------------------------------------------------------
    # Filings
    # ---------------------------------------------------------------
    st.markdown("#### Recent SEC Filings")
    form_filter = st.multiselect(
        "Forms", options=["8-K", "10-Q", "10-K", "4", "13D", "13G", "144", "S-1", "DEF 14A"],
        default=["8-K", "10-Q", "10-K", "4"],
        help="Form 4 is insider dealing; 8-K is a material event, which is where "
             "earnings and guidance land.")

    if not watchlist:
        st.info("Add a ticker above to see its filings.")
    else:
        filing_rows, filing_errors = [], []
        with st.spinner(f"Fetching filings for {', '.join(watchlist)}..."):
            for tkr in watchlist:
                try:
                    for f in econ_calendar.company_filings(tkr, forms=form_filter or None, limit=12):
                        filing_rows.append({
                            "Symbol": tkr,
                            "Filed": f.get("filing_date", ""),
                            "Form": f.get("form", ""),
                            "Description": (f.get("description") or "")[:70],
                            "Accepted": (f.get("acceptance") or "")[:16].replace("T", " "),
                            "Link": f.get("url", ""),
                        })
                except Exception as e:
                    filing_errors.append(f"{tkr}: {str(e)[:90]}")

        if filing_rows:
            filing_rows.sort(key=lambda r: r["Filed"], reverse=True)
            st.dataframe(
                pd.DataFrame(filing_rows), use_container_width=True, hide_index=True,
                column_config={
                    "Link": st.column_config.LinkColumn("Filing", display_text="open"),
                    "Symbol": st.column_config.TextColumn(width="small"),
                    "Form": st.column_config.TextColumn(width="small"),
                })
            st.caption(f"{len(filing_rows)} filings across {len(watchlist)} symbol(s). "
                       "`Accepted` is the SEC's acceptance timestamp, which is what makes "
                       "this near-real-time — the filing date alone is only day-resolution.")
        elif not filing_errors:
            st.info("No filings matching those forms for these symbols.")

        if filing_errors:
            st.warning("Could not fetch filings for: " + "; ".join(f"`{e}`" for e in filing_errors))
            if any("SEC_USER_AGENT" in e for e in filing_errors):
                st.caption("The SEC's fair-access policy requires a contact address. "
                           "Set `SEC_USER_AGENT` in `.env` and restart.")

with tab_alerts:
    st.markdown("### Live Alerts & Watcher Daemon")
    st.markdown("The alert daemon monitors live price and indicator conditions in the background, firing native Windows desktop balloon notifications.")
    
    # Form to add new alert
    with st.expander("Set New Technical Alert", expanded=False):
        with st.form("alert_form"):
            col_a1, col_a2, col_a3 = st.columns([1, 1, 1])
            with col_a1:
                al_symbol = st.text_input("Alert Ticker", value=symbol).strip().upper()
            with col_a2:
                al_cond = st.selectbox("Condition Operator", ["PRICE_ABOVE", "PRICE_BELOW", "RSI_BELOW", "RSI_ABOVE", "MACD_CROSS_BULL", "MACD_CROSS_BEAR"])
            with col_a3:
                al_val = st.number_input("Target Value", value=float(close_val))
                
            al_note = st.text_input("Rationale / Note", value="Setup alert from Dashboard")
            submit_al = st.form_submit_button("Set Alert")
            
            if submit_al:
                import json
                al_path = BASE_DIR + "/dashboard/alerts.json"
                existing_al = []
                if os.path.exists(al_path):
                    with open(al_path, "r", encoding="utf-8") as f:
                        c = f.read().strip()
                        if c:
                            existing_al = json.loads(c)
                existing_al.append({
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": al_symbol,
                    "condition": al_cond,
                    "target_value": al_val,
                    "note": al_note,
                    "status": "ACTIVE"
                })
                with open(al_path, "w", encoding="utf-8") as f:
                    json.dump(existing_al, f, indent=4, ensure_ascii=False)
                st.success(f"Alert set for {al_symbol}: {al_cond} {al_val}")
                st.rerun()
                
    # List active & triggered alerts
    al_path = BASE_DIR + "/dashboard/alerts.json"
    if os.path.exists(al_path):
        with open(al_path, "r", encoding="utf-8") as f:
            c = f.read().strip()
            if c:
                all_alerts = json.loads(c)
                if all_alerts:
                    df_al = pd.DataFrame(all_alerts)
                    st.dataframe(df_al, use_container_width=True)
                else:
                    st.info("No active alerts set.")
            else:
                st.info("No active alerts set.")
    else:
        st.info("No active alerts set.")

# Tab 6: Raw Data Table
with tab_data:
    st.markdown("### Raw Historical and Calculated Indicator Columns")
    st.dataframe(res.sort_values("time", ascending=False), use_container_width=True)
