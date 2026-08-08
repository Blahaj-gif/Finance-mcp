import os
import sys
import pandas as pd
import json
import math
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

# Adjust path to find local modules
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

import dashboard.webull_client as webull_client
import dashboard.indicators as indicators
import dashboard.econ_calendar as econ_calendar
import dashboard.iv_history as iv_history
import dashboard.edgar_forms as edgar_forms

# Data-integrity failures (bad ordering, stale bars) deliberately propagate out
# of the tools as real MCP errors instead of being flattened into a returned
# string. A tool that returns "Error: ..." as ordinary text is indistinguishable
# from content, which is how untrustworthy prices get read as authoritative.
from dashboard.webull_client import DataIntegrityError, StaleDataError, fallback_warning

# Every tool that prints a consensus verdict carries this. The score is a
# hand-tuned weighting, and presenting a number between -5 and +5 without that
# context invites it to be read as a measured probability.
HEURISTIC_NOTE = ('\n\n*The consensus score is a fixed-weight heuristic over five indicators, not a validated edge — backtested over 250 daily bars it underperformed buy & hold on MU, SPY and NVDA. Read it as a summary of what the indicators currently say.*')

# Initialize FastMCP Server
mcp = FastMCP("Finance MCP")

@mcp.tool()
def check_connection() -> str:
    """Tests connection to Webull API and Yahoo Finance fallback."""
    try:
        df, source = webull_client.fetch_data("AAPL", "D", 10)
        return f"Successfully connected! Test symbol AAPL loaded from {source} (10 bars)."
    except Exception as e:
        return f"Connection test FAILED: {e}"

@mcp.tool()
def get_account_info() -> str:
    """Fetches account list / information from Webull TH (requires authenticated token)."""
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
    
    try:
        if not webull_client.WEBULL_APP_KEY or not webull_client.WEBULL_APP_SECRET:
            raise ToolError("Webull App Key and App Secret are not configured in .env")
            
        # Shared, built-once client. Re-registering the SDK loggers per request
        # re-added handlers each time (10.7 MB log, lines duplicated ~20x) and
        # the SDK default level of DEBUG wrote the app key, request signatures
        # and full response bodies to disk.
        api_client = webull_client.get_api_client()

        trade_client = TradeClient(api_client)
        # Using the official v2 account list endpoint
        res = webull_client.unwrap(webull_client.call_webull(trade_client.account_v2.get_account_list))
        return f"### Webull Accounts\n\n```json\n{json.dumps(res, indent=2, default=str)}\n```"
    except Exception as e:
        raise ToolError(f"Error fetching account info: {e}") from e

@mcp.tool()
def get_market_analysis(symbol: str, interval: str = "D", count: int = 100,
                        include_verdict: bool = False) -> str:
    """
    Price action and 50+ technical indicators for a symbol, reported as measured values
    with each indicator's own standard reading (oversold / overbought, above / below its
    signal line, inside / outside its band).

    This is the usual starting point for a single symbol. For the full picture including
    fundamentals, news and filings, call get_comprehensive_profile instead of chaining calls.

    No BUY/SELL score is produced unless you ask for one. The composite verdict is a
    fixed-weight heuristic that underperformed buy-and-hold in backtest, and an
    unvalidated score anchors judgment even when it is labelled unvalidated — so the
    default is to hand back the evidence and leave the call to the reader.

    Args:
        symbol: The stock symbol (e.g. AAPL, KBANK).
        interval: Bar interval: D (Daily), M1 (1 min), M5 (5 min), M15 (15 min), M30 (30 min), H1 (1 hour), W (Weekly).
        count: Number of historical bars to analyze.
        include_verdict: Set true to also compute the composite BUY/SELL score. Off by default.
    """
    try:
        df, source = webull_client.fetch_data(symbol, interval, count)
        
        # Calculate indicators
        res = indicators.calculate_all_indicators(df)
        
        latest_bar = res.iloc[-1]
        prev_bar = res.iloc[-2]
        
        close_val = latest_bar["close"]
        price_change = close_val - prev_bar["close"]
        price_pct_change = (price_change / prev_bar["close"]) * 100
        
        # Automated Signal Calculations
        signals = []
        verdict_score = 0
        
        # RSI Signal
        rsi_val = latest_bar["rsi_14"]
        if rsi_val < 30:
            signals.append(f"- **RSI (14)**: Oversold ({rsi_val:.1f}) 🟢 **BUY**")
            verdict_score += 1.5
        elif rsi_val > 70:
            signals.append(f"- **RSI (14)**: Overbought ({rsi_val:.1f}) 🔴 **SELL**")
            verdict_score -= 1.5
        else:
            signals.append(f"- **RSI (14)**: Neutral ({rsi_val:.1f}) ⚪ **NEUTRAL**")
            
        # MACD
        macd_val = latest_bar["macd"]
        macd_sig = latest_bar["macd_signal"]
        prev_macd = prev_bar["macd"]
        prev_sig = prev_bar["macd_signal"]
        
        if prev_macd <= prev_sig and macd_val > macd_sig:
            signals.append("- **MACD**: Bullish Crossover 🟢 **STRONG BUY**")
            verdict_score += 2
        elif prev_macd >= prev_sig and macd_val < macd_sig:
            signals.append("- **MACD**: Bearish Crossover 🔴 **STRONG SELL**")
            verdict_score -= 2
        else:
            macd_direction = "Bullish" if macd_val > macd_sig else "Bearish"
            signals.append(f"- **MACD**: Trend is {macd_direction} ⚪ **NEUTRAL**")
            
        # Moving Averages
        sma_20 = latest_bar["sma_20"]
        sma_50 = latest_bar["sma_50"]
        if close_val > sma_20 and sma_20 > sma_50:
            signals.append("- **Moving Averages (20/50)**: Bullish Trend (Price > SMA20 > SMA50) 🟢 **BUY**")
            verdict_score += 1
        elif close_val < sma_20 and sma_20 < sma_50:
            signals.append("- **Moving Averages (20/50)**: Bearish Trend (Price < SMA20 < SMA50) 🔴 **SELL**")
            verdict_score -= 1
        else:
            signals.append("- **Moving Averages (20/50)**: Mixed Trend ⚪ **NEUTRAL**")
            
        # Bollinger Bands
        bb_u = latest_bar["bb_upper"]
        bb_l = latest_bar["bb_lower"]
        if close_val <= bb_l:
            signals.append("- **Bollinger Bands**: Price broke Lower Band (Rebound indicator) 🟢 **BUY**")
            verdict_score += 1
        elif close_val >= bb_u:
            signals.append("- **Bollinger Bands**: Price broke Upper Band (Pullback indicator) 🔴 **SELL**")
            verdict_score -= 1
        else:
            signals.append("- **Bollinger Bands**: Price inside Bands ⚪ **NEUTRAL**")
            
        # SuperTrend
        st_dir = latest_bar["supertrend_dir"]
        if st_dir == 1:
            signals.append("- **SuperTrend**: Bullish Trend 🟢 **BUY**")
            verdict_score += 1
        else:
            signals.append("- **SuperTrend**: Bearish Trend 🔴 **SELL**")
            
        # Determine Verdict Text
        if verdict_score >= 3:
            verdict = "STRONG BUY"
        elif verdict_score >= 1:
            verdict = "BUY"
        elif verdict_score <= -3:
            verdict = "STRONG SELL"
        elif verdict_score <= -1:
            verdict = "SELL"
        else:
            verdict = "NEUTRAL"
            
        # Strip the prescriptive tail from each reading unless a verdict was
        # asked for. "RSI (14): Oversold (25.3)" describes what the indicator
        # says; "🟢 BUY" tells the reader what to do on the strength of a
        # weighting nobody validated.
        if not include_verdict:
            import re as _re
            signals = [_re.sub(r"\s*[🟢🔴⚪]\s*\*\*(?:STRONG )?(?:BUY|SELL|NEUTRAL)\*\*\s*$", "", s)
                       for s in signals]

        verdict_block = (
            f"\n#### Composite Verdict *(opt-in heuristic)*\n"
            f"**{verdict}** (Score: {verdict_score:+.1f} / +5.0)\n"
            if include_verdict else ""
        )

        # Format results as a readable markdown block
        summary = f"""### Technical Analysis for {symbol.upper()} ({interval} Interval)
- **Last Price**: ${close_val:.2f} ({'+' if price_change >= 0 else ''}{price_change:.2f} / {price_pct_change:.2f}%)
- **Day's Range**: Low: ${latest_bar['low']:.2f} | High: ${latest_bar['high']:.2f}
- **Volume**: {latest_bar['volume']:,.0f}
{verdict_block}
#### Indicator Readings
{chr(10).join(signals)}

#### Select Indicator Values
- **VWAP**: ${latest_bar['vwap']:.2f} (Bands: ${latest_bar['vwap_lower']:.2f} - ${latest_bar['vwap_upper']:.2f})
- **RSI (14)**: {latest_bar['rsi_14']:.1f}
- **Stochastic (K/D)**: {latest_bar['stoch_k']:.1f} / {latest_bar['stoch_d']:.1f}
- **ATR (14)**: {latest_bar['atr_14']:.2f}
- **ADX (14)**: {latest_bar['adx']:.1f} (+DI: {latest_bar['plus_di']:.1f} | -DI: {latest_bar['minus_di']:.1f})
- **Ichimoku Conversion/Base**: {latest_bar['ichimoku_conversion']:.2f} / {latest_bar['ichimoku_base']:.2f}
"""
        out = (webull_client.freshness_line(df, source, interval)
               + fallback_warning(source) + summary)
        if include_verdict:
            out += HEURISTIC_NOTE
        else:
            out += ("\n*Indicator readings only — no composite score. Pass "
                    "`include_verdict=true` for the heuristic BUY/SELL score, "
                    "noting it underperformed buy-and-hold in backtest.*")
        return out
    except DataIntegrityError as e:
        # Untrustworthy data must reach the caller as an error, not as content.
        raise ToolError(str(e)) from e
    except Exception as e:
        raise ToolError(f"Error executing market analysis: {e}") from e

@mcp.tool()
def get_technical_indicators(symbol: str, interval: str = "D", count: int = 5) -> str:
    """
    Returns the latest calculated technical indicator values in markdown table format.
    Use when you want the raw indicator numbers to reason over yourself. If you want the
    numbers already interpreted into a BUY/SELL verdict, call get_market_analysis instead.
    
    Args:
        symbol: The stock symbol (e.g. AAPL, KBANK).
        interval: Bar interval: D (Daily), M1 (1 min), M5 (5 min), M15 (15 min), M30 (30 min), H1 (1 hour), W (Weekly).
        count: Number of latest bars to return (default 5).
    """
    try:
        df, source = webull_client.fetch_data(symbol, interval, count + 200) # fetch enough history for MA calculations
        res = indicators.calculate_all_indicators(df)
        
        # Take latest `count` rows
        latest_rows = res.tail(count)
        
        # Select columns of interest for display
        cols = [
            "time", "close", "volume", "rsi_14", "macd", "macd_signal", 
            "bb_upper", "bb_lower", "vwap", "supertrend", "adx"
        ]
        
        # Filter existing columns
        existing_cols = [c for c in cols if c in latest_rows.columns]
        display_df = latest_rows[existing_cols].copy()
        
        # Round numeric values for display
        for col in display_df.select_dtypes(include=['float64', 'float32']).columns:
            display_df[col] = display_df[col].round(2)
            
        try:
            table_str = display_df.to_markdown(index=False)
        except Exception:
            table_str = display_df.to_string(index=False)
            
        return (f"### Technical Indicators for {symbol.upper()} ({interval})\n"
                + webull_client.freshness_line(df, source, interval)
                + fallback_warning(source) + table_str)
    except DataIntegrityError as e:
        # Untrustworthy data must reach the caller as an error, not as content.
        raise ToolError(str(e)) from e
    except Exception as e:
        raise ToolError(f"Error retrieving indicators: {e}") from e

def atomic_write_json(file_path: str, data: list):
    """Safely writes JSON using atomic file replacement to prevent crash corruption."""
    temp_path = file_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(temp_path, file_path)

