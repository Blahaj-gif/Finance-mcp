import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import sys
import os
import json
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

import dashboard.webull_client as webull_client
import indicators
import backtester
import forecaster

# Set Page Config
st.set_page_config(
    page_title="Webull Market Intelligence & Quantitative Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for Premium Sleek Dark Mode Look
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Outfit', sans-serif;
    }
    
    .stApp {
        background: radial-gradient(circle at 10% 20%, rgb(15, 23, 42) 0%, rgb(9, 15, 29) 90%);
        color: #E2E8F0;
    }
    
    /* Elegant Header */
    .dashboard-title {
        background: linear-gradient(135deg, #38BDF8 0%, #34D399 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 800;
        font-size: 2.8rem;
        margin-bottom: 0.2rem;
    }
    .dashboard-subtitle {
        color: #94A3B8;
        font-size: 1.1rem;
        font-weight: 300;
        margin-bottom: 2rem;
    }
    
    /* Card Glassmorphism */
    .metric-card {
        background: rgba(30, 41, 59, 0.45);
        backdrop-filter: blur(12px);
        border: 1px solid rgba(255, 255, 255, 0.05);
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.2);
        margin-bottom: 1rem;
        transition: transform 0.2s ease, border-color 0.2s ease;
    }
    .metric-card:hover {
        transform: translateY(-2px);
        border-color: rgba(56, 189, 248, 0.3);
    }
    
    /* Styling Streamlit widgets to match dark mode */
    div[data-testid="stMetricValue"] {
        font-size: 1.8rem !important;
        font-weight: 600 !important;
    }
    
    /* Status pills */
    .verdict-pill {
        display: inline-block;
        padding: 6px 16px;
        border-radius: 9999px;
        font-weight: 600;
        font-size: 0.9rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    .pill-strong-buy { background: rgba(16, 185, 129, 0.2); color: #34D399; border: 1px solid #10B981; }
    .pill-buy { background: rgba(52, 211, 153, 0.1); color: #6EE7B7; border: 1px solid #34D399; }
    .pill-neutral { background: rgba(148, 163, 184, 0.2); color: #CBD5E1; border: 1px solid #94A3B8; }
    .pill-sell { background: rgba(239, 68, 68, 0.2); color: #FCA5A5; border: 1px solid #EF4444; }
    .pill-strong-sell { background: rgba(220, 38, 38, 0.3); color: #F87171; border: 1px solid #DC2626; }
    
    /* Journal layout styling */
    .journal-entry {
        background: rgba(30, 41, 59, 0.3);
        border: 1px solid rgba(255, 255, 255, 0.03);
        border-left: 4px solid #38BDF8;
        border-radius: 8px;
        padding: 15px;
        margin-bottom: 12px;
    }
    .journal-header {
        display: flex;
        justify-content: space-between;
        font-size: 0.85rem;
        color: #94A3B8;
        margin-bottom: 8px;
    }
    .journal-title {
        font-weight: 600;
        font-size: 1.05rem;
        color: #F8FAFC;
    }
</style>
""", unsafe_allow_html=True)

# App Sidebar
st.sidebar.markdown("<h2 style='font-weight:800; color:#38BDF8; margin-bottom: 0px;'>Controls</h2>", unsafe_allow_html=True)

# 1. Market Settings
st.sidebar.markdown("### 1. Market Settings")
symbol = st.sidebar.text_input("Ticker Symbol", value="AAPL", max_chars=10).strip().upper()
interval = st.sidebar.selectbox(
    "Timespan / Interval",
    options=["D", "M1", "M5", "M15", "M30", "H1", "W", "M"],
    format_func=lambda x: {"D": "Daily", "M1": "1 Minute", "M5": "5 Minutes", "M15": "15 Minutes", "M30": "30 Minutes", "H1": "1 Hour", "W": "Weekly", "M": "Monthly"}[x]
)
count = st.sidebar.slider("Historical Bar Count", min_value=50, max_value=500, value=200, step=10)

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

st.sidebar.markdown("---")
st.sidebar.markdown(
    """
    <div style='background: rgba(30, 41, 59, 0.4); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 12px; padding: 15px; text-align: center; margin-top: 15px;'>
        <h4 style='color: #38BDF8; margin-top:0; margin-bottom: 8px;'>☕ Support Open Source</h4>
        <p style='font-size: 0.85rem; color: #94A3B8; margin-bottom: 12px;'>Enjoying this AI Financial Intelligence package? Support future quantitative updates!</p>
        <a href='https://github.com/sponsors' target='_blank' style='background: linear-gradient(135deg, #38BDF8 0%, #34D399 100%); color: #0F172A; font-weight: bold; padding: 8px 16px; border-radius: 8px; text-decoration: none; display: inline-block; font-size: 0.9rem;'>
            ☕ Buy Me a Coffee / Sponsor
        </a>
    </div>
    """,
    unsafe_allow_html=True
)


# Load Data
try:
    with st.spinner(f"Fetching market data for {symbol}..."):
        df, source = webull_client.fetch_data(symbol, interval, count)
except Exception as e:
    st.error(f"Error fetching data: {str(e)}")
    st.stop()

# Compute Indicators Dynamically
with st.spinner("Calculating technical indicators..."):
    # Calculate indicators using updated indicators library
    res = indicators.calculate_all_indicators(df)

# Header Section
st.markdown(f"<div class='dashboard-title'>📈 Webull Market Intelligence</div>", unsafe_allow_html=True)
st.markdown(f"<div class='dashboard-subtitle'>Analyzing {symbol} • Source: {source} • Interval: {interval}</div>", unsafe_allow_html=True)

# ------------------------------------------------------------------
# Market Snapshot Metrics Cards
# ------------------------------------------------------------------
latest_bar = res.iloc[-1]
prev_bar = res.iloc[-2]

close_val = latest_bar["close"]
open_val = latest_bar["open"]
high_val = latest_bar["high"]
low_val = latest_bar["low"]
vol_val = latest_bar["volume"]
current_regime = latest_bar["regime"]
consensus_score = latest_bar["consensus_score"]

price_change = close_val - prev_bar["close"]
price_pct_change = (price_change / prev_bar["close"]) * 100

col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    st.markdown(f"""
    <div class="metric-card">
        <div style="font-size:0.85rem; color:#94A3B8; font-weight:600; text-transform:uppercase;">Last Price</div>
        <div style="font-size:1.8rem; font-weight:700; color:{'#10B981' if price_change >= 0 else '#EF4444'}; margin-top:5px;">
            ${close_val:.2f}
        </div>
        <div style="font-size:0.85rem; color:{'#34D399' if price_change >= 0 else '#FCA5A5'}; font-weight:600; margin-top:2px;">
            {'+' if price_change >= 0 else ''}{price_change:.2f} ({price_pct_change:.2f}%)
        </div>
    </div>
    """, unsafe_allow_html=True)

with col2:
    st.markdown(f"""
    <div class="metric-card">
        <div style="font-size:0.85rem; color:#94A3B8; font-weight:600; text-transform:uppercase;">Volume</div>
        <div style="font-size:1.8rem; font-weight:700; color:#E2E8F0; margin-top:5px;">
            {vol_val:,.0f}
        </div>
        <div style="font-size:0.85rem; color:#64748B; font-weight:600; margin-top:2px;">
            Shares Traded
        </div>
    </div>
    """, unsafe_allow_html=True)

with col3:
    st.markdown(f"""
    <div class="metric-card">
        <div style="font-size:0.85rem; color:#94A3B8; font-weight:600; text-transform:uppercase;">Market Regime</div>
        <div style="font-size:1.4rem; font-weight:700; color:#F59E0B; margin-top:10px; line-height: 1.2;">
            {current_regime}
        </div>
        <div style="font-size:0.85rem; color:#64748B; font-weight:600; margin-top:5px;">
            Regime Classifier
        </div>
    </div>
    """, unsafe_allow_html=True)

# Determine Consensus Verdict
if consensus_score >= 3:
    verdict_text = "Strong Buy"
    verdict_class = "pill-strong-buy"
elif consensus_score >= 1:
    verdict_text = "Buy"
    verdict_class = "pill-buy"
elif consensus_score <= -3:
    verdict_text = "Strong Sell"
    verdict_class = "pill-strong-sell"
elif consensus_score <= -1:
    verdict_text = "Sell"
    verdict_class = "pill-sell"
else:
    verdict_text = "Neutral"
    verdict_class = "pill-neutral"

with col4:
    st.markdown(f"""
    <div class="metric-card">
        <div style="font-size:0.85rem; color:#94A3B8; font-weight:600; text-transform:uppercase;">Adaptive Consensus</div>
        <div style="margin-top:10px;">
            <span class="verdict-pill {verdict_class}">{verdict_text}</span>
        </div>
        <div style="font-size:0.85rem; color:#64748B; font-weight:600; margin-top:12px;">
            Score: {consensus_score:+.1f} / +5.0
        </div>
    </div>
    """, unsafe_allow_html=True)

with col5:
    st.markdown(f"""
    <div class="metric-card">
        <div style="font-size:0.85rem; color:#94A3B8; font-weight:600; text-transform:uppercase;">Volatility (ATR)</div>
        <div style="font-size:1.8rem; font-weight:700; color:#E2E8F0; margin-top:5px;">
            {latest_bar["atr_14"]:.2f}
        </div>
        <div style="font-size:0.85rem; color:#94A3B8; font-weight:600; margin-top:2px;">
            NATR: {latest_bar["natr_14"]:.2f}%
        </div>
    </div>
    """, unsafe_allow_html=True)


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
tab_charts, tab_backtest, tab_journal, tab_signals, tab_execution, tab_portfolio, tab_alerts, tab_data = st.tabs([
    "📊 Technical & Forecast Charts", 
    "📈 Quantitative Backtester", 
    "📝 Local Trading Journal",
    "⚡ Adaptive Consensus Signal Breakdown", 
    "🛒 Order Execution Desk",
    "💼 Portfolio Analytics",
    "🚨 Live Alerts & Watcher Daemon",
    "📋 Calculated Values Table"
])

# Tab 1: Charts
with tab_charts:
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
        
    show_forecast = st.checkbox("Overlay Autoregressive Statistical Forecast (Next 15 Periods)", value=True)
    
    # Generate Subplots Plotly Chart
    num_subplots = 1 + len(selected_subplots)
    row_heights = [0.55] + [0.45 / len(selected_subplots)] * len(selected_subplots) if selected_subplots else [1.0]
    
    fig = make_subplots(
        rows=num_subplots,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights
    )
    
    # Main Candlestick Chart
    fig.add_trace(
        go.Candlestick(
            x=res["time"],
            open=res["open"],
            high=res["high"],
            low=res["low"],
            close=res["close"],
            name="Price",
            increasing_line_color="#10B981", increasing_fillcolor="rgba(16,185,129,0.3)",
            decreasing_line_color="#EF4444", decreasing_fillcolor="rgba(239,68,68,0.3)"
        ),
        row=1, col=1
    )
    
    # Overlays
    if "SMA Fast" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res[f"sma_{sma_fast_len}"], mode="lines", name=f"SMA {sma_fast_len}", line=dict(color="#38BDF8", width=1.5)), row=1, col=1)
    if "SMA Medium" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res[f"sma_{sma_mid_len}"], mode="lines", name=f"SMA {sma_mid_len}", line=dict(color="#60A5FA", width=1.5)), row=1, col=1)
    if "SMA Slow" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res[f"sma_{sma_slow_len}"], mode="lines", name=f"SMA {sma_slow_len}", line=dict(color="#818CF8", width=2.0)), row=1, col=1)
    if "EMA Fast" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res[f"ema_{ema_fast_len}"], mode="lines", name=f"EMA {ema_fast_len}", line=dict(color="#F472B6", width=1.5)), row=1, col=1)
    if "EMA Slow" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res[f"ema_{ema_slow_len}"], mode="lines", name=f"EMA {ema_slow_len}", line=dict(color="#EC4899", width=1.5)), row=1, col=1)
        
    if "Bollinger Bands" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res["bb_upper"], mode="lines", name="BB Upper", line=dict(color="rgba(148, 163, 184, 0.4)", width=1, dash="dash")), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["bb_lower"], mode="lines", name="BB Lower", line=dict(color="rgba(148, 163, 184, 0.4)", width=1, dash="dash"), fill="tonexty", fillcolor="rgba(148, 163, 184, 0.05)"), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["bb_middle"], mode="lines", name="BB Middle", line=dict(color="rgba(148, 163, 184, 0.6)", width=1)), row=1, col=1)
        
    if "Keltner Channels" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res["kc_upper"], mode="lines", name="KC Upper", line=dict(color="rgba(245, 158, 11, 0.4)", width=1, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["kc_lower"], mode="lines", name="KC Lower", line=dict(color="rgba(245, 158, 11, 0.4)", width=1, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["kc_middle"], mode="lines", name="KC Middle", line=dict(color="rgba(245, 158, 11, 0.6)", width=1)), row=1, col=1)
        
    if "Donchian Channels" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res["dc_upper"], mode="lines", name="DC Upper", line=dict(color="rgba(34, 197, 94, 0.3)", width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["dc_lower"], mode="lines", name="DC Lower", line=dict(color="rgba(34, 197, 94, 0.3)", width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["dc_middle"], mode="lines", name="DC Middle", line=dict(color="rgba(34, 197, 94, 0.5)", width=1, dash="dash")), row=1, col=1)
        
    if "VWAP" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res["vwap"], mode="lines", name="VWAP", line=dict(color="#FBBF24", width=2.0)), row=1, col=1)
        
    if "VWAP Bands" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res["vwap_upper"], mode="lines", name="VWAP Upper", line=dict(color="rgba(251, 191, 36, 0.3)", width=1, dash="dash")), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["vwap_lower"], mode="lines", name="VWAP Lower", line=dict(color="rgba(251, 191, 36, 0.3)", width=1, dash="dash")), row=1, col=1)
        
    if "SuperTrend" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res["supertrend"], mode="lines", name="SuperTrend", line=dict(color="#10B981", width=2.0)), row=1, col=1)
        
    if "Ichimoku Cloud" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res["ichimoku_conversion"], mode="lines", name="Tenkan-sen (Conversion)", line=dict(color="#60A5FA", width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["ichimoku_base"], mode="lines", name="Kijun-sen (Base)", line=dict(color="#F472B6", width=1.2)), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["ichimoku_span_a"], mode="lines", name="Senkou Span A", line=dict(color="rgba(16, 185, 129, 0.3)", width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["ichimoku_span_b"], mode="lines", name="Senkou Span B", line=dict(color="rgba(239, 68, 68, 0.3)", width=1), fill="tonexty", fillcolor="rgba(16, 185, 129, 0.05)"), row=1, col=1)
        
    if "Pivot Points" in selected_overlays:
        fig.add_trace(go.Scatter(x=res["time"], y=res["pivot_pp"], mode="lines", name="PP", line=dict(color="#94A3B8", width=1, dash="dash")), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["pivot_r1"], mode="lines", name="R1", line=dict(color="#EF4444", width=0.8, dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=res["time"], y=res["pivot_s1"], mode="lines", name="S1", line=dict(color="#10B981", width=0.8, dash="dot")), row=1, col=1)

    # Forecast Overlay
    if show_forecast:
        try:
            fc = forecaster.run_ar_forecast(res)
            
            # Forecast line
            fig.add_trace(go.Scatter(x=fc["time"], y=fc["forecast_price"], mode="lines", name="Forecast Median", line=dict(color="#38BDF8", width=2)), row=1, col=1)
            
            # 68% Confidence interval
            fig.add_trace(go.Scatter(x=fc["time"], y=fc["upper_68"], mode="lines", name="68% CI Upper", line=dict(width=0), showlegend=False), row=1, col=1)
            fig.add_trace(go.Scatter(x=fc["time"], y=fc["lower_68"], mode="lines", name="68% CI Lower", line=dict(width=0), fill="tonexty", fillcolor="rgba(56, 189, 248, 0.15)", showlegend=False), row=1, col=1)
            
            # 95% Confidence interval
            fig.add_trace(go.Scatter(x=fc["time"], y=fc["upper_95"], mode="lines", name="95% CI Upper", line=dict(width=0), showlegend=False), row=1, col=1)
            fig.add_trace(go.Scatter(x=fc["time"], y=fc["lower_95"], mode="lines", name="95% CI Lower", line=dict(width=0), fill="tonexty", fillcolor="rgba(56, 189, 248, 0.05)", showlegend=False), row=1, col=1)
        except Exception as fe:
            st.warning(f"Could not calculate forecast: {str(fe)}")

    # Add Subplots
    for idx, sub in enumerate(selected_subplots):
        row_num = idx + 2
        
        if sub == "RSI":
            fig.add_trace(go.Scatter(x=res["time"], y=res[f"rsi_{rsi_len}"], mode="lines", name="RSI", line=dict(color="#C084FC", width=1.5)), row=row_num, col=1)
            fig.add_shape(type="line", x0=res["time"].iloc[0], y0=70, x1=res["time"].iloc[-1], y1=70, line=dict(color="rgba(239, 68, 68, 0.4)", width=1, dash="dash"), row=row_num, col=1)
            fig.add_shape(type="line", x0=res["time"].iloc[0], y0=30, x1=res["time"].iloc[-1], y1=30, line=dict(color="rgba(16, 185, 129, 0.4)", width=1, dash="dash"), row=row_num, col=1)
            
        elif sub == "MACD":
            fig.add_trace(go.Scatter(x=res["time"], y=res["macd"], mode="lines", name="MACD", line=dict(color="#60A5FA", width=1.5)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=res["time"], y=res["macd_signal"], mode="lines", name="Signal", line=dict(color="#F59E0B", width=1.2)), row=row_num, col=1)
            hist_colors = ["#10B981" if val >= 0 else "#EF4444" for val in res["macd_hist"]]
            fig.add_trace(go.Bar(x=res["time"], y=res["macd_hist"], name="Histogram", marker_color=hist_colors, opacity=0.6), row=row_num, col=1)
            
        elif sub == "Stochastic":
            fig.add_trace(go.Scatter(x=res["time"], y=res["stoch_k"], mode="lines", name="Stoch %K", line=dict(color="#22C55E", width=1.2)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=res["time"], y=res["stoch_d"], mode="lines", name="Stoch %D", line=dict(color="#EF4444", width=1.2)), row=row_num, col=1)
            
        elif sub == "Stoch RSI":
            fig.add_trace(go.Scatter(x=res["time"], y=res["stoch_rsi_k"], mode="lines", name="Stoch RSI %K", line=dict(color="#38BDF8", width=1.2)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=res["time"], y=res["stoch_rsi_d"], mode="lines", name="Stoch RSI %D", line=dict(color="#FB7185", width=1.2)), row=row_num, col=1)
            
        elif sub == "MFI":
            fig.add_trace(go.Scatter(x=res["time"], y=res["mfi"], mode="lines", name="MFI", line=dict(color="#14B8A6", width=1.5)), row=row_num, col=1)
            
        elif sub == "OBV":
            fig.add_trace(go.Scatter(x=res["time"], y=res["obv"], mode="lines", name="OBV", line=dict(color="#6366F1", width=1.5)), row=row_num, col=1)
            
        elif sub == "CMF":
            fig.add_trace(go.Scatter(x=res["time"], y=res["cmf"], mode="lines", name="CMF", line=dict(color="#E11D48", width=1.5)), row=row_num, col=1)
            
        elif sub == "ATR":
            fig.add_trace(go.Scatter(x=res["time"], y=res["atr"], mode="lines", name="ATR", line=dict(color="#F59E0B", width=1.5)), row=row_num, col=1)
            
        elif sub == "ADX":
            fig.add_trace(go.Scatter(x=res["time"], y=res["adx"], mode="lines", name="ADX", line=dict(color="#F43F5E", width=1.5)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=res["time"], y=res["plus_di"], mode="lines", name="+DI", line=dict(color="#10B981", width=1.0, dash="dot")), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=res["time"], y=res["minus_di"], mode="lines", name="-DI", line=dict(color="#EF4444", width=1.0, dash="dot")), row=row_num, col=1)
            
        elif sub == "Ultimate Oscillator":
            fig.add_trace(go.Scatter(x=res["time"], y=res["ultimate_osc"], mode="lines", name="UO", line=dict(color="#EC4899", width=1.5)), row=row_num, col=1)
            
        elif sub == "Awesome Oscillator":
            ao_colors = ["#10B981" if val >= 0 else "#EF4444" for val in res["ao"]]
            fig.add_trace(go.Bar(x=res["time"], y=res["ao"], name="AO", marker_color=ao_colors), row=row_num, col=1)
            
        elif sub == "CCI":
            fig.add_trace(go.Scatter(x=res["time"], y=res["cci"], mode="lines", name="CCI", line=dict(color="#84CC16", width=1.5)), row=row_num, col=1)
            
        elif sub == "TSI":
            fig.add_trace(go.Scatter(x=res["time"], y=res["tsi"], mode="lines", name="TSI", line=dict(color="#D946EF", width=1.5)), row=row_num, col=1)
            fig.add_trace(go.Scatter(x=res["time"], y=res["tsi_signal"], mode="lines", name="TSI Signal", line=dict(color="#F43F5E", width=1.0, dash="dash")), row=row_num, col=1)

    fig.update_layout(
        template="plotly_dark",
        height=400 + (200 * len(selected_subplots)),
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)"
    )
    
    fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.03)", linecolor="rgba(255,255,255,0.1)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.03)", linecolor="rgba(255,255,255,0.1)")
    
    st.plotly_chart(fig, use_container_width=True)

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
        
        fig_bt.add_trace(go.Scatter(x=bt_df["time"], y=bt_df["cum_strategy_returns"] * 100, mode="lines", name="Consensus Strategy", line=dict(color="#34D399", width=2.5)))
        fig_bt.add_trace(go.Scatter(x=bt_df["time"], y=bt_df["cum_asset_returns"] * 100, mode="lines", name="Buy & Hold (Market)", line=dict(color="#94A3B8", width=1.5, dash="dash")))
        
        fig_bt.update_layout(
            template="plotly_dark",
            title="Cumulative Equity Performance comparison (%)",
            height=500,
            xaxis_title="Timeline",
            yaxis_title="Return (%)",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)"
        )
        
        fig_bt.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.03)")
        fig_bt.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.03)")
        
        st.plotly_chart(fig_bt, use_container_width=True)

# Tab 3: Local Trading Journal
with tab_journal:
    st.markdown("### 📝 Local Trading Journal & Theorem Auditor")
    st.markdown("Inspect and audit trading decisions, hypotheses, and market theorems logged locally by the AI or user.")
    
    journal_file = BASE_DIR + "/dashboard/trading_journal.json"
    
    # Form for manual journal entry
    with st.expander("➕ Log a New Journal Entry / Thesis"):
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
                action_color = "#34D399" if e["action"] == "BUY" else "#F87171" if e["action"] == "SELL" else "#94A3B8"
                st.markdown(f"""
                <div class="journal-entry">
                    <div class="journal-header">
                        <span>📅 {e['timestamp']}</span>
                        <span>Confidence: <b>{e['confidence']}/10</b></span>
                    </div>
                    <div class="journal-title">
                        {e['symbol']} • <span style="color:{action_color}; font-weight:700;">{e['action']}</span> @ ${e['price']:.2f} (Size: {e['size']:.0f})
                    </div>
                    <p style="color:#CBD5E1; font-size:0.95rem; margin-top:8px; line-height:1.4; white-space: pre-wrap;">
                        {e['rationale']}
                    </p>
                </div>
                """, unsafe_allow_html=True)
    else:
        st.info("Journal database file not found. Logging your first entry will create it.")

# Tab 4: Signals Consensus
with tab_signals:
    st.markdown("### Dynamic Regime Classifier & Weighting Breakdown")
    st.markdown(f"Current Market Regime: **{current_regime}**")
    
    # Simple table outlining signals
    st.markdown("#### Live Indicators Signal Status")
    
    # We will build the signals manually for display
    signals = []
    # RSI
    rsi_val = latest_bar["rsi_14"]
    rsi_verdict = "BUY" if rsi_val < 30 else "SELL" if rsi_val > 70 else "NEUTRAL"
    rsi_color = "#10B981" if rsi_verdict == "BUY" else "#EF4444" if rsi_verdict == "SELL" else "#94A3B8"
    signals.append(("Momentum (RSI)", f"Value: {rsi_val:.2f}", rsi_verdict, rsi_color))
    
    # MACD
    macd_val = latest_bar["macd"]
    macd_sig = latest_bar["macd_signal"]
    macd_verdict = "BUY" if macd_val > macd_sig else "SELL"
    macd_color = "#10B981" if macd_verdict == "BUY" else "#EF4444"
    signals.append(("Trend (MACD Crossover)", f"MACD: {macd_val:.3f} | Signal: {macd_sig:.3f}", macd_verdict, macd_color))
    
    # SuperTrend
    st_dir = latest_bar["supertrend_dir"]
    st_verdict = "BUY" if st_dir == 1 else "SELL"
    st_color = "#10B981" if st_verdict == "BUY" else "#EF4444"
    signals.append(("Trend (SuperTrend)", f"Line: ${latest_bar['supertrend']:.2f}", st_verdict, st_color))
    
    # Bollinger
    bb_u = latest_bar["bb_upper"]
    bb_l = latest_bar["bb_lower"]
    bb_verdict = "BUY" if close_val <= bb_l else "SELL" if close_val >= bb_u else "NEUTRAL"
    bb_color = "#10B981" if bb_verdict == "BUY" else "#EF4444" if bb_verdict == "SELL" else "#94A3B8"
    signals.append(("Volatility (Bollinger Bands)", f"Bands: ${bb_l:.2f} - ${bb_u:.2f}", bb_verdict, bb_color))
    
    signal_table_html = """
    <table style="width:100%; border-collapse:collapse; background: rgba(30, 41, 59, 0.2); border-radius:12px; overflow:hidden;">
        <thead>
            <tr style="background: rgba(30, 41, 59, 0.6); text-align:left; border-bottom: 2px solid rgba(255,255,255,0.05);">
                <th style="padding: 14px 20px; font-weight:600; color:#38BDF8;">Indicator Category</th>
                <th style="padding: 14px 20px; font-weight:600; color:#38BDF8;">Calculated Condition</th>
                <th style="padding: 14px 20px; font-weight:600; color:#38BDF8; text-align:right;">Verdict</th>
            </tr>
        </thead>
        <tbody>
    """
    
    for category, details, verdict, color in signals:
        signal_table_html += f"""
            <tr style="border-bottom: 1px solid rgba(255,255,255,0.03);">
                <td style="padding: 14px 20px; font-weight:600; color:#E2E8F0;">{category}</td>
                <td style="padding: 14px 20px; color:#94A3B8;">{details}</td>
                <td style="padding: 14px 20px; font-weight:700; color:{color}; text-align:right;">{verdict}</td>
            </tr>
        """
        
    signal_table_html += """
        </tbody>
    </table>
    """
    st.markdown(signal_table_html, unsafe_allow_html=True)
    
    # Weighting explanation
    st.markdown("<br>#### Regime Adaptive Consensus Weighting Matrix", unsafe_allow_html=True)
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
    st.markdown("### 🛒 Human-In-The-Loop (HITL) Execution Desk")
    st.markdown("Review and approve orders drafted by the AI. **Claude cannot trade without your explicit physical approval here.**")
    
    drafts_path = BASE_DIR + "/dashboard/order_drafts.json"
    
    import json
    import os
    
    col_ref1, col_ref2 = st.columns([4, 1])
    with col_ref2:
        if st.button("🔄 Refresh Drafts"):
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
            st.info("✅ No pending order drafts. Ask Claude to draft a trade!")
        else:
            for draft in pending_drafts:
                st.markdown(f"""
                <div style="background: rgba(30, 41, 59, 0.6); border: 2px solid {'#10B981' if draft['action'] == 'BUY' else '#EF4444'}; border-radius: 12px; padding: 20px; margin-bottom: 15px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 10px; margin-bottom: 15px;">
                        <span style="font-size: 1.2rem; font-weight: bold;">Order Draft ID: {draft['draft_id']}</span>
                        <span style="color: #94A3B8; font-size: 0.9rem;">{draft['timestamp']}</span>
                    </div>
                    <div style="display: flex; gap: 30px; font-size: 1.1rem; margin-bottom: 20px;">
                        <div><span style="color:#94A3B8;">Action:</span> <strong style="color: {'#10B981' if draft['action'] == 'BUY' else '#EF4444'};">{draft['action']}</strong></div>
                        <div><span style="color:#94A3B8;">Symbol:</span> <strong>{draft['symbol']}</strong></div>
                        <div><span style="color:#94A3B8;">Quantity:</span> <strong>{draft['quantity']}</strong></div>
                        <div><span style="color:#94A3B8;">Type:</span> <strong>{draft['order_type']}</strong></div>
                        <div><span style="color:#94A3B8;">Limit Price:</span> <strong>{f"${draft['limit_price']}" if draft['limit_price'] else 'MKT'}</strong></div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                
                preview_key = f"preview_{draft['draft_id']}"
                col_prev, col_exec = st.columns([1, 1])

                # --- Step 1: price the order with the broker (non-binding) ---
                with col_prev:
                    if st.button(f"① Preview with Webull", key=f"btn_{preview_key}", use_container_width=True):
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
                        st.button("② Approve & Submit", key=f"exec_{draft['draft_id']}",
                                  use_container_width=True, disabled=True,
                                  help="Preview the order first — we never submit an order the broker has not validated.")
                    elif st.button(f"② 🔴 APPROVE & SUBMIT {draft['action']} {draft['quantity']} {draft['symbol']}",
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
    st.markdown("### 💼 Portfolio & Analytics")
    st.markdown("Live account balance and open positions straight from Webull.")
    
    col_port1, col_port2 = st.columns([4, 1])
    with col_port2:
        if st.button("🔄 Refresh Portfolio"):
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
        
        mcol1, mcol2, mcol3 = st.columns(3)
        with mcol1:
            st.metric(f"Net Liquidation ({currency})", f"{net_liq:,.2f}")
        with mcol2:
            st.metric("Unrealised P&L", f"{day_pnl:,.2f}",
                      delta=f"{day_pnl:,.2f}", delta_color="normal")
        with mcol3:
            st.metric("Buying Power (USD)", f"${buying_power:,.2f}",
                      help="Buying power is reported per currency; this is the USD line.")

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
            st.dataframe(pd.DataFrame(prows), use_container_width=True, hide_index=True)
            with st.expander("Raw broker payload"):
                st.json(positions)
            
    except Exception as e:
        st.error(f"Failed to fetch Webull account data: {str(e)}")

# Tab 7: Live Alerts & Daemon
with tab_alerts:
    st.markdown("### 🚨 Live Alerts & Watcher Daemon")
    st.markdown("The alert daemon monitors live price and indicator conditions in the background, firing native Windows desktop balloon notifications.")
    
    # Form to add new alert
    with st.expander("➕ Set New Technical Alert", expanded=False):
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
