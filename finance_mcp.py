import os
import sys
import pandas as pd
import json
import math
import time
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

# Adjust path to find local modules
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

import dashboard.webull_client as webull_client
import dashboard.indicators as indicators
import dashboard.econ_calendar as econ_calendar
import dashboard.iv_history as iv_history
import dashboard.options_math as options_math
import dashboard.volume_profile as volume_profile
import dashboard.edgar_forms as edgar_forms
import dashboard.central_banks as central_banks
import dashboard.market_calendar as market_calendar

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
        age = webull_client.bar_age(df, "D")
        # A connection check that says "connected" without saying how fresh the
        # data is answers the wrong question -- the feed was reachable during
        # the original staleness bug too.
        verdict = "live" if age["current"] else f"REACHABLE BUT {age['age'].upper()} BEHIND"
        return (f"Connected — {verdict}. Test symbol AAPL loaded from {source} "
                f"(10 bars, newest {age['as_of']}, {age['age']}).")
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
    fundamentals, filings and insider activity, call get_company_profile instead of chaining calls.

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
            signals.append(f"- **RSI (14)**: Oversold ({rsi_val:.1f}) **BUY**")
            verdict_score += 1.5
        elif rsi_val > 70:
            signals.append(f"- **RSI (14)**: Overbought ({rsi_val:.1f}) **SELL**")
            verdict_score -= 1.5
        else:
            signals.append(f"- **RSI (14)**: Neutral ({rsi_val:.1f}) **NEUTRAL**")
            
        # MACD
        macd_val = latest_bar["macd"]
        macd_sig = latest_bar["macd_signal"]
        prev_macd = prev_bar["macd"]
        prev_sig = prev_bar["macd_signal"]
        
        if prev_macd <= prev_sig and macd_val > macd_sig:
            signals.append("- **MACD**: Bullish Crossover **STRONG BUY**")
            verdict_score += 2
        elif prev_macd >= prev_sig and macd_val < macd_sig:
            signals.append("- **MACD**: Bearish Crossover **STRONG SELL**")
            verdict_score -= 2
        else:
            macd_direction = "Bullish" if macd_val > macd_sig else "Bearish"
            signals.append(f"- **MACD**: Trend is {macd_direction} **NEUTRAL**")
            
        # Moving Averages
        sma_20 = latest_bar["sma_20"]
        sma_50 = latest_bar["sma_50"]
        if close_val > sma_20 and sma_20 > sma_50:
            signals.append("- **Moving Averages (20/50)**: Bullish Trend (Price > SMA20 > SMA50) **BUY**")
            verdict_score += 1
        elif close_val < sma_20 and sma_20 < sma_50:
            signals.append("- **Moving Averages (20/50)**: Bearish Trend (Price < SMA20 < SMA50) **SELL**")
            verdict_score -= 1
        else:
            signals.append("- **Moving Averages (20/50)**: Mixed Trend **NEUTRAL**")
            
        # Bollinger Bands
        bb_u = latest_bar["bb_upper"]
        bb_l = latest_bar["bb_lower"]
        if close_val <= bb_l:
            signals.append("- **Bollinger Bands**: Price broke Lower Band (Rebound indicator) **BUY**")
            verdict_score += 1
        elif close_val >= bb_u:
            signals.append("- **Bollinger Bands**: Price broke Upper Band (Pullback indicator) **SELL**")
            verdict_score -= 1
        else:
            signals.append("- **Bollinger Bands**: Price inside Bands **NEUTRAL**")
            
        # SuperTrend
        st_dir = latest_bar["supertrend_dir"]
        if st_dir == 1:
            signals.append("- **SuperTrend**: Bullish Trend **BUY**")
            verdict_score += 1
        else:
            signals.append("- **SuperTrend**: Bearish Trend **SELL**")
            
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
        # says; a "BUY" label tells the reader what to do on the strength of a
        # weighting nobody validated.
        if not include_verdict:
            import re as _re
            signals = [_re.sub(r"\s*\*\*(?:STRONG )?(?:BUY|SELL|NEUTRAL)\*\*\s*$", "", s)
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
                out += (f"\n\n**Warning:** **The logged price is {drift:.1f}% away from the latest bar.** "
                        "Verify this is intentional and not a stale or mistyped quote.")
        else:
            out += f"\n**Warning:** Could not capture a market reference: {prov.get('error', 'unknown')}"

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
        # Solved from the mid, not read off Yahoo's column -- same correction as
        # get_options_analytics, for the same measured reason.
        t_years = max(days, 1) / 365.0
        iv_info = options_math.atm_iv(calls, puts, spot, t_years)
        atm_iv = iv_info["iv"]
        if atm_iv is None:
            return ""

        sd = options_math.straddle_price(calls, puts, spot)
        if not sd:
            return ""
        straddle = sd["straddle"]
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
    import datetime as _dt
    try:
        ticker = webull_client.yahoo_ticker(symbol.upper())
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
    ages = []

    for sym in ticker_list:
        try:
            df, src = webull_client.fetch_data(sym, interval, 250)
            sources.add(webull_client.base_source(src))
            age = webull_client.bar_age(df, interval)
            ages.append(age)
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
                # Per row, because one stale name among twenty fresh ones is
                # invisible in a header line -- and a scan is exactly where a
                # symbol that quietly stopped updating would hide.
                "As of": age["as_of"],
                "Age": age["age"],
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
    out += webull_client.freshness_summary(ages, interval, "symbols")
    for src in sorted(sources):
        out += fallback_warning(src)
    out += table_str + HEURISTIC_NOTE
    if failures:
        out += (f"\n\n**Warning: {len(failures)} of {len(ticker_list)} symbols could not be evaluated "
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
    ages = []

    for tf, weight in tf_weights.items():
        try:
            df, src = webull_client.fetch_data(symbol, tf, 250)
            ages.append(webull_client.bar_age(df, tf))
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
                # Confluence across timeframes is only meaningful if the legs
                # end at the same place; a weekly leg three sessions behind the
                # hourly one is agreeing about a different week.
                "As of": ages[-1]["as_of"],
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
    out += webull_client.freshness_summary(ages, "D", "timeframes")
    for src in sorted(sources):
        out += fallback_warning(src)
    out += table_str
    out += (f"\n\n**Overall Confluence Score**: `{round(total_confluence, 2)}` / +5.0"
            f"\n**Confluence Verdict**: **{confluence_verdict}**")
    if covered_weight < 0.999:
        out += (f"\n\n**Warning: Partial coverage: only {covered_weight:.0%} of the timeframe weight was "
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
        
        # Both legs stamped separately: a relative return is meaningless if one
        # side stopped updating, and a shared header line would hide which.
        age1 = webull_client.bar_age(df1, "D")
        age2 = webull_client.bar_age(df2, "D")
        out = (
            f"### Relative Performance & Correlation ({period_bars} Days)\n"
            + webull_client.freshness_summary([age1, age2], "D", "legs")
            + "".join(fallback_warning(s) for s in sorted({webull_client.base_source(src1), webull_client.base_source(src2)})) +
            f"* **{symbol1.upper()} Return**: `{ret1:+.2f}%` (Current Price: ${c1.iloc[-1]:.2f}, as of {age1['as_of']} — {age1['age']})\n"
            f"* **{symbol2.upper()} Return**: `{ret2:+.2f}%` (Current Price: ${c2.iloc[-1]:.2f}, as of {age2['as_of']} — {age2['age']})\n"
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
def get_earnings(symbol: str, confirm_with_sec: bool = True) -> str:
    """
    Next earnings date, historical EPS estimates vs actuals, and — critically — whether
    the upcoming date is CONFIRMED or merely Yahoo's ESTIMATE.

    Yahoo publishes an estimated report date as a window ("Oct 28 - Nov 3") and a set
    one as a single day. Both look identical once formatted, so an estimated date can
    read as fact and be wrong by a week. This tool says which it is.

    Past quarters are confirmed against the SEC: an 8-K carrying Item 2.02 ("Results
    of Operations") is the filing a company makes when it actually releases a quarter,
    and its acceptance timestamp is authoritative to the second.

    Args:
        symbol: Stock ticker (e.g. AAPL, NVDA, TSLA).
        confirm_with_sec: Cross-check reported quarters against 8-K Item 2.02 filings.
    """
    import datetime as _dt
    sym = symbol.upper()
    try:
        ticker = webull_client.yahoo_ticker(sym)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        out = f"### Earnings — {sym}  *(as of {stamp})*\n\n"

        # --- Next report: is the date set, or is it Yahoo guessing? ------------
        # Yahoo carries a start and an end timestamp for the next report. They are
        # equal when the date is set and differ when Yahoo is estimating a window.
        # Rendering only the start of a window as "the date" is the failure this
        # tool exists to prevent.
        window = []
        try:
            cal = ticker.calendar or {}
            raw = cal.get("Earnings Date") or []
            window = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        except Exception:
            cal = {}
        is_window = len(window) > 1 and window[0] != window[-1]

        # Yahoo's calendar endpoint and its earnings-dates table are separate
        # feeds and do disagree -- AAPL shows 30 Oct on one and 29 Oct on the
        # other. A date two Yahoo endpoints cannot agree on is not a set date,
        # whatever the single-value shape implies.
        dates_df = ticker.earnings_dates
        table_next = None
        if dates_df is not None and not dates_df.empty:
            pending = dates_df[dates_df.get("Reported EPS").isna()] \
                if "Reported EPS" in dates_df.columns else dates_df.iloc[0:0]
            if not pending.empty:
                table_next = pending.index.min().date()

        if window and not is_window:
            head = window[0]
            if table_next is not None and table_next != head:
                out += (f"**Next report: {head} — UNCONFIRMED.** Yahoo's calendar says "
                        f"{head}; its own earnings table says {table_next}.\n\n"
                        "*Two feeds from the same provider disagree, so the date is not "
                        "settled. Treat the pair as the window.*\n\n")
            else:
                out += (f"**Next report: {head}** — a single date, and Yahoo's two feeds "
                        "agree on it.\n\n"
                        "*That is Yahoo's assessment, not a company confirmation. Only "
                        "the 8-K below proves a quarter was released, and it appears "
                        "after the fact.*\n\n")
        elif window:
            out += (f"**Next report: ESTIMATED between {window[0]} and {window[-1]}** — "
                    "Yahoo has no set date.\n\n"
                    "*Treat this as a window, not a date. Sizing risk to the first day "
                    "of an estimated window is the mistake this flag exists to stop.*\n\n")
        else:
            out += "**Next report: not published by Yahoo.**\n\n"

        estimate_bits = [f"{label} ${cal[key]:,.2f}"
                         for key, label in (("Earnings Average", "Avg"),
                                            ("Earnings Low", "Low"),
                                            ("Earnings High", "High"))
                         if isinstance(cal.get(key), (int, float))]
        if estimate_bits:
            out += "**Analyst EPS estimate (next quarter):** " + " · ".join(estimate_bits) + "\n\n"

        # --- History ----------------------------------------------------------
        if dates_df is None or dates_df.empty:
            out += "*No reported-quarter history available from Yahoo.*\n"
        else:
            hist = dates_df.head(8).copy()
            hist.index = hist.index.strftime("%Y-%m-%d")
            hist = hist.reset_index().rename(columns={
                "index": "Date", "Earnings Date": "Date", "EPS Estimate": "Estimate",
                "Reported EPS": "Reported", "Surprise(%)": "Surprise %"})

            # yfinance's Surprise(%) is ALREADY a percentage (6.74 for a 6.74%
            # beat). Multiplying by 100 turned every AAPL beat into "+674%",
            # which is not a number anyone should repeat. Recompute from the two
            # columns we can see and use ours, checking Yahoo's against it.
            disagreements = []
            surprises = []
            for _, row in hist.iterrows():
                est, rep = row.get("Estimate"), row.get("Reported")
                if pd.isna(rep):
                    surprises.append("pending")
                    continue
                if pd.isna(est) or not est:
                    surprises.append("n/a")
                    continue
                ours = (rep - est) / abs(est) * 100
                theirs = row.get("Surprise %")
                if pd.notna(theirs) and abs(ours - float(theirs)) > 1.0:
                    disagreements.append(f"{row['Date']}: ours {ours:+.2f}% vs "
                                         f"Yahoo {float(theirs):+.2f}%")
                surprises.append(f"{ours:+.2f}%")

            for col in ("Estimate", "Reported"):
                if col in hist.columns:
                    hist[col] = hist[col].apply(
                        lambda v: "pending" if pd.isna(v) else f"{v:,.2f}")
            hist["Surprise %"] = surprises

            out += "**Reported quarters (Yahoo)**\n\n"
            try:
                out += hist.to_markdown(index=False) + "\n"
            except Exception:
                out += hist.to_string(index=False) + "\n"
            out += "\n*Surprise is computed from the estimate and the reported figure.*\n"
            if disagreements:
                out += ("\n**Warning — our surprise disagrees with Yahoo's own column:** "
                        + "; ".join(disagreements[:4]) + "\n")

        # --- SEC confirmation -------------------------------------------------
        if confirm_with_sec:
            try:
                hits = econ_calendar.earnings_filings(sym, limit=6)
            except Exception as e:
                out += f"\n*SEC confirmation unavailable: {str(e)[:140]}*\n"
                hits = None

            if hits is not None:
                if hits:
                    rows = [{
                        "Filed": f["filing_date"],
                        "Accepted (UTC)": (f.get("acceptance") or "").replace("T", " ")[:19],
                        "Period": f.get("report_date") or "—",
                        "Filing": f["url"],
                    } for f in hits]
                    out += ("\n**Confirmed by SEC — 8-K Item 2.02, Results of Operations**\n\n"
                            + pd.DataFrame(rows).to_markdown(index=False) + "\n")
                    out += ("\n*Acceptance timestamps are the SEC's own, to the second. "
                            "These confirm quarters already released; no filing exists "
                            "for a quarter not yet reported.*\n")
                else:
                    out += ("\n*No 8-K carrying Item 2.02 found for this filer. Foreign "
                            "private issuers report on 6-K and will show nothing here — "
                            "that is not evidence they have not reported.*\n")

        return out
    except ToolError:
        raise
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
    ages = []
    for etf, name in sectors.items():
        try:
            # 26 bars so the 20-day lookback has a bar to reference.
            df, src = webull_client.fetch_data(etf, "D", 26)
            sources.add(webull_client.base_source(src))
            age = webull_client.bar_age(df, "D")
            ages.append(age)
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
                # Rotation is a comparison, so a sector whose last bar is older
                # than the others is being ranked against a different day.
                "As of": age["as_of"],
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
    out += webull_client.freshness_summary(ages, "D", "sectors")
    for src in sorted(sources):
        out += fallback_warning(src)
    out += table_str
    out += f"\n\n*Covering {len(rows)} of {len(sectors)} sectors.*"
    if failures:
        out += ("\n\n**Warning: Sectors excluded from this ranking:**\n"
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
    try:
        ticker = webull_client.yahoo_ticker(symbol.upper())
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
                        "Flag": "VOL>OI" if vol > oi else "HIGH IV"
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
    try:
        ticker = webull_client.yahoo_ticker(symbol.upper())
        info = ticker.info
        
        short_pct = info.get("shortPercentOfFloat")
        short_ratio = info.get("shortRatio")
        shares_short = info.get("sharesShort")
        held_inst = info.get("heldPercentInstitutions")
        
        short_pct_str = f"{round(short_pct * 100, 2)}%" if short_pct is not None else "N/A"
        days_to_cover = f"{round(short_ratio, 2)} days" if short_ratio is not None else "N/A"
        shares_str = f"{shares_short:,.0f}" if shares_short is not None else "N/A"
        inst_str = f"{round(held_inst * 100, 2)}%" if held_inst is not None else "N/A"
        
        squeeze_risk = "HIGH SQUEEZE POTENTIAL" if (short_pct and short_pct > 0.15) else "LOW / MODERATE SQUEEZE RISK"

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
                    try:
                        est_price = float(webull_client.yahoo_ticker(symbol).fast_info.last_price)
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

        # State the surface on the draft itself. A model reading this back later
        # should never have to infer whether approving it spends real money.
        surface = ("simulated (Webull sandbox)" if webull_client.is_paper_environment()
                   else "the LIVE account")
        return (f"ORDER DRAFTED: {action.upper()} {quantity} shares of {symbol.upper()} "
                f"at {limit_price if limit_price else 'MKT'}. Pending human approval in "
                f"the dashboard's Execution tab, where it would reach {surface}.")
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
                    f"* **Affordable**: {'yes' if affordable else 'NO - exceeds buying power'}\n")
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

def _business_description(symbol: str) -> str:
    """Sector, industry and business summary. A section of get_company_profile."""
    try:
        ticker = webull_client.yahoo_ticker(symbol.upper())
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
    try:
        ticker = webull_client.yahoo_ticker(symbol.upper())
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
    try:
        tk = webull_client.yahoo_ticker(symbol)
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
    try:
        tk = webull_client.yahoo_ticker(symbol)
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


# How much weight a section's numbers carry. Merging many sources into one
# answer is only safe if this survives the merge -- otherwise a filed figure and
# a scraped headline read identically, which is the failure the whole data
# integrity effort exists to prevent.
PROVENANCE = {
    "filed":       ("[FILED]", "Filed with the SEC — authoritative"),
    "exact":       ("[EXACT]", "Exact parse of a filed form"),
    "market":      ("[MARKET]", "Market data, integrity-checked"),
    "official":    ("[OFFICIAL]", "Official government statistics"),
    "third_party": ("[THIRD-PARTY]", "Third-party feed, not independently verified"),
    "heuristic":   ("[ESTIMATE]", "Computed estimate — verify before relying on it"),
}

# (title, fetch, provenance). Ordered as an analyst reads: what the company is,
# what it reported, what insiders and owners are doing, then the market view.
PROFILE_SECTIONS = {
    "business":    ("Business & Description", lambda s: _business_description(s), "third_party"),
    "financials":  ("Filed Financials", lambda s: get_company_financials(s), "filed"),
    "earnings":    ("Earnings History & Surprises", lambda s: get_earnings(s), "third_party"),
    "filings":     ("Recent SEC Filings", lambda s: get_edgar_filings(symbol=s, form_type="8-K,10-Q,10-K", limit=6), "exact"),
    "insiders":    ("Insider Transactions (Form 4)", lambda s: get_insider_activity(s, limit=6), "exact"),
    "proposed":    ("Proposed Insider Sales (Form 144)", lambda s: get_insider_activity(s, limit=5, forms="144"), "exact"),
    "short":       ("Short Interest", lambda s: get_short_interest(s), "third_party"),
    "price":       ("Price & Technical Indicators", lambda s: get_market_analysis(s), "market"),
    "options":     ("Options — IV Rank & Expected Move", lambda s: get_options_analytics(s), "market"),
    "news":        ("Recent News", lambda s: get_news(s, count=6), "third_party"),
    "consensus":   ("Composite Verdict (heuristic)", lambda s: get_market_analysis(s, include_verdict=True), "heuristic"),
    "technicals":  ("Raw Indicator Table", lambda s: get_technical_indicators(s), "market"),
    "risk":        ("Your Portfolio Exposure", lambda s: get_portfolio_risk(), "market"),
    "macro":       ("Economic Calendar", lambda s: get_economic_calendar(21, 7), "official"),
}

# "Tell me about this company" — what the company is, what it filed, what the
# people closest to it are doing, and where it trades. Deliberately excludes
# the heuristic verdict, the raw indicator dump, and anything about the
# caller's own account.
DEFAULT_PROFILE_SECTIONS = ["business", "financials", "earnings", "filings",
                            "insiders", "short", "price", "news"]


def _run_sections(keys, symbol, timeout=45):
    """
    Fetch sections concurrently.

    They are independent HTTP calls to different hosts, so running them in
    sequence spends the whole profile waiting. The per-host rate limiters stay
    authoritative -- SEC calls still serialise at 10/s and Webull at its own
    pace -- concurrency only overlaps the waiting between different sources.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(keys)))) as pool:
        futures = {}
        for key in keys:
            title, fn, prov = PROFILE_SECTIONS[key]
            futures[pool.submit(fn, symbol)] = key
        for future in as_completed(futures, timeout=timeout):
            key = futures[future]
            try:
                results[key] = (True, future.result())
            except Exception as e:
                results[key] = (False, str(e))
    return results


@mcp.tool()
def get_company_profile(symbol: str, sections: str | list[str] = None,
                        detail: str = "standard") -> str:
    """
    Everything worth knowing about a company, in one call. Start here.

    Answers "tell me about X" without the caller needing to know which of the other
    tools to reach for: what the business is, what it filed with the SEC, what
    insiders and the market are doing, and where the stock trades. Sections are
    fetched concurrently, and each is labelled with how much weight its numbers
    carry — a figure taken from a filing is not the same kind of fact as one
    scraped from a third-party feed.

    Args:
        symbol: Ticker symbol (e.g. MU, AAPL).
        sections: Override which sections to include, comma-separated. Available:
            business, financials, earnings, filings, insiders, proposed, short,
            price, options, news, consensus, technicals, risk, macro.
        detail: "brief" (business, financials, price), "standard" (the default eight),
            or "full" (everything, including the heuristic verdict and macro calendar).
    """
    try:
        if sections:
            raw = sections.split(",") if isinstance(sections, str) else list(sections)
            wanted = [str(x).strip().lower() for x in raw if str(x).strip()]
        elif str(detail).lower() == "brief":
            wanted = ["business", "financials", "price"]
        elif str(detail).lower() == "full":
            wanted = list(PROFILE_SECTIONS)
        else:
            wanted = list(DEFAULT_PROFILE_SECTIONS)

        unknown = [w for w in wanted if w not in PROFILE_SECTIONS]
        if unknown:
            raise ToolError(f"Unknown section(s) {unknown}. "
                            f"Available: {', '.join(PROFILE_SECTIONS)}")

        started = time.time()
        results = _run_sections(wanted, symbol)
        elapsed = time.time() - started

        out = f"# {symbol.upper()} — Company Profile\n\n"
        failed, used = [], []

        for n, key in enumerate(wanted, start=1):
            title, _fn, prov = PROFILE_SECTIONS[key]
            icon, _meaning = PROVENANCE[prov]
            ok, body = results.get(key, (False, "section did not return"))
            out += f"## {n}. {title} {icon}\n"
            if ok:
                used.append(prov)
                out += body.strip() + "\n\n"
            else:
                failed.append(key)
                out += f"*Unavailable: {body}*\n\n"

        # The legend covers only what actually appears above, so it stays short
        # and every symbol in it is one the reader has just seen.
        out += "---\n**How to weigh these figures**\n"
        for prov in [p for p in PROVENANCE if p in used]:
            icon, meaning = PROVENANCE[prov]
            out += f"* {icon} {meaning}\n"

        out += (f"\n*{len(wanted) - len(failed)} of {len(wanted)} sections in "
                f"{elapsed:.1f}s, fetched concurrently.*")
        if failed:
            out += (f"\n\n**Warning: Unavailable: {', '.join(failed)}.** "
                    "The rest of this profile is unaffected.")
        return out
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error building profile for {symbol}: {e}") from e


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
            # This number sizes an actual order. The bar it came from belongs
            # next to it, not implied by the absence of a warning.
            f"{webull_client.freshness_line(df, source, 'D')}"
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
            out += (f"\n**Warning:** Risk-based size was **{raw_shares:,.4f} shares** (`${notional:,.2f}`) "
                    f"but that exceeds {capped_by} of `${buying_power:,.2f}`. Size shown is capped.\n")
        if atr > 0 and atr_multiple < 1:
            out += ("\n**Warning:** The stop is inside one ATR of daily noise — a routine day's range "
                    "would likely take you out.\n")
        return out
    except (DataIntegrityError, ToolError):
        raise
    except Exception as e:
        raise ToolError(f"Error calculating position size: {e}") from e


@mcp.tool()
def get_volume_profile(symbol: str, interval: str = "D", lookback: int = 100,
                       buckets: int = 20, value_area_pct: float = 0.70) -> str:
    """
    Volume-by-price for a symbol: point of control, value area, and the high and
    low volume nodes around the current price.

    Answers "where did this market previously agree on value, and where did it
    refuse to trade" — the auction-theory reading that price and oscillators
    cannot give. High volume nodes are shelves the market accepted and tends to
    revisit; low volume nodes are thin prices it rejected, and price usually
    travels through them quickly, so they act as breakout levels.

    Args:
        symbol: Stock ticker (e.g. AAPL, NVDA).
        interval: Bar size — D, W, M, H1, M30, M15, M5, M1 (default D).
        lookback: Bars in the profile window (default 100).
        buckets: Price bins; higher is a finer profile (default 20).
        value_area_pct: Fraction of volume inside the value area (default 0.70).
    """
    try:
        # Fetch a little beyond the window so the profile is never built from a
        # truncated one -- a short window silently reports a different auction.
        df, source = webull_client.fetch_data(symbol, interval, max(lookback + 20, 60))
        if len(df) < lookback:
            raise ToolError(
                f"{symbol.upper()}: only {len(df)} {interval} bars available, "
                f"fewer than the {lookback}-bar window requested.")

        highs = df["high"].tolist()
        lows = df["low"].tolist()
        volumes = df["volume"].tolist()
        spot = float(df["close"].iloc[-1])

        va = volume_profile.value_area(highs, lows, volumes, lookback=int(lookback),
                                       buckets=int(buckets), fraction=float(value_area_pct))
        if va is None:
            raise ToolError(
                f"No volume profile could be built for {symbol.upper()}: the window "
                "has no price range or no volume.")

        hvn = volume_profile.high_volume_nodes(highs, lows, volumes,
                                               lookback=int(lookback), buckets=int(buckets))
        lvn = volume_profile.low_volume_nodes(highs, lows, volumes,
                                              lookback=int(lookback), buckets=int(buckets))

        out = f"### Volume Profile — {symbol.upper()} ({interval}, {lookback} bars)\n"
        out += webull_client.freshness_line(df, source, interval) + "\n"
        out += webull_client.fallback_warning(source)
        out += (f"* **Last**: `{spot:,.2f}`\n"
                f"* **Point of control**: `{va['poc']:,.2f}` — the single price with the "
                "most traded volume in the window\n"
                f"* **Value area**: `{va['val']:,.2f}` – `{va['vah']:,.2f}` "
                f"({va['coverage'] * 100:.1f}% of volume)\n\n")

        out += f"**{volume_profile.describe_position(spot, hvn, lvn, va)}**\n\n"

        if hvn:
            rows = [{"Price": round(n["price"], 2),
                     "% of window volume": round(n["volume_share"] * 100, 2),
                     "POC": "yes" if n["is_poc"] else ""}
                    for n in sorted(hvn, key=lambda n: -n["price"])]
            out += "**High volume nodes** — prices the market accepted\n\n"
            out += pd.DataFrame(rows).to_markdown(index=False) + "\n\n"

        if lvn:
            rows = [{"Price": round(n["price"], 2),
                     "% of window volume": round(n["volume_share"] * 100, 2)}
                    for n in sorted(lvn, key=lambda n: -n["price"])]
            out += "**Low volume nodes** — thin prices the market rejected\n\n"
            out += pd.DataFrame(rows).to_markdown(index=False) + "\n\n"
        else:
            out += ("*No low volume nodes: the rule requires a bucket strictly thinner "
                    "than both neighbours, so a continuously-traded profile or a fully "
                    "untraded gap both return none.*\n\n")

        icon, meaning = PROVENANCE["market"]
        out += (f"{icon} {meaning}. Volume is spread uniformly across each bar's "
                "high-low range — real intrabar volume clusters at the open and close, "
                "so treat node prices as approximate to within a bucket "
                f"(`{(va['profile']['bucket_width']):,.2f}` wide here).\n")
        return out
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error building volume profile for {symbol}: {e}") from e


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

        rows, returns, warnings, ages = [], {}, [], []
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
            age = None
            try:
                df, _ = webull_client.fetch_data(sym, "D", 90)
                age = webull_client.bar_age(df, "D")
                ages.append(age)
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
                # The broker supplies the mark; the volatility comes from our
                # own history, and those can be different ages.
                "Vol as of": age["as_of"] if age else "n/a",
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
               + webull_client.freshness_summary(ages, "D", "position histories")
               + f"**Gross exposure**: `${gross:,.2f}` across {len(rows)} position(s)\n\n"
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
            out += "\n**Warning: Risk notes**\n" + "\n".join(f"* {w}" for w in warnings) + "\n"
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
        tk = webull_client.yahoo_ticker(symbol.upper())
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

        # Priced off the mid and solved from it, not taken from Yahoo's columns.
        # An audit of AAPL/SPY/NVDA/MU found `lastPrice` up to 31 days stale on
        # illiquid strikes, and `impliedVolatility` inconsistent with the very
        # quotes printed beside it -- re-pricing the ATM call at Yahoo's IV came
        # out 9.4% to 17.6% below the mid. Solving from the mid reproduces it to
        # within 0.1%.
        sd = options_math.straddle_price(calls, puts, spot)
        if sd:
            straddle, straddle_quality = sd["straddle"], sd["quality"]
        else:
            straddle = float(atm_call["lastPrice"]) + float(atm_put["lastPrice"])
            straddle_quality = "last"
        implied_move_pct = straddle / spot * 100

        iv_info = options_math.atm_iv(calls, puts, spot, t)
        atm_iv = iv_info["iv"]
        if atm_iv is None:
            raise ToolError(
                f"Could not derive an at-the-money implied volatility for "
                f"{symbol.upper()} at {expiry}. The ATM quotes are unusable "
                "(no two-sided market, or a price outside the no-arbitrage band)."
            )

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
            # Spot comes from the validated price feed, so it carries the feed's
            # as-of. The option quotes have their own staleness, reported below.
            f"{webull_client.freshness_line(df, source, 'D')}"
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
        # Illiquid rows are dropped first. 30% of AAPL strikes had a zero bid
        # and the 90th-percentile relative spread was 200%; averaging those in
        # imports noise as signal, and OTM wings are exactly where Yahoo's own
        # IV column goes to 600%+.
        def _wing_iv(frame, mask, is_call):
            rows = frame[mask]
            vols = []
            for _, row in rows.iterrows():
                if not options_math.is_liquid(row):
                    continue
                solved = options_math.solve_row_iv(row, spot, t, is_call)
                if solved["iv"] is not None:
                    vols.append(solved["iv"])
            return vols

        # Wings sized by the move the market is actually pricing, not a flat 5%.
        # On a 1-day expiry nothing trades 5% out, so a fixed band reported
        # "not measurable" on the most liquid chain in the world; on a LEAP it
        # would sit far inside the money instead.
        wing = max(implied_move_pct / 100.0, 0.01)
        put_vols = _wing_iv(puts, puts["strike"] < spot * (1 - wing), False)
        call_vols = _wing_iv(calls, calls["strike"] > spot * (1 + wing), True)
        if put_vols and call_vols:
            skew = sum(put_vols) / len(put_vols) - sum(call_vols) / len(call_vols)
            direction = "downside protection is bid up (bearish / hedging demand)" if skew > 0.02 else \
                        ("upside calls are bid up (bullish/speculative)" if skew < -0.02 else "roughly symmetric")
            out += (f"* **Put/call IV skew**: `{skew * 100:+.1f} vol pts` — {direction}\n"
                    f"  <br>*From {len(put_vols)} put and {len(call_vols)} call strikes beyond "
                    f"±{wing * 100:.1f}% (one implied move) with a tradeable two-sided "
                    f"market; illiquid strikes excluded.*\n")
        else:
            out += ("* **Put/call IV skew**: not measurable — too few wing strikes "
                    "have a two-sided market.\n")

        # Greeks for strikes bracketing the money.
        near_calls = calls.nsmallest(3, "dist").sort_values("strike")
        near_puts = puts.nsmallest(3, "dist").sort_values("strike")
        grows = []
        for frame, is_call in ((near_calls, True), (near_puts, False)):
            for _, row in frame.iterrows():
                solved = options_math.solve_row_iv(row, spot, t, is_call)
                iv = solved["iv"]
                if iv is None:
                    continue
                g = options_math.greeks(spot, float(row["strike"]), t, iv, is_call)
                if not g:
                    continue
                grows.append({
                    "Type": "CALL" if is_call else "PUT",
                    "Strike": round(float(row["strike"]), 2),
                    # The price the greeks were actually derived from, and how
                    # good that price is -- a greek off a 31-day-old last trade
                    # should not read the same as one off a live two-sided mid.
                    "Price": round(solved["price"], 2) if solved["price"] else None,
                    "Quote": solved["price_quality"],
                    "IV %": round(iv * 100, 1),
                    "IV src": solved["iv_source"],
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
        return (f"\n*Verified against the filed {f['form']} "
                f"({f['filed_date']}): {f['field'].replace('_', ' ')} matches to "
                f"{f['divergence_pct']:.2f}%.*\n")

    note = "\n**Warning: Disagrees with the company's own filing:**\n"
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


def _render_proposed_sales(symbol: str, limit: int) -> str:
    """Form 144 notices — intent to sell, filed ahead of the trade."""
    res = edgar_forms.proposed_sales(symbol, limit=limit)
    if not res["notices"]:
        return (f"### Proposed Sales (Form 144) — {res['company']} ({res['symbol']})\n\n"
                "*No Form 144 notices on file.*")

    out = (f"### Proposed Sales (Form 144) — {res['company']} ({res['symbol']})\n\n"
           f"**{len(res['notices'])} notice(s), "
           f"`${res['total_proposed_value']:,.0f}` of stock proposed for sale**\n\n")

    rows = []
    for n in res["notices"]:
        rows.append({
            "Filed": n["filed"],
            "Planned sale": n["approx_sale_date"] or "—",
            "Seller": (n["seller"] or "—")[:26],
            "Shares": f"{n['units_to_be_sold']:,.0f}" if n["units_to_be_sold"] else "—",
            "Market value": f"${n['aggregate_market_value']:,.0f}" if n["aggregate_market_value"] else "—",
            "% of shares out": f"{n['pct_of_shares_outstanding']:.3f}%" if n["pct_of_shares_outstanding"] else "—",
            "Acquired via": (n["acquisition_nature"] or "—")[:22],
            "10b5-1 adopted": ", ".join(n["plan_adoption_dates"]) or "—",
        })
    table = pd.DataFrame(rows)
    try:
        out += table.to_markdown(index=False)
    except Exception:
        out += table.to_string(index=False)

    prior = [(n, p) for n in res["notices"] for p in n["sold_in_past_3_months"]]
    if prior:
        seen, prows = set(), []
        for n, p in prior:
            key = (p["date"], p["shares"], p["seller"])
            if key in seen:
                continue
            seen.add(key)
            prows.append({
                "Sale date": p["date"],
                "Seller": p["seller"][:30],
                "Shares": f"{p['shares']:,.0f}",
                "Gross proceeds": f"${p['gross_proceeds']:,.0f}" if p["gross_proceeds"] else "—",
            })
        ptable = pd.DataFrame(prows)
        try:
            rendered = ptable.to_markdown(index=False)
        except Exception:
            rendered = ptable.to_string(index=False)
        out += "\n\n**Already sold in the prior three months** *(as declared on the notice)*\n\n" + rendered

    if res["errors"]:
        out += f"\n\n**Warning: {len(res['errors'])} notice(s) unparsed:** " + "; ".join(res["errors"][:3])

    out += ("\n\n*A Form 144 is a declaration of intent, filed before the sale — it leads the "
            "Form 4 that reports the completed trade, and not every notice results in a sale. "
            "The plan-adoption date is the one Form 4 does not carry: a 10b5-1 plan adopted "
            "shortly before a large sale is worth a second look, since the cooling-off rules "
            "turn on that date.*")
    return out


@mcp.tool()
def get_insider_activity(symbol: str, limit: int = 10, person: str = None,
                         since: str = None, forms: str = "4") -> str:
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
        forms: Which ownership forms to read — "4" (changes in ownership, the default),
            "3" (initial statement filed on becoming an insider — all holdings, no trades),
            "5" (annual statement of exempt or deferred transactions), or "3,4,5".
            Use "144" for notices of *proposed* sales, which are filed before the
            trade and so lead the Form 4 that later reports it.
    """
    try:
        form_list = [f.strip() for f in str(forms).split(",") if f.strip()]

        # Form 144 is a different schema and a different question -- intent to
        # sell, not a completed sale -- so it gets its own rendering.
        if form_list == ["144"]:
            return _render_proposed_sales(symbol, limit)
        res = edgar_forms.insider_transactions(symbol, limit=limit, person=person,
                                               since=since, forms=form_list)
        reports = res["filings"]

        if not reports:
            scope = f" matching '{person}'" if person else ""
            scope += f" since {since}" if since else ""
            return (f"### Insider Activity — {res['company']} ({res['symbol']})\n\n"
                    f"*No Form 4 filings found{scope}.*")

        out = f"### Insider Activity — {res['company']} ({res['symbol']})\n\n"

        flow = edgar_forms.summarise_insider_flow(reports)
        any_transactions = any(r["transactions"] for r in reports)

        # A Form 3 is holdings only. Printing "Bought $0 / Sold $0 / Net $0"
        # over it reads as "no insider activity" when the filing is in fact an
        # insider declaring an opening position.
        if any_transactions:
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

        if rows:
            table = pd.DataFrame(rows)
            try:
                out += table.to_markdown(index=False)
            except Exception:
                out += table.to_string(index=False)

        # Forms 3 and 5 report positions rather than trades, so a filing can be
        # entirely holdings. Omitting them would show an empty result for a
        # form whose whole purpose is to state an opening position.
        held = [(r, h) for r in reports for h in r.get("holdings", [])]
        if held:
            hrows = [{
                "Insider": r["owner"][:26],
                "Form": r.get("form", "?"),
                "Security": h["security"][:30],
                "Shares held": f"{h['shares_held']:,.0f}" if h["shares_held"] else "—",
                "Ownership": h.get("ownership") or "—",
                "Strike": f"${h['exercise_price']:,.2f}" if h.get("exercise_price") else "",
                "Expires": h.get("expiry") or "",
            } for r, h in held]
            htable = pd.DataFrame(hrows)
            try:
                rendered = htable.to_markdown(index=False)
            except Exception:
                rendered = htable.to_string(index=False)
            out += "\n\n**Positions held** *(Forms 3 and 5 report holdings, not trades)*\n\n" + rendered

        notes = [fn for r in reports for fn in r["footnotes"]]
        if notes:
            out += "\n\n**Footnotes from the filings**\n"
            for n in dict.fromkeys(notes[:5]):
                out += f"* {n[:240]}\n"

        if res["errors"]:
            out += f"\n**Warning: {len(res['errors'])} filing(s) could not be parsed:** " + \
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
def read_filing(symbol: str, form: str = "10-K", section: str = None,
                query: str = None, budget: int = 6000) -> str:
    """
    Read a named section out of a company's latest filing, or search its text.

    Filings are far too large to hand over whole — a Micron 10-K is ~610,000 tokens
    raw and ~97,000 after stripping markup — so this locates what you asked for and
    returns it under a character budget.

    Args:
        symbol: Ticker symbol (e.g. MU, AAPL).
        form: Filing type — 10-K, 10-Q, 8-K, S-1, DEF 14A.
        section: Item number to extract: 1 (Business), 1A (Risk Factors), 3 (Legal),
            7 (MD&A), 7A (Market Risk), 8 (Financial Statements), 9A (Controls).
        query: Instead of a section, return excerpts around each match of this phrase.
        budget: Maximum characters of section text to return (default 6000).
    """
    try:
        filings = econ_calendar.company_filings(symbol, forms=[form], limit=1)
        if not filings:
            raise ToolError(f"No {form} filing found for {symbol.upper()}.")
        f = filings[0]
        cik = econ_calendar.ticker_to_cik(symbol)["cik"]

        # Fetch the raw document once: the structured extractors below need the
        # markup (inline XBRL lives in attributes), while section extraction
        # needs it flattened.
        base = edgar_forms._filing_dir(cik, f["accession"])
        doc_name = (f.get("primary_document") or "").rsplit("/", 1)[-1]
        raw = edgar_forms._fetch(f"{base}/{doc_name}") if doc_name else ""
        text = edgar_forms.html_to_text(raw) if raw else \
            edgar_forms.fetch_filing_text(cik, f["accession"], f.get("primary_document"))

        head = (f"### {f['company']} — {f['form']} filed {f['filing_date']}\n"
                f"*Accepted {f['acceptance'][:19].replace('T', ' ')} · "
                f"[source]({f['url']}) · {len(text):,} chars of text*\n\n")

        upper_form = f["form"].upper()

        # ---- Structured extractors, when the form has one -------------
        if not section and not query and upper_form.startswith("DEF 14A"):
            comp = edgar_forms.executive_compensation(raw)
            if comp["found"]:
                out = head + f"#### Executive Compensation *(inline XBRL, {comp['count']} tagged facts)*\n\n"
                rows = [{"Measure": v["label"], "Value": f"${v['value']:,.0f}"}
                        for v in comp["facts"].values() if abs(v["value"]) > 1000]
                if rows:
                    t = pd.DataFrame(rows)
                    try:
                        out += t.to_markdown(index=False) + "\n"
                    except Exception:
                        out += t.to_string(index=False) + "\n"
                if comp["flags"]:
                    out += "\n**Governance disclosures**\n"
                    for fl in comp["flags"].values():
                        out += f"* {fl['label']}: **{'yes' if fl['value'] else 'no'}**\n"
                out += ("\n*Tagged under the `ecd` taxonomy from the 2023 pay-versus-performance "
                        "rule. These facts live inside the proxy, not in the XBRL companyfacts API. "
                        "Pass `section` or `query` to read the surrounding narrative.*")
                return out

        if not section and not query and upper_form.startswith("8-K"):
            # The 8-K cover document is usually a one-page pointer; the substance
            # is in exhibit 99.1.
            items = edgar_forms.describe_8k_items(f.get("items", ""))
            out = head
            if items:
                out += "**Reported items**\n" + "\n".join(f"* {i}" for i in items) + "\n\n"

            exhibit, name = edgar_forms.fetch_exhibit_99(cik, f["accession"])
            if exhibit:
                figures = edgar_forms.extract_headline_figures(exhibit)
                out += f"**Press release** (`{name}`, {len(exhibit):,} chars)\n\n"
                if figures:
                    out += "*Headline figures found in the opening text:*\n"
                    for k, v in figures.items():
                        label = k.replace("_", " ").title()
                        out += (f"* {label}: `{v:,.2f}%`\n" if k == "gross_margin"
                                else f"* {label}: `${v:,.2f}`\n" if k == "eps_diluted"
                                else f"* {label}: `${v:,.0f}`\n")
                    out += ("\n*Parsed from prose and phrased differently each quarter — treat as a "
                            "pointer and confirm against get_company_financials, which reads the "
                            "filed XBRL.*\n")
                out += "\n" + exhibit[:budget]
                if len(exhibit) > budget:
                    out += (f"\n\n*Truncated: {budget:,} of {len(exhibit):,} characters. "
                            "Use `query` to search within the filing.*")
                return out

            out += "*No exhibit 99 in this filing.*\n\n" + text[:budget]
            return out

        if not section and not query and upper_form.startswith(("SC 13D", "SC 13G")):
            stake = edgar_forms.parse_13dg(raw, is_xml=raw.lstrip().startswith("<?xml"))
            fields = stake["fields"]
            if fields:
                kind = "13D — filed with intent to influence control" if "13D" in upper_form \
                    else "13G — passive stake"
                out = head + f"#### Beneficial Ownership ({kind})\n\n"
                pretty = {"cusip": "CUSIP", "aggregate_amount": "Aggregate amount owned",
                          "percent_of_class": "Percent of class", "sole_voting": "Sole voting power",
                          "shared_voting": "Shared voting power",
                          "sole_dispositive": "Sole dispositive power",
                          "shared_dispositive": "Shared dispositive power"}
                for k, label in pretty.items():
                    if k in fields and fields[k] is not None:
                        v = fields[k]
                        out += (f"* **{label}**: `{v}`\n" if isinstance(v, str)
                                else f"* **{label}**: `{v:,.2f}`\n" if k == "percent_of_class"
                                else f"* **{label}**: `{v:,.0f}`\n")
                if stake.get("purpose_of_transaction"):
                    out += ("\n**Item 4 — Purpose of Transaction**\n\n"
                            + stake["purpose_of_transaction"][:2000] + "\n")
                out += (f"\n*Parsed from the {stake['source'].upper()} cover page, confidence "
                        f"{stake['confidence']}. The SEC mandated XML for these in December 2024, "
                        "but many filings remain HTML, so the cover page is read either way — "
                        "verify against the source link when confidence is not high.*")
                return out

        if query:
            hits = edgar_forms.search_filing(text, query, window=600, max_hits=6)
            if not hits:
                return head + f"*No occurrence of \"{query}\" in this filing.*"
            out = head + f"**{len(hits)} excerpt(s) around \"{query}\"**\n\n"
            for i, h in enumerate(hits, 1):
                out += f"{i}. …{h['excerpt']}…\n\n"
            return out

        if not section:
            # 8-K, DEF 14A and 13D/G are handled above by their own extractors.
            available = ", ".join(f"{k} ({v})" for k, v in edgar_forms.FILING_SECTIONS.items())
            return head + f"*Pass `section` to extract one, or `query` to search.*\n\nAvailable: {available}"

        body, meta = edgar_forms.extract_section(text, section, budget=budget)
        if not meta["found"]:
            return head + (f"*{meta['reason']}. Available sections in a 10-K: "
                           f"{', '.join(edgar_forms.FILING_SECTIONS)}*")

        title = edgar_forms.FILING_SECTIONS.get(section.upper(), "")
        out = head + f"#### Item {section.upper()} — {title}\n\n{body}\n"
        if meta["truncated"]:
            out += (f"\n*Truncated: showing {budget:,} of {meta['full_length']:,} characters. "
                    "Raise `budget` or use `query` to search within it.*")
        if meta["confidence"] == "low":
            out += ("\n\n*Warning: Low confidence in the section boundary — this filer's headings did not "
                    f"match cleanly ({meta['candidates']} candidate heading(s), "
                    f"basis: {meta['boundary_basis']}). Verify against the source link.*")
        return out
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error reading {form} for {symbol}: {e}") from e


def _render_fund_holdings(identifier: str, limit: int) -> str:
    """NPORT-P portfolio — monthly, and unlike 13F it includes bonds and derivatives."""
    d = edgar_forms.fund_holdings(identifier, limit=limit)
    if not d["holdings"]:
        return f"### {d['fund']}\n\n*No holdings in the latest NPORT-P.*"

    out = (f"### Fund Portfolio (NPORT-P) — {d['fund']}\n"
           f"*Period ending {d['period_end'] or '—'} · filed {d['filed']} · "
           f"{d['positions']:,} positions*\n\n")
    if d["net_assets"]:
        out += f"* **Net assets**: `${d['net_assets']:,.0f}`\n"
    if d["by_category"]:
        out += "* **By asset class**: " + " · ".join(
            f"{k} `{v / max(d['holdings_value'], 1) * 100:.1f}%`"
            for k, v in list(d["by_category"].items())[:6]) + "\n"
    out += "\n"

    rows = [{
        "Holding": (h["name"] or h["title"])[:32],
        "Value": f"${h['value_usd']:,.0f}" if h["value_usd"] else "—",
        "% of fund": f"{h['pct_of_fund']:.3f}%" if h["pct_of_fund"] else "—",
        "Units": f"{h['balance']:,.0f}" if h["balance"] else "—",
        "Class": edgar_forms.ASSET_CATEGORIES.get(h["asset_category"], h["asset_category"] or "—"),
        "Side": h.get("payoff_profile") or "—",
    } for h in d["holdings"]]

    table = pd.DataFrame(rows)
    try:
        out += table.to_markdown(index=False)
    except Exception:
        out += table.to_string(index=False)

    return out + ("\n\n*NPORT-P is filed monthly and covers the whole portfolio — bonds, "
                  "derivatives and short positions included — where a 13F shows only US-listed "
                  "long equity and options, quarterly.*")


@mcp.tool()
def get_institutional_holdings(institution: str, limit: int = 25, source: str = "13F") -> str:
    """
    Latest 13F-HR portfolio for an institutional manager — every reported position,
    largest first. Accepts a ticker (BRK-B) or a raw CIK (1067983).

    Positions are merged across the manager rows a fund files separately: Berkshire
    reports Apple across 12 rows, and reading only the first understates the holding
    threefold.

    Args:
        institution: Ticker or CIK of the filer (e.g. 1067983 for Berkshire Hathaway).
        limit: How many positions to show (default 25).
        source: "13F" for quarterly manager holdings (default), or "NPORT" for a
            registered fund's monthly portfolio, which also covers bonds and
            derivatives that 13F omits entirely.
    """
    try:
        if str(source).upper().startswith("NPORT"):
            return _render_fund_holdings(institution, limit)

        data = edgar_forms.institutional_holdings(institution, limit=limit)
        if not data["holdings"]:
            return f"### {data['institution']}\n\n*No holdings in the latest 13F-HR.*"

        total = data["total_value"] or 1
        rows = [{
            "Issuer": h["issuer"][:30],
            "Value": f"${h['value']:,.0f}" if h["value"] else "—",
            "Weight": f"{(h['value'] or 0) / total * 100:.2f}%",
            "Shares": f"{h['shares']:,.0f}" if h["shares"] else "—",
            "Type": h.get("put_call") or "common",
            "CUSIP": h["cusip"],
        } for h in data["holdings"]]

        table = pd.DataFrame(rows)
        try:
            table_str = table.to_markdown(index=False)
        except Exception:
            table_str = table.to_string(index=False)

        shown = sum(h["value"] or 0 for h in data["holdings"])
        return (f"### 13F Holdings — {data['institution']}\n"
                f"*Quarter ending {data['period']} · filed {data['filed']} · "
                f"{data['positions']} positions · **${data['total_value']:,.0f}** total*\n\n"
                + table_str
                + f"\n\n*Top {len(rows)} shown = {shown / total * 100:.1f}% of reported value. "
                  "13F covers US-listed long equity and options only — it excludes cash, bonds, "
                  "shorts and foreign listings, so it is not the whole portfolio.*")
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error fetching 13F holdings for {institution}: {e}") from e


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

        # Which broker surface is live is the single most consequential line in
        # this report -- it decides whether an approved order spends money.
        paper = webull_client.is_paper_environment()
        out += "**Webull OpenAPI** — primary price feed and broker\n"
        out += (f"* **Environment: {webull_client.environment_label()}** — "
                + ("orders are simulated against Webull's sandbox.\n" if paper else
                   "orders reach the real account. Set `WEBULL_ENVIRONMENT=paper` "
                   "in `.env` to point at the sandbox instead.\n"))
        out += (f"* Credentials configured: {'yes' if webull_client.WEBULL_APP_KEY else 'NO - set WEBULL_APP_KEY/SECRET in .env'}\n"
                f"* Region: `{webull_client.WEBULL_REGION_ID}`\n"
                f"* Pacing: {webull_client.WEBULL_MIN_REQUEST_INTERVAL}s between calls, "
                f"{webull_client.WEBULL_MAX_RETRIES} retries on HTTP 429\n\n")

        out += "**Yahoo Finance** — fallback prices, options, fundamentals\n"
        out += (f"* No key required. Paced at {webull_client.YF_MIN_REQUEST_INTERVAL}s "
                f"between calls, {webull_client.YF_MAX_RETRIES} retries when throttled.\n")
        out += ("* An empty response is classified before it is reported: a "
                f"`{webull_client._CANARY_SYMBOL}` canary distinguishes a rate limit "
                "from a symbol that does not exist.\n")
        delay = webull_client.yahoo_feed_delay("SPY")
        if delay:
            if delay["market_open"]:
                out += (f"* Observed lag right now: **{delay['observed_lag_minutes']:.1f} min** "
                        f"behind {delay['exchange']} (last print {delay['last_print_utc']} UTC).\n")
            else:
                out += (f"* {delay['exchange']} is closed; last print "
                        f"{delay['last_print_utc']} UTC, so the gap is the session, "
                        "not feed delay.\n")
        out += "* Fundamentals are cross-checked against SEC filings where possible.\n\n"

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
        out += f"* Contact header configured: {'yes' if sec['user_agent_configured'] else 'NO'}\n"
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
        icon = "OK " if result["valid"] else "BAD"
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
                          include_latest_data: bool = True,
                          sources: str = "bls,fomc,bea") -> str:
    """
    Upcoming US macroeconomic events with their scheduled date, time and — where the
    release has already happened — the number that came out.

    Three live sources, all free and keyless: the Bureau of Labor Statistics (CPI,
    core CPI, PPI, the employment situation/NFP, JOLTS), the Federal Reserve (FOMC
    rate decisions, flagged when they carry a Summary of Economic Projections), and
    the BEA (PCE — the Fed's target measure — plus GDP and the trade balance).

    Each row carries a reading: the actual print for a release that has happened, or
    the PREVIOUS print for one that has not. There is no consensus/expectations feed
    here — street forecasts are a licensed product — so every comparison is against
    the prior reading and is labelled that way. Do not read a "prior" figure as a
    forecast for the release being waited on.

    Args:
        days_ahead: How far forward to look for scheduled events (default 30).
        days_back: How far back to include recently published releases (default 7).
        include_latest_data: Also report the latest CPI/core CPI/unemployment/payroll prints.
        sources: Comma-separated subset of bls,fomc,bea. Default all three.
    """
    import datetime as _dt
    try:
        wanted = [s.strip().lower() for s in str(sources).split(",") if s.strip()]
        unknown = [s for s in wanted if s not in econ_calendar.CALENDAR_SOURCES]
        if unknown:
            raise ToolError(
                f"Unknown calendar source(s): {', '.join(unknown)}. "
                f"Available: {', '.join(econ_calendar.CALENDAR_SOURCES)}.")

        today = _dt.date.today()
        upcoming, failed = econ_calendar.economic_calendar(
            days_ahead=days_ahead, days_back=days_back, sources=wanted, today=today)

        out = f"### US Economic Calendar — as of {today} ({days_back}d back, {days_ahead}d ahead)\n\n"

        if upcoming:
            rows = []
            for e in upcoming:
                delta = (e["date"] - today).days
                when = "TODAY" if delta == 0 else (f"in {delta}d" if delta > 0 else f"{-delta}d ago")
                rows.append({
                    "Date": str(e["date"]),
                    "Time (ET)": e.get("time_et", ""),
                    "When": when,
                    "Source": e.get("source", ""),
                    "Release": e["release"],
                    "Covers": e.get("reference_period", ""),
                    "Reading": econ_calendar.describe_reading(e) or "—",
                })
            table = pd.DataFrame(rows)
            try:
                out += table.to_markdown(index=False) + "\n"
            except Exception:
                out += table.to_string(index=False) + "\n"
            out += ("\n*Readings are the actual print where published, otherwise the "
                    "**prior** print with its own reference period. No consensus "
                    "forecast is available from any free source, so nothing here is "
                    "an expectation.*\n")
        else:
            out += "*No scheduled releases in this window.*\n"

        if include_latest_data:
            # Isolated from the schedule above. A transient 503 on the data API
            # used to discard a calendar that had already been fetched
            # successfully -- one flaky call taking down an unrelated answer.
            try:
                data = econ_calendar.fetch_bls_series(
                    ["cpi", "core_cpi", "unemployment", "payrolls", "ppi"])
            except Exception as e:
                data = None
                out += f"\n*Latest prints unavailable: {str(e)[:120]}*\n"

        if include_latest_data and data:
            out += "\n**Latest prints**\n\n"
            drows = []
            for key, series in data.items():
                obs = series["observations"]
                if not obs:
                    continue
                o = obs[0]
                u = o.get("change_unit", "%")
                prev = obs[1]["value"] if len(obs) > 1 else None
                headline = econ_calendar.headline(key, series["unit"], o, prev)
                drows.append({
                    "Indicator": series["label"],
                    "Period": f"{o['period']} {o['year']}",
                    # Payrolls as "+0.09% MoM" is right and unreadable; the print
                    # is "+147k". The headline column says it the quoted way.
                    "Print": headline["text"] if headline else "n/a",
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
            out += (f"\n**Warning — this calendar is incomplete ({len(failed)} issue(s)):** "
                    + "; ".join(failed) + "\n")

        status = econ_calendar.source_status()["bls"]
        out += (f"\n*Sources: BLS {status['tier']}, Federal Reserve, BEA"
                + (f" · {status['remaining_today']} of {status['daily_cap']} queries left today"
                   if status["remaining_today"] is not None else "")
                + (f" · {status['note']}" if status["note"] else "") + "*")
        return out
    except Exception as e:
        raise ToolError(f"Error building economic calendar: {e}") from e


def _render_market_series(series, count: int) -> str:
    """Policy rates, the curve and financial conditions — FRED, ECB, BoE."""
    keys = series.split(",") if isinstance(series, str) else list(series)
    keys = [str(k).strip().lower() for k in keys if str(k).strip()]
    if not keys or keys == ["cpi", "core_cpi", "unemployment"]:
        keys = ["fed_funds", "us_2y", "us_10y", "curve_10y_2y", "ecb_deposit", "boe_bank_rate"]

    unknown = [k for k in keys if k not in central_banks.SERIES]
    if unknown:
        raise ToolError(f"Unknown series {unknown}. "
                        f"Available: {', '.join(central_banks.SERIES)}")

    data = central_banks.fetch_series(keys, observations=max(2, count))

    rows, notes, sources = [], [], set()
    for key in keys:
        d = data[key]
        if d.get("error"):
            notes.append(f"`{key}` — {d['error']}")
            continue
        sources.add(d["source"])
        latest = d["latest"]
        suffix = "%" if d["unit"] == "percent" else ""
        rows.append({
            "Series": d["label"][:38],
            "Latest": f"{latest['value']:,.4g}{suffix}" if latest else "—",
            "As of": latest["date"][:10] if latest else "—",
            "Change": (f"{d['change']:+.4g}{suffix}" if d["change"] is not None else "—"),
            "Source": d["source"].upper(),
            "": "stale" if d["stale"] else "",
        })

    if not rows:
        raise ToolError("No series could be retrieved. " + "; ".join(notes))

    table = pd.DataFrame(rows)
    try:
        rendered = table.to_markdown(index=False)
    except Exception:
        rendered = table.to_string(index=False)

    out = "### Policy Rates, Curve & Financial Conditions\n\n" + rendered + "\n"

    stale = [d["label"] for d in data.values() if d.get("stale")]
    if stale:
        out += ("\n**Warning: Stale series:** " + "; ".join(stale) +
                ". A discontinued series keeps returning its last value with nothing "
                "in the response to say so — FRED still serves the Bank of England's "
                "BOERUKM, retired in 2017.\n")
    if notes:
        out += "\n**Unavailable:** " + "; ".join(notes) + "\n"

    out += "\n*Sources: " + ", ".join(
        central_banks.SOURCE_LABELS.get(s, s) for s in sorted(sources)) + ".*"

    status = central_banks.source_status()
    if not central_banks.FRED_API_KEY and "fred" in sources:
        out += f"\n*FRED: {status['fred']['note']}*"
    if "boj_call_rate" in keys:
        out += f"\n*Bank of Japan: {status['boj']['note']}*"
    return out


@mcp.tool()
def get_macro_data(series: str | list[str] = "cpi,core_cpi,unemployment",
                   months: int = 13, source: str = "bls") -> str:
    """
    Historical macroeconomic series from the BLS with month-over-month and
    year-over-year changes — the numbers behind the inflation and labour narrative.

    Args:
        series: Comma-separated keys or a list.
            BLS (default source): cpi, cpi_sa, core_cpi, unemployment, payrolls,
            ppi, avg_hourly_pay, labor_force.
            Central banks and markets (source="markets"): fed_funds,
            fed_target_upper, ecb_deposit, ecb_refi, boe_bank_rate, boe_sonia,
            boj_call_rate, us_2y, us_10y, us_30y, curve_10y_2y, curve_10y_3m,
            breakeven_10y, us_cpi, us_core_pce, us_gdp_real, us_unemployment,
            euro_hicp, dollar_index, vix, hy_spread, mortgage_30y.
        months: How many observations to show per series (default 13).
        source: "bls" for US labour statistics (default), or "markets" for policy
            rates, the yield curve and financial conditions via FRED, the ECB and
            the Bank of England.
    """
    try:
        if str(source).lower().startswith(("market", "fred", "cb", "central")):
            return _render_market_series(series, months)

        keys = series.split(",") if isinstance(series, str) else list(series)
        keys = [str(k).strip().lower() for k in keys if str(k).strip()]

        unknown = [k for k in keys if k not in econ_calendar.BLS_SERIES]
        if unknown:
            # A key that exists under the other source is not "unknown" -- saying
            # so sends the caller looking for a series it already found, and the
            # available-list it gets back does not contain the name it just used.
            elsewhere = [k for k in unknown if k in central_banks.SERIES]
            if elsewhere:
                raise ToolError(
                    f"{', '.join(elsewhere)} " +
                    ("is a markets series" if len(elsewhere) == 1 else "are markets series") +
                    ', not a BLS one. Re-run with source="markets".')
            raise ToolError(f"Unknown series {unknown}. Available from BLS: "
                            f"{', '.join(econ_calendar.BLS_SERIES)}. "
                            f'Policy rates, the curve and financial conditions are '
                            f'under source="markets": {", ".join(central_banks.SERIES)}')

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


@mcp.tool()
def get_updates(symbols: str | list[str] = "", since: str = "24h",
                include_macro: bool = True, move_threshold_pct: float = 3.0) -> str:
    """
    What has actually changed since a point in time: new SEC filings, macro releases
    that have printed, and outsized price moves.

    Every other tool here answers "what is true now". Answering "what is new" without
    this means refetching everything and diffing by hand, which is expensive and easy
    to get wrong. This does the diff against a timestamp you supply.

    Args:
        symbols: Tickers to check, comma-separated or a list. Filings and price moves
                 are per-symbol; leave empty for macro only.
        since: A window ("24h", "3d", "2w"), a date ("2026-08-01"), or an ISO
               timestamp ("2026-08-01T13:30:00Z"). Default 24h.
        include_macro: Include economic releases that printed inside the window.
        move_threshold_pct: Report a price move only if it is at least this large.
    """
    import datetime as _dt
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        try:
            cutoff = market_calendar.parse_since(since, now=now)
        except ValueError as e:
            raise ToolError(str(e)) from e

        span_hours = (now - cutoff).total_seconds() / 3600.0
        span = (f"{span_hours:.0f}h" if span_hours < 48 else f"{span_hours / 24:.1f}d")
        tickers = symbols.split(",") if isinstance(symbols, str) else list(symbols)
        tickers = [t.strip().upper() for t in tickers if str(t).strip()][:12]

        out = (f"### Updates since {cutoff:%Y-%m-%d %H:%M UTC} "
               f"({span} ago; now {now:%Y-%m-%d %H:%M UTC})\n\n")
        found_anything = False
        problems = []

        # --- New filings ------------------------------------------------------
        if tickers:
            rows = []
            for sym in tickers:
                try:
                    for f in econ_calendar.company_filings(sym, limit=30):
                        stamp = (f.get("acceptance") or "").strip()
                        if not stamp:
                            continue
                        try:
                            when = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if when < cutoff:
                            # Filings come back newest-first, so the first one
                            # older than the cutoff ends this symbol.
                            break
                        rows.append({
                            "Symbol": sym,
                            "Accepted (UTC)": stamp.replace("T", " ")[:19],
                            "Form": str(f["form"]),
                            "What": edgar_forms.describe_form(f["form"], f.get("items", "")),
                            "Link": f["url"],
                        })
                except Exception as e:
                    problems.append(f"{sym} filings: {str(e)[:90]}")

            if rows:
                found_anything = True
                rows.sort(key=lambda r: r["Accepted (UTC)"], reverse=True)
                out += f"**New SEC filings ({len(rows)})**\n\n"
                out += pd.DataFrame(rows).astype(str).to_markdown(index=False) + "\n\n"

        # --- Macro that printed ------------------------------------------------
        if include_macro:
            try:
                days_back = max(1, math.ceil(span_hours / 24))
                entries, warns = econ_calendar.economic_calendar(
                    days_ahead=0, days_back=days_back)
                problems.extend(warns)
                printed = [e for e in entries if e["date"] >= cutoff.date()]
                if printed:
                    found_anything = True
                    out += f"**Economic releases in the window ({len(printed)})**\n\n"
                    out += pd.DataFrame([{
                        "Date": str(e["date"]),
                        "Source": e.get("source", ""),
                        "Release": e["release"],
                        "Covers": e.get("reference_period", ""),
                        "Reading": econ_calendar.describe_reading(e) or "—",
                    } for e in printed]).to_markdown(index=False) + "\n\n"
            except Exception as e:
                problems.append(f"macro calendar: {str(e)[:90]}")

        # --- Price moves -------------------------------------------------------
        if tickers:
            moves, skipped = [], []
            # A window under a day needs intraday bars; a longer one needs daily.
            interval = "5" if span_hours <= 8 else ("60" if span_hours <= 48 else "D")
            for sym in tickers:
                try:
                    df, source = webull_client.fetch_data(sym, interval=interval, count=200)
                    stamps = pd.to_datetime(df["time"], errors="coerce", utc=True)
                    inside = df[stamps >= cutoff]
                    if inside.empty or len(df) < 2:
                        skipped.append(sym)
                        continue
                    # The reference is the last bar BEFORE the window, so the move
                    # spans the window rather than starting inside it.
                    first_idx = inside.index[0]
                    prior_idx = df.index[df.index.get_loc(first_idx) - 1] \
                        if df.index.get_loc(first_idx) > 0 else first_idx
                    base = float(df.loc[prior_idx, "close"])
                    last = float(df["close"].iloc[-1])
                    if not base:
                        skipped.append(sym)
                        continue
                    pct = (last / base - 1) * 100
                    age = webull_client.bar_age(df, interval)
                    moves.append({
                        "Symbol": sym,
                        "From": f"{base:,.2f}",
                        "To": f"{last:,.2f}",
                        "Move": f"{pct:+.2f}%",
                        "_abs": abs(pct),
                        "Bars": len(inside),
                        "As of": age["as_of"] if age else "n/a",
                        "Source": source,
                    })
                except Exception as e:
                    problems.append(f"{sym} prices: {str(e)[:90]}")

            notable = [m for m in moves if m["_abs"] >= abs(move_threshold_pct)]
            if notable:
                found_anything = True
                notable.sort(key=lambda m: m["_abs"], reverse=True)
                out += (f"**Price moves of {abs(move_threshold_pct):.1f}% or more "
                        f"({len(notable)} of {len(moves)} priced)**\n\n")
                out += pd.DataFrame([{k: v for k, v in m.items() if k != "_abs"}
                                     for m in notable]).to_markdown(index=False) + "\n\n"
            elif moves:
                quiet = ", ".join(f"{m['Symbol']} {m['Move']}" for m in moves)
                out += (f"**Prices:** nothing moved {abs(move_threshold_pct):.1f}% or more "
                        f"({quiet}).\n\n")
            if skipped:
                problems.append("no bars inside the window for " + ", ".join(skipped))

        if not found_anything:
            out += ("**Nothing new in this window.** That is a result, not an error — "
                    "the sources were queried and returned no filings, releases or "
                    "outsized moves for what was asked.\n\n")

        if problems:
            out += ("**Incomplete — this diff did not cover everything:** "
                    + "; ".join(problems[:8]) + "\n")

        out += ("\n*A filing appears here the moment the SEC accepts it. A price move "
                "is measured from the last bar before the window to the newest bar, so "
                "it is bounded by feed latency, not by this tool.*")
        return out
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error building the update diff: {e}") from e


if __name__ == "__main__":
    mcp.run(transport="stdio")