@mcp.tool()
def log_journal_entry(symbol: str, action: str, price: float, size: float, rationale: str, confidence: int = 5) -> str:
    """
    Logs a trade, market thesis, or trading theorem to the local Trading Journal database.
    Includes atomic file writing & de-duplication protection.
    
    Args:
        symbol: The stock symbol (e.g. AAPL, KBANK).
        action: The trade action: BUY, SELL, HOLD, or SYSTEM THESIS.
        price: Price per share of the asset at entry/thesis.
        size: Size of the trade in number of shares (0 for thesis only).
        rationale: Structured reason, thesis logic, or mathematical theorem why this trade/position is entered.
        confidence: Confidence level from 1 (lowest) to 10 (highest).
    """
    import json
    import datetime
    import hashlib
    
    journal_path = BASE_DIR + "/dashboard/trading_journal.json"
    
    try:
        entries = []
        if os.path.exists(journal_path):
            with open(journal_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    entries = json.loads(content)
                    
        # Generate unique fingerprint for de-duplication
        fingerprint = f"{symbol.upper()}_{action.upper()}_{round(float(price), 2)}_{rationale.strip()}"
        entry_id = hashlib.md5(fingerprint.encode()).hexdigest()[:10]
        
        # Prevent duplicate entries
        for e in entries:
            if e.get("entry_id") == entry_id:
                return f"Notice: Trade thesis for {symbol.upper()} already logged in journal (ID: {entry_id})."
                    
        # Capture where the market was, and from which feed, at the moment this
        # thesis was recorded. A price with no provenance cannot be audited later.
        prov = webull_client.get_provenance(symbol, "D")

        new_entry = {
            "entry_id": entry_id,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol.upper(),
            "action": action.upper(),
            "price": float(price),
            "size": float(size),
            "rationale": rationale,
            "confidence": int(confidence),
            "provenance": prov,
        }

        entries.append(new_entry)
        atomic_write_json(journal_path, entries)

        out = f"Successfully logged {action} thesis for {symbol} at ${price:.2f} (Entry ID: {entry_id})."

        # Flag a logged price that disagrees with the live market. This is the
        # check that would have exposed the reversed-bar bug immediately.
        market = prov.get("bar_close")
        if market:
            drift = abs(float(price) - market) / market * 100
            out += f"\nMarket reference: ${market:,.2f} as of {prov['bar_time']} ({prov['source']})."
            if drift > 5:
                out += (f"\n\n⚠️ **The logged price is {drift:.1f}% away from the latest bar.** "
                        "Verify this is intentional and not a stale or mistyped quote.")
        else:
            out += f"\n⚠️ Could not capture a market reference: {prov.get('error', 'unknown')}"

        return out
    except Exception as e:
        raise ToolError(f"Error logging to trading journal: {e}") from e

# =====================================================================
# TIER 1 — HIGH IMPACT DATA TOOLS
# =====================================================================

@mcp.tool()
def get_ohlcv(symbol: str, interval: str = "D", count: int = 20) -> str:
    """
    Fetches raw OHLCV (Open, High, Low, Close, Volume) candlestick bars for a symbol.
    Use when you need the price series itself — to eyeball recent action or do your own
    maths. For indicators use get_technical_indicators; for a verdict use get_market_analysis.
    
    Args:
        symbol: The stock symbol (e.g. AAPL, KBANK).
        interval: Bar interval (D, M1, M5, M15, M30, H1, W).
        count: Number of bars to return (default 20).
    """
    try:
        df, source = webull_client.fetch_data(symbol, interval, count)
        latest = df.tail(count).copy()
        
        for col in ["open", "high", "low", "close"]:
            if col in latest.columns:
                latest[col] = latest[col].round(2)
                
        try:
            table_str = latest.to_markdown(index=False)
        except Exception:
            table_str = latest.to_string(index=False)
            
        return (f"### OHLCV Bars for {symbol.upper()} ({interval})\n"
                + webull_client.freshness_line(df, source, interval)
                + fallback_warning(source) + table_str)
    except DataIntegrityError as e:
        # Untrustworthy data must reach the caller as an error, not as content.
        raise ToolError(str(e)) from e
    except Exception as e:
        raise ToolError(f"Error fetching OHLCV: {e}") from e

def _iv_context_block(symbol, calls, puts, spot, days, price_df) -> str:
    """
    IV rank and the implied expected move, as a compact header.

    Shared by the chain and the analytics tool so both report the same numbers
    from the same basis — the rank comes from this server's own recorded IV
    history once a symbol has enough of it, and says so when it does not.
    """
    try:
        c = calls.copy(); p = puts.copy()
        c["d"] = (c["strike"] - spot).abs(); p["d"] = (p["strike"] - spot).abs()
        atm_c, atm_p = c.nsmallest(1, "d").iloc[0], p.nsmallest(1, "d").iloc[0]
        atm_iv = (float(atm_c["impliedVolatility"]) + float(atm_p["impliedVolatility"])) / 2
        straddle = float(atm_c["lastPrice"]) + float(atm_p["lastPrice"])
        move_pct = straddle / spot * 100 if spot else 0.0

        try:
            iv_history.record_snapshot(symbol.upper(), atm_iv, spot=spot, dte=days)
            real = iv_history.iv_rank(symbol.upper(), atm_iv)
        except Exception:
            real = None

        block = f"* **ATM implied volatility**: `{atm_iv * 100:.1f}%`\n"
        if real:
            block += (f"* **IV rank**: `{real['rank']:.0f}/100` (percentile "
                      f"`{real['percentile']:.0f}%`) — from {real['observations']} recorded days\n")
        else:
            rv = price_df["close"].pct_change().dropna().rolling(20).std() * (252 ** 0.5)
            rv = rv.dropna()
            if len(rv) > 30:
                lo, hi = float(rv.min()), float(rv.max())
                proxy = (atm_iv - lo) / (hi - lo) * 100 if hi > lo else 50.0
                need = max(0, iv_history.MIN_OBSERVATIONS - iv_history.observation_count(symbol.upper()))
                block += (f"* **IV rank (proxy)**: `{max(0, min(100, proxy)):.0f}/100` "
                          f"— against realised volatility; {need} more daily observations "
                          "needed for a true IV rank\n")
        block += (f"* **Expected move by expiry**: **±{move_pct:.2f}%** "
                  f"(`${spot * (1 - move_pct / 100):,.2f}` – `${spot * (1 + move_pct / 100):,.2f}`) "
                  f"from the `${straddle:,.2f}` ATM straddle\n")
        return block
    except Exception as e:
        return f"* *IV context unavailable: {str(e)[:70]}*\n"


@mcp.tool()
def get_options_chain(symbol: str, expiration: str = None, strikes: int = 6) -> str:
    """
    Option chain around the money, with IV rank and the market-implied expected move
    at the top — the two numbers that tell you whether the board is cheap or dear and
    how far it is priced to travel.

    Strikes are selected to bracket spot, not taken from one end of the ladder.
    For greeks and put/call skew as well, call get_options_analytics; to hunt
    unusual flow call get_unusual_options.

    Args:
        symbol: The stock symbol (e.g. AAPL, SPY, NVDA).
        expiration: Expiry as YYYY-MM-DD. Defaults to the nearest.
        strikes: How many strikes either side of spot to show (default 6).
    """
    import yfinance as yf
    import datetime as _dt
    try:
        ticker = yf.Ticker(symbol.upper())
        options_dates = ticker.options
        if not options_dates:
            return f"No options data available for {symbol}."

        near_date = expiration or options_dates[0]
        if near_date not in options_dates:
            raise ToolError(f"{near_date} is not a listed expiry. Available: "
                            f"{', '.join(options_dates[:8])}")
        chain = ticker.option_chain(near_date)

        # Spot from the validated price feed, not from the chain's own quotes.
        df, source = webull_client.fetch_data(symbol, "D", 260)
        spot = float(df["close"].iloc[-1])
        days = max((_dt.date.fromisoformat(near_date) - _dt.date.today()).days, 0)

        header = (f"### Options Chain — {symbol.upper()} @ {near_date} ({days}d)\n"
                  + webull_client.freshness_line(df, source, "D")
                  + f"* **Spot**: `${spot:,.2f}`\n")
        header += _iv_context_block(symbol, chain.calls, chain.puts, spot, days, df)

        # Strikes bracketing the money. Previously this took .head(strikes),
        # which is the *lowest* strikes on the ladder -- deep in-the-money calls
        # and far out-of-the-money puts, i.e. the least useful rows on the board.
        cols = ["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]
        out_tables = []
        for label, frame in (("Calls", chain.calls), ("Puts", chain.puts)):
            f = frame.copy()
            f["dist"] = (f["strike"] - spot).abs()
            f = f.nsmallest(max(1, strikes), "dist").sort_values("strike")[cols].copy()
            f["moneyness"] = f["strike"].apply(
                lambda k: "ATM" if abs(k - spot) / spot < 0.005
                else ("ITM" if (k < spot) == (label == "Calls") else "OTM"))
            f["impliedVolatility"] = (f["impliedVolatility"] * 100).round(1).astype(str) + "%"
            for c in ("lastPrice", "bid", "ask"):
                f[c] = f[c].round(2)
            try:
                out_tables.append(f"**{label}** (bracketing spot)\n\n" + f.to_markdown(index=False))
            except Exception:
                out_tables.append(f"**{label}** (bracketing spot)\n\n" + f.to_string(index=False))

        return header + "\n" + "\n\n".join(out_tables)
    except (DataIntegrityError, ToolError):
        raise
    except Exception as e:
        raise ToolError(f"Error fetching options chain for {symbol}: {e}") from e

# NOTE: get_news is defined once, further down. It previously had a second
# definition here; duplicate @mcp.tool() names silently shadow one another in
# FastMCP, leaving one implementation registered and the other dead code.

# =====================================================================
# TIER 2 — MEANINGFUL EDGE ANALYTICS
# =====================================================================

@mcp.tool()
def scan_watchlist(symbols: str | list[str], interval: str = "D") -> str:
    """
    Scans a list of stock tickers, calculates consensus score & regime, and returns a ranked verdict table.

    Args:
        symbols: Symbols as a comma-separated string ("AAPL, TSLA, NVDA") or a list (["AAPL", "TSLA"]).
        interval: Bar interval (D, M15, H1).
    """
    # Clients send this either way; splitting a list raises AttributeError.
    if isinstance(symbols, str):
        raw_symbols = symbols.split(",")
    else:
        raw_symbols = list(symbols)
    ticker_list = [str(s).strip().upper() for s in raw_symbols if str(s).strip()]

    results = []
    failures = []
    sources = set()

    for sym in ticker_list:
        try:
            df, src = webull_client.fetch_data(sym, interval, 250)
            sources.add(webull_client.base_source(src))
            res_df = indicators.calculate_all_indicators(df)
            regime_df = indicators.classify_market_regime(res_df)
            regime = str(regime_df["regime"].iloc[-1])
            score_series = indicators.calculate_adaptive_consensus(res_df)
            raw_score = score_series.iloc[-1]
            if pd.isna(raw_score):
                # Fewer bars than the consensus warm-up needs. Say so rather
                # than coercing NaN into a number that reads like a signal.
                raise ValueError(
                    f"only {len(res_df)} bars available; the consensus score needs "
                    f"{indicators.CONSENSUS_WARMUP_BARS}+ bars of history")
            score = float(raw_score)
            last_close = float(res_df["close"].iloc[-1])
            
            verdict = "NEUTRAL"
            if score >= 2.0:
                verdict = "STRONG BUY"
            elif score >= 0.8:
                verdict = "BUY"
            elif score <= -2.0:
                verdict = "STRONG SELL"
            elif score <= -0.8:
                verdict = "SELL"
                
            results.append({
                "Symbol": sym,
                "Price": round(last_close, 2),
                "Consensus Score": round(score, 2),
                "Verdict": verdict,
                "Regime": regime
            })
        except Exception as e:
            # Never inject a 0.0 score into a ranked table -- a failed symbol is
            # not a neutral one, and sorting on a fabricated 0.0 silently places
            # it mid-pack as though it had been evaluated.
            failures.append(f"{sym}: {e}")

    if not results:
        return (f"### Watchlist Technical Scan ({interval} Interval)\n\n"
                "**No symbols could be evaluated.**\n\n"
                + "\n".join(f"* `{f}`" for f in failures))

    res_table = pd.DataFrame(results).sort_values(by="Consensus Score", ascending=False)
    try:
        table_str = res_table.to_markdown(index=False)
    except Exception:
        table_str = res_table.to_string(index=False)

    out = f"### Watchlist Technical Scan ({interval} Interval)\n\n"
    for src in sorted(sources):
        out += fallback_warning(src)
    out += table_str + HEURISTIC_NOTE
    if failures:
        out += (f"\n\n**⚠️ {len(failures)} of {len(ticker_list)} symbols could not be evaluated "
                "and are excluded from the ranking:**\n"
                + "\n".join(f"* `{f}`" for f in failures))
    return out

@mcp.tool()
def get_multi_timeframe(symbol: str) -> str:
    """
    Performs multi-timeframe analysis across Daily (D), 1-Hour (H1), and 15-Min (M15) for a symbol to compute a Confluence Score.
    
    Args:
        symbol: Stock ticker (e.g. AAPL, KBANK).
    """
    tf_weights = {"D": 0.5, "H1": 0.3, "M15": 0.2}
    rows = []
    total_confluence = 0.0
    covered_weight = 0.0
    sources = set()

    for tf, weight in tf_weights.items():
        try:
            df, src = webull_client.fetch_data(symbol, tf, 250)
            sources.add(webull_client.base_source(src))
            res_df = indicators.calculate_all_indicators(df)
            regime_df = indicators.classify_market_regime(res_df)
            regime = str(regime_df["regime"].iloc[-1])
            score_series = indicators.calculate_adaptive_consensus(res_df)
            raw_score = score_series.iloc[-1]
            if pd.isna(raw_score):
                # Fewer bars than the consensus warm-up needs. Say so rather
                # than coercing NaN into a number that reads like a signal.
                raise ValueError(
                    f"only {len(res_df)} bars available; the consensus score needs "
                    f"{indicators.CONSENSUS_WARMUP_BARS}+ bars of history")
            score = float(raw_score)
            weighted_contrib = score * weight
            total_confluence += weighted_contrib
            covered_weight += weight

            rows.append({
                "Timeframe": tf,
                "Weight": f"{int(weight*100)}%",
                "Score": round(score, 2),
                "Regime": regime,
                "Weighted Score": round(weighted_contrib, 2)
            })
        except Exception as e:
            # "FAILED", not 0.0. A missing timeframe is an absence of evidence,
            # not evidence of neutrality -- scoring it 0.0 let a failed daily leg
            # silently halve the confluence signal.
            rows.append({"Timeframe": tf, "Weight": f"{int(weight*100)}%", "Score": "FAILED",
                         "Regime": f"Error: {str(e)}", "Weighted Score": "n/a"})

    if covered_weight == 0.0:
        return (f"### Multi-Timeframe Confluence Analysis: {symbol.upper()}\n\n"
                "**No timeframe could be evaluated — no confluence score is available.**\n\n"
                + "\n".join(f"* `{r['Timeframe']}`: {r['Regime']}" for r in rows))

    # Renormalise to the weight actually covered, so the verdict thresholds stay
    # calibrated when a timeframe drops out.
    total_confluence = total_confluence / covered_weight

    df_tf = pd.DataFrame(rows)
    try:
        table_str = df_tf.to_markdown(index=False)
    except Exception:
        table_str = df_tf.to_string(index=False)

    confluence_verdict = "NEUTRAL"
    if total_confluence >= 2.0:
        confluence_verdict = "HIGH CONFLUENCE BULLISH (STRONG BUY)"
    elif total_confluence >= 0.8:
        confluence_verdict = "MODERATE BULLISH"
    elif total_confluence <= -2.0:
        confluence_verdict = "HIGH CONFLUENCE BEARISH (STRONG SELL)"
    elif total_confluence <= -0.8:
        confluence_verdict = "MODERATE BEARISH"
        
    out = f"### Multi-Timeframe Confluence Analysis: {symbol.upper()}\n\n"
    for src in sorted(sources):
        out += fallback_warning(src)
    out += table_str
    out += (f"\n\n**Overall Confluence Score**: `{round(total_confluence, 2)}` / +5.0"
            f"\n**Confluence Verdict**: **{confluence_verdict}**")
    if covered_weight < 0.999:
        out += (f"\n\n**⚠️ Partial coverage: only {covered_weight:.0%} of the timeframe weight was "
                "available. The score is renormalised over the timeframes that resolved.**")
    out += HEURISTIC_NOTE
    return out

@mcp.tool()
def compare_symbols(symbol1: str, symbol2: str, period_bars: int = 60) -> str:
    """
    Compares relative strength, price correlation, and return performance between two tickers.
    
    Args:
        symbol1: First ticker (e.g. AAPL or QQQ).
        symbol2: Second ticker (e.g. MSFT or SPY).
        period_bars: Number of historical daily bars for comparison (default 60).
    """
    try:
        # Both frames are guaranteed oldest-first by fetch_data, so iloc[0] is
        # the start of the window and iloc[-1] the latest bar. Before that
        # guarantee existed these returns came out sign-inverted.
        df1, src1 = webull_client.fetch_data(symbol1, "D", period_bars)
        df2, src2 = webull_client.fetch_data(symbol2, "D", period_bars)

        c1 = df1["close"].tail(period_bars).reset_index(drop=True)
        c2 = df2["close"].tail(period_bars).reset_index(drop=True)
        
        min_len = min(len(c1), len(c2))
        c1 = c1.tail(min_len)
        c2 = c2.tail(min_len)
        
        ret1 = ((c1.iloc[-1] - c1.iloc[0]) / c1.iloc[0]) * 100
        ret2 = ((c2.iloc[-1] - c2.iloc[0]) / c2.iloc[0]) * 100
        
        corr = float(c1.corr(c2))
        ratio = float(c1.iloc[-1] / c2.iloc[-1])
        
        out = (
            f"### Relative Performance & Correlation ({period_bars} Days)\n"
            + "".join(fallback_warning(s) for s in sorted({webull_client.base_source(src1), webull_client.base_source(src2)})) +
            f"* **{symbol1.upper()} Return**: `{ret1:+.2f}%` (Current Price: ${c1.iloc[-1]:.2f})\n"
            f"* **{symbol2.upper()} Return**: `{ret2:+.2f}%` (Current Price: ${c2.iloc[-1]:.2f})\n"
            f"* **Outperformer**: **{symbol1.upper() if ret1 > ret2 else symbol2.upper()}** (Spread: `{abs(ret1 - ret2):.2f}%`)\n"
            f"* **Price Correlation**: `{corr:.2f}`\n"
            f"* **Relative Ratio ({symbol1.upper()}/{symbol2.upper()})**: `{ratio:.4f}`\n"
        )
        return out
    except DataIntegrityError as e:
        # Untrustworthy data must reach the caller as an error, not as content.
        raise ToolError(str(e)) from e
    except Exception as e:
        raise ToolError(f"Error comparing symbols {symbol1} and {symbol2}: {e}") from e

# =====================================================================
# TIER 3 — JOURNAL & PORTFOLIO INTELLIGENCE
# =====================================================================

@mcp.tool()
def get_journal_summary() -> str:
    """
    Queries local Trading Journal DB for win rate, total trades, average confidence, and recent trade logs.
    """
    import json
    journal_path = BASE_DIR + "/dashboard/trading_journal.json"
    try:
        if not os.path.exists(journal_path):
            return "Trading Journal is empty."
            
        with open(journal_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
            
        if not entries:
            return "No entries found in Trading Journal."
            
        total = len(entries)
        buys = sum(1 for e in entries if e.get("action") == "BUY")
        sells = sum(1 for e in entries if e.get("action") == "SELL")
        avg_conf = sum(e.get("confidence", 5) for e in entries) / total
        
        latest_entries = entries[-5:]
        df_log = pd.DataFrame(latest_entries)
        try:
            log_str = df_log.to_markdown(index=False)
        except Exception:
            log_str = df_log.to_string(index=False)
            
        return (
            f"### Trading Journal Executive Summary\n"
            f"* **Total Logged Entries**: `{total}`\n"
            f"* **Buy Orders / Long Theses**: `{buys}`\n"
            f"* **Sell Orders / Short Theses**: `{sells}`\n"
            f"* **Average Conviction Score**: `{avg_conf:.1f}/10`\n\n"
            f"**Recent Log Entries**:\n{log_str}"
        )
    except Exception as e:
        raise ToolError(f"Error reading journal summary: {e}") from e

@mcp.tool()
def get_open_positions() -> str:
    """
    Pulls open Webull account positions and holdings summary.
    """
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
    
    try:
        if not webull_client.WEBULL_APP_KEY or not webull_client.WEBULL_APP_SECRET:
            raise ToolError("Webull App Key and App Secret are not configured in .env")
            
        # Shared, built-once client. Re-registering the SDK loggers per request
        # re-added handlers each time (10.7 MB log, lines duplicated ~20x) and
        # the SDK default level of DEBUG wrote the app key, request signatures
        # and full response bodies to disk.
        api_client = webull_client.get_api_client()

        trade_client = TradeClient(api_client)

        # This previously called get_account_list(), which returns accounts, not
        # holdings -- the tool never returned a position despite its name. The
        # SDK method is singular and account-scoped: get_account_position(account_id).
        account_id = webull_client.get_primary_account_id(trade_client)
        positions = webull_client.unwrap(webull_client.call_webull(trade_client.account_v2.get_account_position, account_id))

        if not positions:
            return f"### Open Positions\n\nNo open positions in account {account_id}."

        return f"### Open Positions (account {account_id})\n\n```json\n{positions}\n```"
    except Exception as e:
        raise ToolError(f"Error fetching open positions: {e}") from e

# =====================================================================
# INSTITUTIONAL & REPLICANT INTELLIGENCE TOOLS
# =====================================================================

@mcp.tool()
def get_earnings(symbol: str) -> str:
    """
    Fetches next earnings date, historical EPS estimates vs actuals, and surprise % for risk framing.
    
    Args:
        symbol: Stock ticker (e.g. AAPL, NVDA, TSLA).
    """
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol.upper())
        dates_df = ticker.earnings_dates
        
        if dates_df is None or dates_df.empty:
            return f"No earnings date information found for {symbol}."
            
        dates_df = dates_df.head(6).copy()
        dates_df.index = dates_df.index.strftime("%Y-%m-%d")
        dates_df = dates_df.reset_index().rename(columns={"index": "Date", "EPS Estimate": "Estimate", "Reported EPS": "Reported", "Surprise(%)": "Surprise %"})
        
        for col in ["Estimate", "Reported"]:
            if col in dates_df.columns:
                dates_df[col] = dates_df[col].round(2)
        if "Surprise %" in dates_df.columns:
            dates_df["Surprise %"] = (dates_df["Surprise %"] * 100).round(2).astype(str) + "%"
            
        try:
            table_str = dates_df.to_markdown(index=False)
        except Exception:
            table_str = dates_df.to_string(index=False)
            
        return f"### Earnings Calendar & History: {symbol.upper()}\n\n" + table_str
    except Exception as e:
        raise ToolError(f"Error fetching earnings data for {symbol}: {e}") from e

@mcp.tool()
def get_sector_heatmap() -> str:
    """
    Scans the 11 major S&P sector ETFs (Technology, Financials, Energy, Healthcare, Industrial, Consumer, Utilities, Real Estate, Materials) by momentum to identify sector rotation.
    """
    sectors = {
        "XLK": "Technology",
        "XLF": "Financials",
        "XLE": "Energy",
        "XLV": "Healthcare",
        "XLI": "Industrials",
        "XLY": "Consumer Discretionary",
        "XLP": "Consumer Staples",
        "XLC": "Communication Services",
        "XLU": "Utilities",
        "XLB": "Materials",
        "XLRE": "Real Estate"
    }
    
    def _n_day_return(close, n):
        """n-bar trailing return, in percent. close is oldest-first."""
        if len(close) < n + 1:
            return None
        prior = float(close.iloc[-1 - n])
        return ((float(close.iloc[-1]) - prior) / prior) * 100

    rows = []
    failures = []
    sources = set()
    for etf, name in sectors.items():
        try:
            # 26 bars so the 20-day lookback has a bar to reference.
            df, src = webull_client.fetch_data(etf, "D", 26)
            sources.add(webull_client.base_source(src))
            close = df["close"]

            # Requires oldest-first ordering. On the raw newest-first Webull
            # frames these came out sign-inverted, which meant the table below
            # sorted the worst-performing sectors to the top as "LEADER".
            ret_1d = _n_day_return(close, 1)
            ret_5d = _n_day_return(close, 5)
            ret_20d = _n_day_return(close, 20)
            if ret_1d is None or ret_5d is None or ret_20d is None:
                raise ValueError(f"only {len(close)} bars returned; need 21+ for a 20-day lookback")

            mom_score = 0.5 * ret_1d + 0.3 * ret_5d + 0.2 * ret_20d
            status = "LEADER [BULL]" if mom_score > 1.0 else ("LAGGARD [BEAR]" if mom_score < -1.0 else "NEUTRAL")

            rows.append({
                "ETF": etf,
                "Sector Name": name,
                "Price": round(float(close.iloc[-1]), 2),
                "1-Day %": f"{ret_1d:+.2f}%",
                "5-Day %": f"{ret_5d:+.2f}%",
                "20-Day %": f"{ret_20d:+.2f}%",
                "Rotation Status": status,
                "mom_num": mom_score
            })
        except Exception as e:
            # Was `except Exception: pass` -- sectors vanished from the table
            # with no indication, so a 7-sector heatmap looked like an 11-sector one.
            failures.append(f"{etf} ({name}): {e}")

    if not rows:
        return ("### S&P Sector Rotation & Momentum Heatmap\n\n"
                "**No sector could be evaluated.**\n\n"
                + "\n".join(f"* `{f}`" for f in failures))

    df_sec = pd.DataFrame(rows).sort_values(by="mom_num", ascending=False).drop(columns=["mom_num"])
    try:
        table_str = df_sec.to_markdown(index=False)
    except Exception:
        table_str = df_sec.to_string(index=False)

    out = "### S&P Sector Rotation & Momentum Heatmap\n\n"
    for src in sorted(sources):
        out += fallback_warning(src)
    out += table_str
    out += f"\n\n*Covering {len(rows)} of {len(sectors)} sectors.*"
    if failures:
        out += ("\n\n**⚠️ Sectors excluded from this ranking:**\n"
                + "\n".join(f"* `{f}`" for f in failures))
    return out

@mcp.tool()
def set_alert(symbol: str, condition: str, target_value: float, note: str = "") -> str:
    """
    Sets a local price or technical indicator alert for a ticker (e.g. "RSI < 30" or "PRICE > 250").
    
    Args:
        symbol: Ticker symbol (e.g. NVDA, AAPL).
        condition: Condition operator (e.g. PRICE_ABOVE, PRICE_BELOW, RSI_BELOW, RSI_ABOVE, MACD_CROSS).
        target_value: The price or indicator threshold value.
        note: Rationale or note for why this alert is set.
    """
    import json, datetime
    alerts_path = BASE_DIR + "/dashboard/alerts.json"
    try:
        alerts = []
        if os.path.exists(alerts_path):
            with open(alerts_path, "r", encoding="utf-8") as f:
                c = f.read().strip()
                if c:
                    alerts = json.loads(c)
                    
        # Record the market level the alert was set against, so a threshold can
        # later be judged against where price actually was when it was chosen.
        prov = webull_client.get_provenance(symbol, "D")

        new_alert = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol.upper(),
            "condition": condition.upper(),
            "target_value": float(target_value),
            "note": note,
            "status": "ACTIVE",
            "provenance_at_creation": prov,
        }
        alerts.append(new_alert)
        atomic_write_json(alerts_path, alerts)

        out = f"Successfully set alert for {symbol.upper()}: `{condition.upper()} {target_value}`. Note: {note}"
        if prov.get("bar_close"):
            out += (f"\nPrice when set: ${prov['bar_close']:,.2f} "
                    f"(bar {prov['bar_time']}, {prov['source']}).")
        return out
    except Exception as e:
        raise ToolError(f"Error setting alert: {e}") from e

@mcp.tool()
def get_unusual_options(symbol: str) -> str:
    """
    Scans for unusual options activity (Volume > Open Interest or high IV > 50%) to detect smart money positioning.
    
    Args:
        symbol: Stock ticker (e.g. AAPL, NVDA, TSLA).
    """
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol.upper())
        opts = ticker.options
        if not opts:
            return f"No options chain data for {symbol}."
            
        near_date = opts[0]
        chain = ticker.option_chain(near_date)
        
        unusual = []
        for opt_type, df_opt in [("CALL", chain.calls), ("PUT", chain.puts)]:
            for _, row in df_opt.iterrows():
                vol = row.get("volume", 0)
                oi = row.get("openInterest", 0)
                iv = row.get("impliedVolatility", 0)
                
                vol = 0 if pd.isna(vol) else vol
                oi = 0 if pd.isna(oi) else oi
                iv = 0 if pd.isna(iv) else iv
                strike = row.get("strike", 0)
                
                if (vol > oi and vol >= 100) or iv >= 0.60:
                    unusual.append({
                        "Type": opt_type,
                        "Strike": strike,
                        "Last Price": round(row.get("lastPrice", 0), 2),
                        "Volume": int(vol),
                        "Open Interest": int(oi),
                        "Vol/OI Ratio": round(vol / max(oi, 1), 2),
                        "IV %": f"{round(iv * 100, 1)}%",
                        "Flag": "🔥 Vol > OI" if vol > oi else "⚡ High IV"
                    })
                    
        if not unusual:
            return f"No unusual options activity flagged for {symbol.upper()} on expiration {near_date}."
            
        df_u = pd.DataFrame(unusual).sort_values(by="Volume", ascending=False).head(10)
        try:
            table_str = df_u.to_markdown(index=False)
        except Exception:
            table_str = df_u.to_string(index=False)
            
        return f"### Unusual Options Activity: {symbol.upper()} (Expiration: {near_date})\n\n" + table_str
    except Exception as e:
        raise ToolError(f"Error scanning unusual options for {symbol}: {e}") from e

@mcp.tool()
def get_short_interest(symbol: str) -> str:
    """
    Fetches short interest metrics (Short % of Float, Days to Cover / Short Ratio, Shares Short) for squeeze or squeeze-fade thesis.
    
    Args:
        symbol: Stock ticker (e.g. GME, TSLA, NVDA).
    """
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol.upper())
        info = ticker.info
        
        short_pct = info.get("shortPercentOfFloat")
        short_ratio = info.get("shortRatio")
        shares_short = info.get("sharesShort")
        held_inst = info.get("heldPercentInstitutions")
        
        short_pct_str = f"{round(short_pct * 100, 2)}%" if short_pct is not None else "N/A"
        days_to_cover = f"{round(short_ratio, 2)} days" if short_ratio is not None else "N/A"
        shares_str = f"{shares_short:,.0f}" if shares_short is not None else "N/A"
        inst_str = f"{round(held_inst * 100, 2)}%" if held_inst is not None else "N/A"
        
        squeeze_risk = "HIGH SQUEEZE POTENTIAL 🔥" if (short_pct and short_pct > 0.15) else "LOW / MODERATE SQUEEZE RISK"

        out = (
            f"### Short Interest & Float Analysis: {symbol.upper()}\n"
            f"* **Short % of Float**: `{short_pct_str}`\n"
            f"* **Days to Cover (Short Ratio)**: `{days_to_cover}`\n"
            f"* **Total Shares Short**: `{shares_str}`\n"
            f"* **Institutional Ownership**: `{inst_str}`\n"
            f"* **Squeeze Assessment**: **{squeeze_risk}**\n"
        )

        # Short % of float is a ratio against the share count. If that
        # denominator is wrong, so is every number above it -- so check it
        # against the figure the company actually filed.
        out += _fundamentals_check_note(
            symbol, {"shares_outstanding": info.get("sharesOutstanding")})
        return out
    except Exception as e:
        raise ToolError(f"Error fetching short interest for {symbol}: {e}") from e

# =====================================================================
# HUMAN-IN-THE-LOOP (HITL) ORDER EXECUTION DESK
# =====================================================================

@mcp.tool()
def draft_order(symbol: str, action: str, quantity: float, order_type: str = "LMT", limit_price: float = None) -> str:
    """
    Drafts an order for human review and approval in the Streamlit Dashboard.
    For safety, Claude NEVER places orders directly. All orders must be drafted and manually approved.
    
    Args:
        symbol: Ticker symbol (e.g. AAPL, TSLA).
        action: BUY or SELL.
        quantity: Number of shares.
        order_type: LMT (Limit) or MKT (Market).
        limit_price: The limit price if order_type is LMT.
    """
    import json
    import datetime
    import hashlib
    
    drafts_path = BASE_DIR + "/dashboard/order_drafts.json"
    try:
        drafts = []
        if os.path.exists(drafts_path):
            with open(drafts_path, "r", encoding="utf-8") as f:
                c = f.read().strip()
                if c:
                    drafts = json.loads(c)
        # Generate unique order draft ID
        fingerprint = f"{symbol.upper()}_{action.upper()}_{quantity}_{limit_price}_{datetime.datetime.now().timestamp()}"
        draft_id = "DRFT_" + hashlib.md5(fingerprint.encode()).hexdigest()[:8]
        
        # --- PRE-TRADE RISK CHECKS ---
        try:
            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient
            import sys
            
            api_client = webull_client.get_api_client()
            trade_client = TradeClient(api_client)
            
            # Every trade endpoint is account-scoped; calling these with no
            # argument raised TypeError, which the outer handler turned into a
            # blanket "failed to verify" block. The guardrails never actually ran.
            account_id = webull_client.get_primary_account_id(trade_client)

            if action.upper() == "SELL":
                positions = webull_client.unwrap(
                    webull_client.call_webull(trade_client.account_v2.get_account_position, account_id))
                inventory = webull_client.get_position_quantity(positions, symbol)

                if quantity > inventory:
                    return f"SAFETY BLOCK: You requested to SELL {quantity} {symbol}, but account inventory only shows {inventory} shares. Naked short-selling is blocked."

            elif action.upper() == "BUY":
                balances = webull_client.unwrap(
                    webull_client.call_webull(trade_client.account_v2.get_account_balance, account_id))
                bp = webull_client.get_buying_power(balances, "USD")

                est_price = limit_price
                if est_price is None:
                    import yfinance as yf
                    try:
                        est_price = float(yf.Ticker(symbol).fast_info.last_price)
                    except Exception as price_err:
                        # Previously this swallowed the error and set est_price = 0,
                        # which combined with the `est_price > 0` guard below to
                        # disable the buying-power check entirely. An order we
                        # cannot price is an order we must not wave through.
                        return (f"SAFETY BLOCK: Could not determine a price for {symbol.upper()} "
                                f"({price_err}), so the buying-power check cannot be performed. "
                                "Re-submit with an explicit limit_price.")

                est_price = float(est_price)
                if est_price <= 0:
                    return (f"SAFETY BLOCK: Non-positive price ({est_price}) for {symbol.upper()}; "
                            "cannot verify buying power.")

                notional = float(quantity) * est_price
                if notional > bp:
                    return f"SAFETY BLOCK: Order requires ~${notional:,.2f} but account buying power is only ${bp:,.2f}."
                    
        except Exception as e:
            # If risk check fails due to auth or API issues, return an error to prevent blind drafting
            return f"SAFETY BLOCK: Failed to verify account risk parameters: {str(e)}"
        # -----------------------------
        
        # Reject anything that could not be sent anyway, before it reaches the
        # human approval queue. A draft that cannot become an order is noise.
        try:
            webull_client.build_order(
                symbol=symbol, action=action, quantity=quantity,
                order_type=order_type, limit_price=limit_price,
                client_order_id=draft_id,
            )
        except ValueError as ve:
            return f"SAFETY BLOCK: This order could not be constructed: {ve}"

        new_draft = {
            "draft_id": draft_id,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol.upper(),
            "action": action.upper(),
            "quantity": float(quantity),
            "order_type": order_type.upper(),
            "limit_price": float(limit_price) if limit_price else None,
            "status": "PENDING_APPROVAL"
        }

        drafts.append(new_draft)
        atomic_write_json(drafts_path, drafts)

        return f"🚨 ORDER DRAFTED: {action.upper()} {quantity} shares of {symbol.upper()} at {limit_price if limit_price else 'MKT'}. Pending Human Approval in the MCP Dashboard."
    except Exception as e:
        raise ToolError(f"Error drafting order: {e}") from e


@mcp.tool()
def preview_order(symbol: str, action: str, quantity: float, order_type: str = "LMT",
                  limit_price: float = None) -> str:
    """
    Asks Webull to price and validate an order WITHOUT placing it. Non-binding and safe.
    Returns the broker's estimated cost and transaction fee, plus a buying-power comparison.
    Use this before draft_order to check affordability and fees.

    Args:
        symbol: Ticker symbol (e.g. AAPL, MU).
        action: BUY or SELL.
        quantity: Number of shares (fractional allowed).
        order_type: LMT (Limit) or MKT (Market).
        limit_price: Required when order_type is LMT.
    """
    from webull.trade.trade_client import TradeClient
    try:
        order = webull_client.build_order(
            symbol=symbol, action=action, quantity=quantity,
            order_type=order_type, limit_price=limit_price,
        )
        trade_client = TradeClient(webull_client.get_api_client())
        account_id = webull_client.get_primary_account_id(trade_client)
        quote = webull_client.preview_order(trade_client, account_id, order)

        cost = float(quote.get("estimated_cost", 0) or 0)
        fee = float(quote.get("estimated_transaction_fee", 0) or 0)

        out = (
            f"### Order Preview — {order['side']} {quantity} {order['symbol']} "
            f"({order['order_type']})\n"
            f"* **Estimated cost**: `${cost:,.2f}`\n"
            f"* **Estimated fee**: `${fee:,.2f}`\n"
            f"* **Total**: `${cost + fee:,.2f}`\n"
        )

        # The broker's preview does not enforce buying power, so state it here.
        try:
            balances = webull_client.unwrap(webull_client.call_webull(
                trade_client.account_v2.get_account_balance, account_id))
            bp = webull_client.get_buying_power(balances, "USD")
            affordable = order["side"] == "SELL" or (cost + fee) <= bp
            out += (f"* **USD buying power**: `${bp:,.2f}`\n"
                    f"* **Affordable**: {'✅ yes' if affordable else '❌ NO — exceeds buying power'}\n")
        except Exception as be:
            out += f"* **Buying power**: could not verify ({be})\n"

        out += "\n*This is a non-binding quote. Nothing has been ordered.*"
        return out
    except ValueError as ve:
        raise ToolError(f"Invalid order: {ve}") from ve
    except Exception as e:
        raise ToolError(f"Webull refused to preview this order: {e}") from e

@mcp.tool()
def get_open_orders() -> str:
    """
    Fetches all active/pending/working orders on the Webull account.
    """
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
    import logging
    import sys
    import os
    
    try:
        api_client = webull_client.get_api_client()

        trade_client = TradeClient(api_client)
        # get_order_open is account-scoped: calling it bare raised TypeError.
        account_id = webull_client.get_primary_account_id(trade_client)
        res = webull_client.unwrap(webull_client.call_webull(trade_client.order_v2.get_order_open, account_id))

        if not res:
            return f"### Open Orders\n\nNo working orders in account {account_id}."
        return f"### Open Orders (account {account_id})\n\n```json\n{res}\n```"
    except Exception as e:
        raise ToolError(f"Error fetching open orders: {e}") from e

@mcp.tool()
def cancel_order(order_id: str) -> str:
    """
    Cancels a pending or active order on the Webull account immediately.
    """
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
    import logging
    import sys
    import os
    
    try:
        api_client = webull_client.get_api_client()

        trade_client = TradeClient(api_client)
        # cancel_order(account_id, client_order_id) -- both arguments required.
        account_id = webull_client.get_primary_account_id(trade_client)
        res = webull_client.unwrap(webull_client.call_webull(trade_client.order_v2.cancel_order, account_id, order_id))

        return f"Cancellation request sent for Order ID {order_id} (account {account_id}). Response:\n```json\n{res}\n```"
    except Exception as e:
        raise ToolError(f"Error cancelling order {order_id}: {e}") from e

@mcp.tool()
def get_company_profile(symbol: str) -> str:
    """
    Gets business description, sector, and industry background for an asset (crucial for narrative context).
    """
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol.upper())
        info = ticker.info
        
        sector = info.get("sector", "N/A")
        industry = info.get("industry", "N/A")
        summary = info.get("longBusinessSummary", "No description available.")
        employees = info.get("fullTimeEmployees", "N/A")
        website = info.get("website", "N/A")
        
        out = (
            f"### Company Profile: {symbol.upper()}\n"
            f"* **Sector**: `{sector}`\n"
            f"* **Industry**: `{industry}`\n"
            f"* **Employees**: `{employees}`\n"
            f"* **Website**: `{website}`\n\n"
            f"**Business Summary**:\n{summary}"
        )
        return out
    except Exception as e:
        raise ToolError(f"Error fetching company profile for {symbol}: {e}") from e

@mcp.tool()
def get_news(symbol: str, count: int = 10) -> str:
    """
    Fetches the most recent news headlines for a given stock using Yahoo Finance.
    Crucial for analyzing fundamental catalysts or identifying the source of technical momentum breakouts.

    Args:
        symbol: Ticker symbol (e.g. AAPL, TSLA).
        count: Number of headlines to return (default 10).
    """
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol.upper())
        news = ticker.news
        if not news:
            return f"No recent news found for {symbol.upper()}."
            
        out = f"### Recent News for {symbol.upper()}\n\n"
        for idx, item in enumerate(news[:count]):
            # `.get(k, default)` returns None when the key exists with a null
            # value, and Yahoo sends "clickThroughUrl": null routinely -- hence
            # `or {}` rather than a default argument at every level.
            content = item.get("content") or item

            title = content.get("title") or "No Title"

            # Publisher (new schema: provider.displayName, old schema: publisher)
            provider = content.get("provider") or {}
            publisher = provider.get("displayName") or content.get("publisher") or "Unknown Publisher"

            # Link (new schema: clickThroughUrl.url / canonicalUrl.url, old: link)
            link_dict = content.get("clickThroughUrl") or content.get("canonicalUrl") or {}
            link = link_dict.get("url") or content.get("link") or "#"

            # Date (new schema: pubDate, old schema: providerPublishTime)
            import datetime
            pub_date = content.get("pubDate")
            if pub_date:
                # ISO8601 string, e.g. 2026-08-05T19:56:01Z
                date_str = str(pub_date).replace("T", " ").replace("Z", "")
            else:
                timestamp = content.get("providerPublishTime") or 0
                try:
                    date_str = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S') if timestamp else "Unknown Date"
                except (ValueError, OSError, TypeError):
                    date_str = "Unknown Date"

            out += f"**{idx+1}. {title}**\n"
            out += f"* {publisher} • {date_str} • [Link]({link})\n\n"

        return out
    except Exception as e:
        raise ToolError(f"Error fetching news for {symbol}: {e}") from e

# NOTE: get_earnings is defined once, in the TIER 2 section above. A second
# definition used to live here and silently shadowed it.

@mcp.tool()
def get_insider_trades(symbol: str) -> str:
    """
    Fetches recent insider transactions (executive buying/selling) for the ticker.
    Useful for gauging the 'smart money' sentiment of the company's leadership.
    """
    import yfinance as yf
    try:
        tk = yf.Ticker(symbol)
        df = tk.insider_transactions
        if df is None or df.empty:
            return f"No recent insider transactions found for {symbol}."
            
        out = f"### Insider Transactions for {symbol.upper()}\n\n"
        out += df.head(10).to_markdown()
        return out
    except Exception as e:
        raise ToolError(f"Error fetching insider transactions for {symbol}: {e}") from e

@mcp.tool()
def get_sec_filings(symbol: str) -> str:
    """
    Fetches the most recent SEC filings (10-K, 10-Q, 8-K) and their URLs.
    Provides raw access to corporate regulatory documents.
    """
    import yfinance as yf
    try:
        tk = yf.Ticker(symbol)
        filings = tk.sec_filings
        if not filings:
            return f"No SEC filings found for {symbol}."
            
        out = f"### Recent SEC Filings for {symbol.upper()}\n\n"
        for idx, f in enumerate(filings[:10]):
            title = f.get("title", f.get("type", "Unknown Filing"))
            date_str = f.get("date", "")
            link = f.get("edgarUrl", "#")
            out += f"**{idx+1}. {title}**\n"
            out += f"* Date: {date_str} • [EDGAR Link]({link})\n\n"
        return out
    except Exception as e:
        raise ToolError(f"Error fetching SEC filings for {symbol}: {e}") from e


PROFILE_SECTIONS = {
    "profile":   ("Company Profile",                 lambda s: get_company_profile(s)),
    "technicals":("Technical Indicators & Price Data", lambda s: get_technical_indicators(s)),
    "consensus": ("Market Consensus & Adaptive Signals", lambda s: get_market_analysis(s)),
    "short":     ("Short Interest",                  lambda s: get_short_interest(s)),
    "news":      ("Recent News",                     lambda s: get_news(s)),
    "earnings":  ("Earnings Calendar & Surprises",   lambda s: get_earnings(s)),
    "insiders":  ("Insider Trading Activity",        lambda s: get_insider_trades(s)),
    "filings":   ("SEC Filings",                     lambda s: get_sec_filings(s)),
    "options":   ("Options Analytics",               lambda s: get_options_analytics(s)),
    "risk":      ("Portfolio Risk",                  lambda s: get_portfolio_risk()),
    "edgar":     ("Live SEC Filings",                lambda s: get_edgar_filings(symbol=s, form_type="8-K,10-Q,10-K", limit=8)),
    "macro":     ("Economic Calendar & Macro",       lambda s: get_economic_calendar(21, 7)),
}

DEFAULT_PROFILE_SECTIONS = ["profile", "technicals", "consensus", "short",
                            "news", "earnings", "insiders", "filings"]


@mcp.tool()
def get_comprehensive_profile(symbol: str, sections: str | list[str] = None) -> str:
    """
    Fetches a master payload for a stock in a single round-trip, instead of 8 separate calls.
    Use this to open any analysis; reach for the individual tools only when you need
    one specific thing or a non-default parameter.

    Args:
        symbol: Ticker symbol (e.g. AAPL, TSLA).
        sections: Which sections to include, as a comma-separated string or list.
            Defaults to the 8 core sections. Available:
            profile, technicals, consensus, short, news, earnings, insiders,
            filings, options (IV rank/greeks), risk (live account exposure),
            edgar (real-time SEC filings), macro (economic calendar).
    """
    if sections is None:
        wanted = list(DEFAULT_PROFILE_SECTIONS)
    else:
        raw = sections.split(",") if isinstance(sections, str) else list(sections)
        wanted = [str(x).strip().lower() for x in raw if str(x).strip()]

    unknown = [w for w in wanted if w not in PROFILE_SECTIONS]
    if unknown:
        raise ToolError(
            f"Unknown section(s) {unknown}. Available: {', '.join(PROFILE_SECTIONS)}")

    out = f"# COMPREHENSIVE PROFILE: {symbol.upper()}\n\n"
    failed = []

    for n, key in enumerate(wanted, start=1):
        title, fn = PROFILE_SECTIONS[key]
        out += f"## {n}. {title}\n"
        try:
            out += fn(symbol) + "\n\n"
        except Exception as e:
            # Per-section isolation. The sub-tools raise ToolError now, so
            # without this one bad section would abort the whole profile --
            # which is the opposite of what a bundle is for.
            failed.append(key)
            out += f"*Unavailable: {e}*\n\n"

    if failed:
        out += (f"---\n**⚠️ {len(failed)} of {len(wanted)} section(s) unavailable: "
                f"{', '.join(failed)}.** The rest of this profile is unaffected.\n")
    return out


# =====================================================================
# RISK & POSITION SIZING
# =====================================================================

@mcp.tool()
def calculate_position_size(symbol: str, stop_loss_price: float, risk_percent: float = 1.0,
                            entry_price: float = None, account_currency: str = "USD") -> str:
    """
    Sizes a position from account risk rather than gut feel: how many shares can be
    bought such that being stopped out costs no more than `risk_percent` of the account.
    Also reports the ATR-based stop distance for context.

    Args:
        symbol: Ticker symbol (e.g. MU, NVDA).
        stop_loss_price: The price at which the thesis is wrong and you exit.
        risk_percent: Percent of account equity to risk on this trade (default 1.0).
        entry_price: Planned entry. Defaults to the latest close.
        account_currency: Currency line to size against (default USD).
    """
    from webull.trade.trade_client import TradeClient
    try:
        if risk_percent <= 0 or risk_percent > 100:
            raise ToolError(f"risk_percent must be between 0 and 100, got {risk_percent}")

        df, source = webull_client.fetch_data(symbol, "D", 60)
        res = indicators.calculate_all_indicators(df)
        latest = res.iloc[-1]
        last_close = float(latest["close"])
        atr = float(latest["atr_14"])

        entry = float(entry_price) if entry_price is not None else last_close
        stop = float(stop_loss_price)
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            raise ToolError("Entry and stop-loss are identical — risk per share would be zero.")

        direction = "LONG" if stop < entry else "SHORT"

        trade_client = TradeClient(webull_client.get_api_client())
        account_id = webull_client.get_primary_account_id(trade_client)
        balances = webull_client.unwrap(webull_client.call_webull(
            trade_client.account_v2.get_account_balance, account_id))
        buying_power = webull_client.get_buying_power(balances, account_currency)

        # Equity, not buying power, is the correct base for a risk budget.
        equity = 0.0
        for asset in balances.get("account_currency_assets", []) or []:
            if str(asset.get("currency", "")).upper() == account_currency.upper():
                equity = float(asset.get("market_value", 0) or 0) + float(asset.get("cash_balance", 0) or 0)
                break
        if equity <= 0:
            equity = buying_power

        risk_budget = equity * (risk_percent / 100.0)
        raw_shares = risk_budget / risk_per_share
        notional = raw_shares * entry

        capped_by = None
        shares = raw_shares
        if notional > buying_power:
            shares = buying_power / entry
            capped_by = "buying power"

        stop_pct = risk_per_share / entry * 100
        atr_multiple = risk_per_share / atr if atr > 0 else float("nan")

        out = (
            f"### Position Sizing — {direction} {symbol.upper()}\n"
            f"{fallback_warning(source)}"
            f"* **Entry**: `${entry:,.2f}`{'' if entry_price is not None else ' (latest close)'}\n"
            f"* **Stop loss**: `${stop:,.2f}`  →  risk/share `${risk_per_share:,.2f}` ({stop_pct:.2f}%)\n"
            f"* **Stop distance in ATR(14)**: `{atr_multiple:.2f}×` (ATR = ${atr:,.2f})\n\n"
            f"* **{account_currency} equity**: `${equity:,.2f}`\n"
            f"* **Risk budget @ {risk_percent:g}%**: `${risk_budget:,.2f}`\n"
            f"* **Suggested size**: **{shares:,.4f} shares** "
            f"(notional `${shares * entry:,.2f}`)\n"
        )
        if capped_by:
            out += (f"\n⚠️ Risk-based size was **{raw_shares:,.4f} shares** (`${notional:,.2f}`) "
                    f"but that exceeds {capped_by} of `${buying_power:,.2f}`. Size shown is capped.\n")
        if atr > 0 and atr_multiple < 1:
            out += ("\n⚠️ The stop is inside one ATR of daily noise — a routine day's range "
                    "would likely take you out.\n")
        return out
    except (DataIntegrityError, ToolError):
        raise
    except Exception as e:
        raise ToolError(f"Error calculating position size: {e}") from e


@mcp.tool()
def get_portfolio_risk() -> str:
    """
    Analyses the live account: position-level P&L, concentration, and portfolio
    volatility/beta versus SPY. Highlights over-concentration and correlated clusters.
    """
    from webull.trade.trade_client import TradeClient
    import numpy as np
    try:
        trade_client = TradeClient(webull_client.get_api_client())
        account_id = webull_client.get_primary_account_id(trade_client)
        positions = webull_client.unwrap(webull_client.call_webull(
            trade_client.account_v2.get_account_position, account_id))

        if not positions:
            return f"### Portfolio Risk\n\nNo open positions in account {account_id}."

        rows, returns, warnings = [], {}, []
        gross = 0.0
        for p in positions:
            sym = str(p.get("symbol", "")).upper()
            qty = float(p.get("quantity", 0) or 0)
            cost = float(p.get("cost_price", 0) or 0)
            last = float(p.get("last_price", 0) or 0)
            value = qty * last
            gross += value
            pnl_pct = ((last - cost) / cost * 100) if cost else 0.0

            vol = None
            try:
                df, _ = webull_client.fetch_data(sym, "D", 90)
                r = df["close"].pct_change().dropna()
                returns[sym] = r.reset_index(drop=True)
                vol = float(r.std() * (252 ** 0.5) * 100)
            except Exception as e:
                warnings.append(f"{sym}: no price history ({str(e)[:60]})")

            rows.append({
                "Symbol": sym,
                "Qty": round(qty, 4),
                "Cost": round(cost, 2),
                "Last": round(last, 2),
                "Value": round(value, 2),
                "P&L %": f"{pnl_pct:+.2f}%",
                "Ann. Vol %": f"{vol:.1f}%" if vol is not None else "n/a",
            })

        for r in rows:
            r["Weight %"] = f"{(r['Value'] / gross * 100):.1f}%" if gross else "n/a"
            if gross and r["Value"] / gross > 0.40:
                warnings.append(f"{r['Symbol']} is {r['Value'] / gross * 100:.0f}% of the portfolio "
                                "— single-name concentration above 40%.")

        table = pd.DataFrame(rows)[
            ["Symbol", "Qty", "Cost", "Last", "Value", "Weight %", "P&L %", "Ann. Vol %"]]
        try:
            table_str = table.to_markdown(index=False)
        except Exception:
            table_str = table.to_string(index=False)

        out = (f"### Portfolio Risk — account {account_id}\n\n"
               f"**Gross exposure**: `${gross:,.2f}` across {len(rows)} position(s)\n\n"
               + table_str + "\n")

        # Portfolio-level volatility and beta, weighted by position value.
        if len(returns) >= 1 and gross:
            try:
                bench, _ = webull_client.fetch_data("SPY", "D", 90)
                bench_r = bench["close"].pct_change().dropna().reset_index(drop=True)

                weights, series = [], []
                for r in rows:
                    s = returns.get(r["Symbol"])
                    if s is not None and len(s) > 5:
                        weights.append(r["Value"] / gross)
                        series.append(s)

                if series:
                    n = min(min(len(s) for s in series), len(bench_r))
                    mat = np.column_stack([s.tail(n).to_numpy() for s in series])
                    w = np.array(weights) / sum(weights)
                    port = mat @ w
                    b = bench_r.tail(n).to_numpy()

                    port_vol = float(port.std() * (252 ** 0.5) * 100)
                    var = b.var()
                    beta = float(np.cov(port, b)[0][1] / var) if var else float("nan")

                    out += (f"\n**Portfolio annualised volatility**: `{port_vol:.1f}%`\n"
                            f"**Beta vs SPY**: `{beta:.2f}`\n")

                    if len(series) > 1:
                        corr = np.corrcoef(mat, rowvar=False)
                        pairs = [
                            f"{rows[i]['Symbol']}/{rows[j]['Symbol']} `{corr[i][j]:.2f}`"
                            for i in range(len(series)) for j in range(i + 1, len(series))
                            if corr[i][j] > 0.7
                        ]
                        if pairs:
                            warnings.append("Highly correlated holdings (>0.70): " + ", ".join(pairs)
                                            + " — these will not diversify each other in a drawdown.")
            except Exception as e:
                out += f"\n*Portfolio volatility unavailable: {str(e)[:80]}*\n"

        if warnings:
            out += "\n**⚠️ Risk notes**\n" + "\n".join(f"* {w}" for w in warnings) + "\n"
        return out
    except (DataIntegrityError, ToolError):
        raise
    except Exception as e:
        raise ToolError(f"Error analysing portfolio risk: {e}") from e


@mcp.tool()
def get_options_analytics(symbol: str, expiration: str = None) -> str:
    """
    Options analytics beyond a raw chain: implied-volatility rank and percentile
    against the past year of realised volatility, the ATM straddle's implied move,
    put/call skew, and Black-Scholes greeks for near-the-money strikes.

    Args:
        symbol: Ticker symbol (e.g. MU, NVDA).
        expiration: Expiry as YYYY-MM-DD. Defaults to the nearest expiry.
    """
    import yfinance as yf
    import numpy as np
    import datetime as _dt
    from math import log, sqrt, exp, erf, pi

    def _norm_cdf(x):
        return 0.5 * (1 + erf(x / sqrt(2)))

    def _norm_pdf(x):
        return exp(-0.5 * x * x) / sqrt(2 * pi)

    def greeks(spot, strike, t, iv, is_call, r=0.04):
        """Black-Scholes greeks. Returns per-share sensitivities."""
        if t <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
            return {}
        d1 = (log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * sqrt(t))
        d2 = d1 - iv * sqrt(t)
        delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1
        gamma = _norm_pdf(d1) / (spot * iv * sqrt(t))
        vega = spot * _norm_pdf(d1) * sqrt(t) / 100          # per 1 vol point
        theta_year = (-(spot * _norm_pdf(d1) * iv) / (2 * sqrt(t))
                      + (-r if is_call else r) * strike * exp(-r * t)
                      * (_norm_cdf(d2) if is_call else _norm_cdf(-d2)))
        return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta_year / 365}

    try:
        tk = yf.Ticker(symbol.upper())
        expiries = tk.options
        if not expiries:
            raise ToolError(f"No options listed for {symbol.upper()}.")

        expiry = expiration or expiries[0]
        if expiry not in expiries:
            raise ToolError(f"{expiry} is not a listed expiry. Available: {', '.join(expiries[:8])}")

        # Spot from the validated price feed, not from the option chain.
        df, source = webull_client.fetch_data(symbol, "D", 260)
        spot = float(df["close"].iloc[-1])

        chain = tk.option_chain(expiry)
        calls, puts = chain.calls.copy(), chain.puts.copy()
        if calls.empty or puts.empty:
            raise ToolError(f"Empty option chain for {symbol.upper()} at {expiry}.")

        days = max((_dt.date.fromisoformat(expiry) - _dt.date.today()).days, 0)
        t = max(days, 1) / 365.0

        # ATM strike and the straddle's implied move.
        calls["dist"] = (calls["strike"] - spot).abs()
        puts["dist"] = (puts["strike"] - spot).abs()
        atm_call = calls.nsmallest(1, "dist").iloc[0]
        atm_put = puts.nsmallest(1, "dist").iloc[0]
        straddle = float(atm_call["lastPrice"]) + float(atm_put["lastPrice"])
        implied_move_pct = straddle / spot * 100

        atm_iv = (float(atm_call["impliedVolatility"]) + float(atm_put["impliedVolatility"])) / 2

        # IV rank/percentile against a year of realised vol -- a true IV history
        # needs a paid feed, so state the proxy rather than implying otherwise.
        rv = df["close"].pct_change().dropna().rolling(20).std() * (252 ** 0.5)
        rv = rv.dropna()
        # Record today's reading so a genuine IV history accumulates, then use it
        # if there is enough. IV rank is defined against implied-volatility
        # history; realised volatility is a different quantity and only ever a
        # stand-in until the real series exists.
        try:
            observations = iv_history.record_snapshot(symbol.upper(), atm_iv, spot=spot, dte=days)
            real_rank = iv_history.iv_rank(symbol.upper(), atm_iv)
        except Exception:
            observations, real_rank = 0, None

        if real_rank:
            iv_rank, iv_pct = real_rank["rank"], real_rank["percentile"]
            rank_basis = (f"true IV rank over {real_rank['observations']} recorded days "
                          f"({real_rank['first_date']} → {real_rank['last_date']}, "
                          f"range {real_rank['low'] * 100:.1f}%–{real_rank['high'] * 100:.1f}%)")
            rank_is_proxy = False
        else:
            iv_rank = iv_pct = None
            if len(rv) > 30:
                lo, hi = float(rv.min()), float(rv.max())
                if hi > lo:
                    iv_rank = (atm_iv - lo) / (hi - lo) * 100
                iv_pct = float((rv < atm_iv).mean() * 100)
            still_needed = max(0, iv_history.MIN_OBSERVATIONS - observations)
            rank_basis = (f"proxy against 1y realised volatility — "
                          f"{observations} IV observation(s) recorded so far, "
                          f"{still_needed} more needed for a true IV rank")
            rank_is_proxy = True

        out = (
            f"### Options Analytics — {symbol.upper()} @ {expiry} ({days}d)\n"
            f"{fallback_warning(source)}"
            f"* **Spot**: `${spot:,.2f}` ({webull_client.base_source(source)})\n"
            f"* **ATM implied volatility**: `{atm_iv * 100:.1f}%`\n"
        )
        if iv_rank is not None:
            label = "IV rank (proxy)" if rank_is_proxy else "IV rank"
            out += (f"* **{label}**: `{iv_rank:.0f}/100`"
                    f"  |  **percentile**: `{iv_pct:.0f}%`\n"
                    f"  <br>*Basis: {rank_basis}.*\n")
            verdict = ("Options look expensive — favours selling premium." if iv_rank > 65
                       else "Options look cheap — favours buying premium." if iv_rank < 35
                       else "")
            if verdict:
                out += f"  *{verdict}*\n"
        else:
            out += f"* **IV rank**: not yet available — {rank_basis}.\n"
        out += (f"* **ATM straddle**: `${straddle:,.2f}`  →  market implies a "
                f"**±{implied_move_pct:.2f}%** move by expiry "
                f"(`${spot * (1 - implied_move_pct / 100):,.2f}` – `${spot * (1 + implied_move_pct / 100):,.2f}`)\n")

        # Put/call skew: are downside strikes bid up relative to upside?
        otm_puts = puts[puts["strike"] < spot * 0.95]
        otm_calls = calls[calls["strike"] > spot * 1.05]
        if not otm_puts.empty and not otm_calls.empty:
            skew = float(otm_puts["impliedVolatility"].mean() - otm_calls["impliedVolatility"].mean())
            direction = "downside protection is bid up (bearish / hedging demand)" if skew > 0.02 else \
                        ("upside calls are bid up (bullish/speculative)" if skew < -0.02 else "roughly symmetric")
            out += f"* **Put/call IV skew**: `{skew * 100:+.1f} vol pts` — {direction}\n"

        # Greeks for strikes bracketing the money.
        near_calls = calls.nsmallest(3, "dist").sort_values("strike")
        near_puts = puts.nsmallest(3, "dist").sort_values("strike")
        grows = []
        for frame, is_call in ((near_calls, True), (near_puts, False)):
            for _, row in frame.iterrows():
                iv = float(row["impliedVolatility"])
                g = greeks(spot, float(row["strike"]), t, iv, is_call)
                if not g:
                    continue
                grows.append({
                    "Type": "CALL" if is_call else "PUT",
                    "Strike": round(float(row["strike"]), 2),
                    "Last": round(float(row["lastPrice"]), 2),
                    "IV %": round(iv * 100, 1),
                    "Delta": round(g["delta"], 3),
                    "Gamma": round(g["gamma"], 5),
                    "Vega": round(g["vega"], 3),
                    "Theta/day": round(g["theta"], 3),
                })
        if grows:
            gdf = pd.DataFrame(grows)
            try:
                gtable = gdf.to_markdown(index=False)
            except Exception:
                gtable = gdf.to_string(index=False)
            out += ("\n**Greeks near the money** (Black-Scholes, r=4%, per share)\n\n"
                    + gtable + "\n")

        if rank_is_proxy:
            out += ("\n*No free feed publishes implied-volatility history, so this server records "
                    "one observation per symbol per day as options are queried. Until a symbol has "
                    f"{iv_history.MIN_OBSERVATIONS} days of its own, the rank falls back to "
                    "realised volatility — a different quantity, labelled as a proxy.*")
        else:
            out += "\n*IV rank is computed from this server's own recorded implied-volatility history.*"
        return out
    except (DataIntegrityError, ToolError):
        raise
    except Exception as e:
        raise ToolError(f"Error computing options analytics for {symbol}: {e}") from e


# =====================================================================
# PUBLIC MACRO & FILINGS (BLS + SEC EDGAR)
# =====================================================================

def _fundamentals_check_note(symbol: str, external_values: dict) -> str:
    """
    Verify externally sourced figures against the company's own XBRL filing.

    Yahoo's fundamentals were the one part of this system with no validation
    behind them. This closes that: where a figure can be compared to the filed
    value, it is, and any disagreement is stated rather than left to be
    discovered later.
    """
    try:
        findings = econ_calendar.cross_check_fundamentals(symbol, external_values)
    except Exception as e:
        return f"\n*Filing cross-check unavailable: {str(e)[:90]}*\n"

    if not findings:
        return "\n*No comparable figure was tagged in this filer's XBRL, so nothing was cross-checked.*\n"

    disagreements = [f for f in findings if not f["agrees"]]
    if not disagreements:
        f = findings[0]
        return (f"\n*✅ Verified against the filed {f['form']} "
                f"({f['filed_date']}): {f['field'].replace('_', ' ')} matches to "
                f"{f['divergence_pct']:.2f}%.*\n")

    note = "\n**⚠️ Disagrees with the company's own filing:**\n"
    for f in disagreements:
        note += (f"* `{f['field']}` — source says `{f['external']:,.0f}`, "
                 f"{f['form']} filed {f['filed_date']} says `{f['filed']:,.0f}` "
                 f"({f['divergence_pct']:.1f}% apart). Prefer the filing.\n")
    return note


@mcp.tool()
def get_company_financials(symbol: str) -> str:
    """
    Headline financials taken straight from the company's filed XBRL on SEC EDGAR —
    revenue, net income, diluted EPS, assets, liabilities, cash, equity and shares
    outstanding. Every figure carries the form and filing date it came from.

    Prefer this over get_company_profile when a number has to be right: this is the
    filing itself, not a third-party summary of it.

    Args:
        symbol: Ticker symbol (e.g. MU, AAPL).
    """
    try:
        data = econ_calendar.company_financials(symbol)
        if not data["facts"]:
            raise ToolError(
                f"No XBRL facts found for {symbol.upper()} (CIK {data['cik']}). "
                "Foreign private issuers and pre-2009 filers may not tag financials.")

        rows = []
        for name, fact in data["facts"].items():
            value = fact["value"]
            if fact["unit"] == "USD":
                shown = f"${value:,.0f}"
            elif fact["unit"] in ("shares", "pure"):
                shown = f"{value:,.0f}"
            else:
                shown = f"{value:,.2f} {fact['unit']}"
            period = (f"{fact['start']} → {fact['end']}" if fact.get("start") else fact.get("end", "—"))
            rows.append({
                "Metric": name.replace("_", " ").title(),
                "Value": shown,
                "Period": period,
                "Form": fact.get("form", "—"),
                "Filed": fact.get("filed", "—"),
            })

        table = pd.DataFrame(rows)
        try:
            table_str = table.to_markdown(index=False)
        except Exception:
            table_str = table.to_string(index=False)

        return (f"### Filed Financials — {data['company']} ({data['symbol']})\n"
                f"*CIK {data['cik']} · source: SEC EDGAR XBRL, as filed*\n\n"
                + table_str
                + "\n\n*These are the company's own reported figures. Where a third-party "
                  "feed disagrees with this, the filing is authoritative.*")
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error fetching filed financials for {symbol}: {e}") from e


@mcp.tool()
def get_insider_activity(symbol: str, limit: int = 10, person: str = None,
                         since: str = None) -> str:
    """
    Parsed SEC Form 4 insider transactions — who traded, when, at what price, and
    crucially **whether the sale was made under a Rule 10b5-1 plan**.

    That distinction is the whole point: a pre-scheduled 10b5-1 sale carries almost no
    information about an insider's view, while a discretionary open-market sale does.
    The tool also separates real decisions (codes P/S) from compensation mechanics —
    option exercises, grants, and shares withheld to pay tax on vesting — which are
    routinely and wrongly reported as "insiders sold $X".

    Args:
        symbol: Ticker symbol (e.g. MU, AAPL).
        limit: How many Form 4 filings to parse (default 10).
        person: Filter to one insider by name, case- and accent-insensitive (e.g. "Mehrotra").
        since: ISO date (YYYY-MM-DD); drop transactions before it.
    """
    try:
        res = edgar_forms.insider_transactions(symbol, limit=limit, person=person, since=since)
        reports = res["filings"]

        if not reports:
            scope = f" matching '{person}'" if person else ""
            scope += f" since {since}" if since else ""
            return (f"### Insider Activity — {res['company']} ({res['symbol']})\n\n"
                    f"*No Form 4 filings found{scope}.*")

        out = f"### Insider Activity — {res['company']} ({res['symbol']})\n\n"

        flow = edgar_forms.summarise_insider_flow(reports)
        out += (f"**Open-market decisions across {len(reports)} filing(s)**\n"
                f"* Bought: `${flow['open_market_bought_value']:,.0f}` "
                f"({flow['open_market_bought_shares']:,.0f} sh)\n"
                f"* Sold: `${flow['open_market_sold_value']:,.0f}` "
                f"({flow['open_market_sold_shares']:,.0f} sh)\n"
                f"* **Net: `${flow['net_value']:,.0f}`**\n"
                f"* Of those sales — under a 10b5-1 plan: **{flow['sales_under_10b5_1']}**, "
                f"not under a plan: **{flow['sales_not_under_10b5_1']}**\n")
        if flow["non_discretionary_value"]:
            out += (f"* Excluded as compensation mechanics (grants, exercises, tax withholding): "
                    f"`${flow['non_discretionary_value']:,.0f}` — not a view on the stock\n")
        out += "\n"

        rows = []
        for r in reports:
            plan = {True: "Yes", False: "No", None: "not disclosed"}[r["plan_10b5_1"]]
            for t in r["transactions"]:
                rows.append({
                    "Date": t["date"],
                    "Insider": r["owner"][:26],
                    "Role": ", ".join(r["roles"])[:24] or "—",
                    "Action": f"{t['code']} · {t['code_label']}",
                    "Shares": f"{t['shares']:,.0f}" if t["shares"] else "—",
                    "Price": f"${t['price']:,.2f}" if t["price"] else "—",
                    "Value": f"${t['value']:,.0f}" if t["value"] else "—",
                    "10b5-1": plan,
                    "Discretionary": "yes" if t["is_open_market_decision"] else "no",
                })

        table = pd.DataFrame(rows)
        try:
            out += table.to_markdown(index=False)
        except Exception:
            out += table.to_string(index=False)

        notes = [fn for r in reports for fn in r["footnotes"]]
        if notes:
            out += "\n\n**Footnotes from the filings**\n"
            for n in dict.fromkeys(notes[:5]):
                out += f"* {n[:240]}\n"

        if res["errors"]:
            out += f"\n**⚠️ {len(res['errors'])} filing(s) could not be parsed:** " + \
                   "; ".join(res["errors"][:3]) + "\n"

        out += ("\n*Source: SEC Form 4 XML as filed. The 10b5-1 column reads the form's own "
                "`aff10b5One` checkbox — 'not disclosed' means the filer left it blank, which "
                "is not the same as 'no plan'.*")
        return out
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error reading insider activity for {symbol}: {e}") from e


@mcp.tool()
def get_data_sources() -> str:
    """
    Configuration and remaining quota for every external data source, plus how to
    raise the limits. Use this when a data tool fails or seems rate-limited.
    """
    try:
        status = econ_calendar.source_status()
        bls, sec = status["bls"], status["sec"]

        out = "### Data Source Status\n\n"

        out += "**Webull OpenAPI** — primary price feed\n"
        out += (f"* Credentials configured: {'✅' if webull_client.WEBULL_APP_KEY else '❌ set WEBULL_APP_KEY/SECRET in .env'}\n"
                f"* Region: `{webull_client.WEBULL_REGION_ID}`\n"
                f"* Pacing: {webull_client.WEBULL_MIN_REQUEST_INTERVAL}s between calls, "
                f"{webull_client.WEBULL_MAX_RETRIES} retries on HTTP 429\n\n")

        out += "**Yahoo Finance** — fallback prices, options, fundamentals\n"
        out += "* No key required. Fundamentals are cross-checked against SEC filings where possible.\n\n"

        out += f"**BLS** — macroeconomic data — `{bls['tier']}`\n"
        out += f"* Daily cap: {bls['daily_cap']} API queries"
        if bls["remaining_today"] is not None:
            out += f" · **{bls['remaining_today']} remaining today**"
        out += "\n* Release-schedule pages are ordinary web fetches and do not use this quota.\n"
        if bls["note"]:
            out += f"* {bls['note']}\n"
            out += (f"* To register: visit {econ_calendar.BLS_REGISTRATION_URL}, confirm by email, "
                    "then put the key in `.env` as `BLS_API_KEY=...` and restart. "
                    "Re-run this tool to confirm it was accepted.\n")
        out += "\n"

        out += "**SEC EDGAR** — filings and filed financials\n"
        out += f"* Contact header configured: {'✅' if sec['user_agent_configured'] else '❌'}\n"
        out += f"* Rate limit: {sec['rate_limit']}\n"
        if sec["note"]:
            out += f"* {sec['note']}\n"

        return out
    except Exception as e:
        raise ToolError(f"Error reading data source status: {e}") from e


@mcp.tool()
def validate_bls_key(key: str = None) -> str:
    """
    Test a BLS registration key against the live API and report which tier it unlocks.

    A mistyped key does not raise an error — it silently drops you to the 25/day
    unregistered limit, which only surfaces days later as an exhausted quota. Run this
    once after setting BLS_API_KEY.

    Args:
        key: Key to test. Omit to test whatever BLS_API_KEY is currently set to.
    """
    try:
        result = econ_calendar.validate_bls_key(key)
        icon = "✅" if result["valid"] else "❌"
        out = (f"### BLS Key Check\n\n"
               f"{icon} **{result['tier']}** — {result['daily_cap'] or '?'} queries/day\n\n"
               f"{result['detail']}\n")
        if not result["valid"] and not (key or econ_calendar.BLS_API_KEY):
            out += ("\n**To register (free, ~2 minutes):**\n"
                    f"1. Go to {econ_calendar.BLS_REGISTRATION_URL}\n"
                    "2. Enter your email; the key arrives by return mail.\n"
                    "3. Add `BLS_API_KEY=<your key>` to `.env`.\n"
                    "4. Restart the MCP server and run this tool again.\n\n"
                    "Registered access raises the cap from 25 to 500 queries/day, extends "
                    "history from 10 to 20 years, and enables BLS-computed percentage "
                    "changes that this server checks its own arithmetic against.\n")
        return out
    except Exception as e:
        raise ToolError(f"Error validating BLS key: {e}") from e

@mcp.tool()
def get_economic_calendar(days_ahead: int = 30, days_back: int = 7,
                          include_latest_data: bool = True) -> str:
    """
    Upcoming US macroeconomic releases (CPI, PPI, jobs, JOLTS and more) with their
    scheduled date and time, plus the most recent actual readings. Sourced live from
    the Bureau of Labor Statistics — free, no key required.

    Use this to know what is due before sizing risk into an event, or to check where
    inflation and employment actually stand.

    Args:
        days_ahead: How far forward to look for scheduled releases (default 30).
        days_back: How far back to include recently published releases (default 7).
        include_latest_data: Also report the latest CPI/core CPI/unemployment/payroll prints.
    """
    import datetime as _dt
    try:
        upcoming, failed = econ_calendar.upcoming_releases(days_ahead=days_ahead, days_back=days_back)
        today = _dt.date.today()

        out = f"### US Economic Calendar — {today} ({days_back}d back, {days_ahead}d ahead)\n\n"

        if upcoming:
            rows = []
            for e in upcoming:
                delta = (e["date"] - today).days
                when = "TODAY" if delta == 0 else (f"in {delta}d" if delta > 0 else f"{-delta}d ago")
                rows.append({
                    "Date": str(e["date"]),
                    "Time (ET)": e["time_et"],
                    "When": when,
                    "Release": e["release"],
                    "Covers": e["reference_period"],
                })
            table = pd.DataFrame(rows)
            try:
                out += table.to_markdown(index=False) + "\n"
            except Exception:
                out += table.to_string(index=False) + "\n"
        else:
            out += "*No scheduled releases in this window.*\n"

        if include_latest_data:
            data = econ_calendar.fetch_bls_series(
                ["cpi", "core_cpi", "unemployment", "payrolls", "ppi"])
            out += "\n**Latest prints**\n\n"
            drows = []
            for key, series in data.items():
                if not series["observations"]:
                    continue
                o = series["observations"][0]
                u = o.get("change_unit", "%")
                drows.append({
                    "Indicator": series["label"],
                    "Period": f"{o['period']} {o['year']}",
                    "Value": f"{o['value']:,.2f}",
                    "MoM": f"{o['mom_pct']:+.2f}{u}" if o["mom_pct"] is not None else "n/a",
                    "YoY": f"{o['yoy_pct']:+.2f}{u}" if o["yoy_pct"] is not None else "n/a",
                })
            dtable = pd.DataFrame(drows)
            try:
                out += dtable.to_markdown(index=False) + "\n"
            except Exception:
                out += dtable.to_string(index=False) + "\n"

        if failed:
            out += f"\n**⚠️ {len(failed)} schedule(s) unavailable:** " + "; ".join(failed) + "\n"

        status = econ_calendar.source_status()["bls"]
        out += (f"\n*Source: BLS {status['tier']}"
                + (f" · {status['remaining_today']} of {status['daily_cap']} queries left today"
                   if status["remaining_today"] is not None else "")
                + (f" · {status['note']}" if status["note"] else "") + "*")
        return out
    except Exception as e:
        raise ToolError(f"Error building economic calendar: {e}") from e


@mcp.tool()
def get_macro_data(series: str | list[str] = "cpi,core_cpi,unemployment",
                   months: int = 13) -> str:
    """
    Historical macroeconomic series from the BLS with month-over-month and
    year-over-year changes — the numbers behind the inflation and labour narrative.

    Args:
        series: Comma-separated keys or a list. Available: cpi, cpi_sa, core_cpi,
            unemployment, payrolls, ppi, avg_hourly_pay, labor_force.
        months: How many months of history to show per series (default 13, giving a full YoY view).
    """
    try:
        keys = series.split(",") if isinstance(series, str) else list(series)
        keys = [str(k).strip().lower() for k in keys if str(k).strip()]

        unknown = [k for k in keys if k not in econ_calendar.BLS_SERIES]
        if unknown:
            raise ToolError(f"Unknown series {unknown}. Available: "
                            f"{', '.join(econ_calendar.BLS_SERIES)}")

        data = econ_calendar.fetch_bls_series(keys)
        out = "### US Macroeconomic Data (BLS)\n\n"

        for key in keys:
            s = data.get(key)
            if not s or not s["observations"]:
                out += f"**{key}** — no observations returned.\n\n"
                continue
            obs = s["observations"][:max(1, months)]
            unit_sym = obs[0].get("change_unit", "%")
            rows = [{
                "Period": f"{o['period'][:3]} {o['year']}",
                "Value": f"{o['value']:,.2f}",
                f"MoM {unit_sym}": f"{o['mom_pct']:+.2f}" if o["mom_pct"] is not None else "n/a",
                f"YoY {unit_sym}": f"{o['yoy_pct']:+.2f}" if o["yoy_pct"] is not None else "n/a",
            } for o in obs]
            table = pd.DataFrame(rows)
            latest = obs[0]
            out += (f"**{s['label']}** (`{s['series_id']}`, {s['unit']}) — latest "
                    f"{latest['period']} {latest['year']}: **{latest['value']:,.2f}**"
                    + (f", YoY **{latest['yoy_pct']:+.2f}{unit_sym}**" if latest["yoy_pct"] is not None else "")
                    + "\n\n")
            try:
                out += table.to_markdown(index=False) + "\n\n"
            except Exception:
                out += table.to_string(index=False) + "\n\n"

        status = econ_calendar.source_status()["bls"]
        out += f"*Source: BLS {status['tier']}. {status['note']}*"
        return out
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error fetching macro data: {e}") from e


@mcp.tool()
def get_edgar_filings(symbol: str = None, form_type: str = "8-K",
                      query: str = None, limit: int = 15) -> str:
    """
    SEC EDGAR filings, as close to real time as a public feed allows — timestamps
    carry the second the SEC accepted the document.

    Three modes, chosen by what you pass:
      * `symbol` set        → that company's recent filings (earnings 8-Ks, 10-Q, 10-K, Form 4).
      * `query` set         → full-text search across filing bodies, 2001-present.
      * neither             → the live firehose of `form_type` filings from every registrant.

    Args:
        symbol: Ticker to scope to (e.g. MU, AAPL).
        form_type: Filing type — 8-K, 10-Q, 10-K, 4, S-1, 13F-HR. Comma-separate for several.
        query: Full-text phrase to search for (e.g. "going concern", "tariff").
        limit: Maximum rows to return (default 15).
    """
    try:
        forms = [f.strip().upper() for f in str(form_type).split(",") if f.strip()]

        if query:
            res = econ_calendar.full_text_search(query, forms=forms, limit=limit)
            out = (f"### EDGAR Full-Text Search — \"{query}\"\n"
                   f"*{res['total']:,} total matches; showing {len(res['results'])}.*\n\n")
            if not res["results"]:
                return out + "*No filings matched.*"
            table = pd.DataFrame([{
                "Filed": r["filed"], "Form": r["form"],
                "Company": r["company"][:44], "Link": r["url"],
            } for r in res["results"]])

        elif symbol:
            rows = econ_calendar.company_filings(symbol, forms=forms, limit=limit)
            if not rows:
                return (f"### EDGAR Filings — {symbol.upper()}\n\n"
                        f"*No {', '.join(forms)} filings found.*")
            out = f"### EDGAR Filings — {rows[0]['company']} ({symbol.upper()})\n\n"
            table = pd.DataFrame([{
                "Accepted (ET)": r["acceptance"][:19].replace("T", " "),
                "Form": r["form"],
                "Period": r["report_date"] or "—",
                "Items": (r["items"][:26] or "—"),
                "Link": r["url"],
            } for r in rows])

        else:
            rows = econ_calendar.live_filings(forms[0] if forms else "8-K", count=limit)
            out = (f"### EDGAR Live Feed — {forms[0] if forms else '8-K'} "
                   f"(all registrants, newest first)\n\n")
            if not rows:
                return out + "*No filings returned.*"
            table = pd.DataFrame([{
                "Accepted (ET)": r["accepted"][:19].replace("T", " "),
                "Form": r["form"],
                "Company": r["company"][:42],
                "CIK": r["cik"],
                "Link": r["url"],
            } for r in rows[:limit]])

        try:
            out += table.to_markdown(index=False)
        except Exception:
            out += table.to_string(index=False)

        out += ("\n\n*Source: SEC EDGAR. Latency is your polling interval, not the feed — "
                "acceptance timestamps are exact to the second.*")
        return out
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error fetching EDGAR filings: {e}") from e


if __name__ == "__main__":
    mcp.run(transport="stdio")
