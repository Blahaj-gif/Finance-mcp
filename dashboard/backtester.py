import pandas as pd
import numpy as np

# Bars per year, used to annualise the Sharpe ratio. Hardcoding 252 meant an
# hourly or 15-minute backtest was annualised as if each bar were a trading day,
# understating Sharpe by ~2.5x on H1 and ~5x on M15.
BARS_PER_YEAR = {
    "M1": 252 * 390,
    "M5": 252 * 78,
    "M15": 252 * 26,
    "M30": 252 * 13,
    "H1": 252 * 6.5,
    "D": 252,
    "W": 52,
    "M": 12,
}
TRADING_DAYS_PER_YEAR = 252


def bars_per_year_for(interval: str) -> float:
    return float(BARS_PER_YEAR.get(str(interval).upper(), TRADING_DAYS_PER_YEAR))


def run_backtest(df: pd.DataFrame, consensus_col: str = "consensus_score",
                 buy_threshold: float = 1.0, sell_threshold: float = -1.0,
                 transaction_fee: float = 0.0015, interval: str = "D") -> dict:
    """
    Simulates a long-only strategy driven by the consensus score.

      - Enter long (1) when consensus >= buy_threshold
      - Flat (0)      when consensus <= sell_threshold
      - Otherwise hold the current position (hysteresis)

    Execution model: the signal computed from bar *t*'s close is acted on at
    that same close, so the strategy earns the move from close t to close t+1.
    That is what `position.shift(1)` encodes. It contains no look-ahead -- the
    signal uses only data up to and including bar t -- but it does assume you
    can transact on the closing print, which is mildly optimistic.

    A NaN score (indicator warm-up) is not a signal: the position is simply
    held, so the strategy stays flat until the indicators mean something.

    Args:
        interval: Bar interval, used to annualise the Sharpe ratio correctly.
    """
    df = df.copy().reset_index(drop=True)

    if consensus_col not in df.columns:
        raise KeyError(f"Backtest needs a '{consensus_col}' column; got {list(df.columns)}")
    if len(df) < 2:
        raise ValueError(f"Need at least 2 bars to backtest, got {len(df)}")

    # ---- Positions -----------------------------------------------------
    positions = []
    current_pos = 0
    for score in df[consensus_col]:
        if pd.notna(score):
            if score >= buy_threshold:
                current_pos = 1
            elif score <= sell_threshold:
                current_pos = 0
        positions.append(current_pos)

    df["position"] = positions
    df["exec_position"] = df["position"].shift(1).fillna(0)

    # ---- Returns -------------------------------------------------------
    df["asset_returns"] = df["close"].pct_change().fillna(0)
    df["strategy_returns"] = df["exec_position"] * df["asset_returns"]

    df["position_changes"] = df["exec_position"].diff().abs().fillna(0)
    df["transaction_costs"] = df["position_changes"] * transaction_fee
    df["strategy_returns"] = df["strategy_returns"] - df["transaction_costs"]

    df["cum_asset_returns"] = (1 + df["asset_returns"]).cumprod() - 1
    df["cum_strategy_returns"] = (1 + df["strategy_returns"]).cumprod() - 1

    total_asset_return = df["cum_asset_returns"].iloc[-1] * 100
    total_strat_return = df["cum_strategy_returns"].iloc[-1] * 100

    # ---- Sharpe (annualised for this bar size, rf = 0) ------------------
    periods = bars_per_year_for(interval)
    mean_ret = df["strategy_returns"].mean()
    std_ret = df["strategy_returns"].std()
    sharpe_ratio = (mean_ret / std_ret) * np.sqrt(periods) if std_ret > 0 else 0.0

    # ---- Drawdown, for the strategy and for buy & hold ------------------
    def _max_drawdown(returns: pd.Series) -> float:
        equity = (1 + returns).cumprod()
        return float(((equity - equity.cummax()) / equity.cummax()).min() * 100)

    max_drawdown = _max_drawdown(df["strategy_returns"])
    asset_max_drawdown = _max_drawdown(df["asset_returns"])

    # ---- Trades --------------------------------------------------------
    # Walk the executed position and pair entries with exits, so the trade
    # count and the win rate are derived from the same list. Previously the
    # count was `changes / 2` (which truncates an unclosed final trade) and was
    # then floored at 1 by `max(1, ...)`, reporting a trade for a strategy that
    # never traded.
    trades = []
    entry_price = None
    exec_pos = df["exec_position"].to_numpy()
    close = df["close"].to_numpy()

    for i in range(len(df)):
        if exec_pos[i] == 1 and entry_price is None:
            # Position was taken at the previous close, which is the price the
            # bar-i return is measured from.
            entry_price = close[i - 1] if i > 0 else close[i]
        elif exec_pos[i] == 0 and entry_price is not None:
            exit_price = close[i - 1] if i > 0 else close[i]
            trades.append({"entry": entry_price, "exit": exit_price, "open": False,
                           "ret": (exit_price - entry_price) / entry_price if entry_price else 0.0})
            entry_price = None

    open_trade = None
    if entry_price is not None:
        exit_price = close[-1]
        open_trade = {"entry": entry_price, "exit": exit_price, "open": True,
                      "ret": (exit_price - entry_price) / entry_price if entry_price else 0.0}
        trades.append(open_trade)

    closed = [t for t in trades if not t["open"]]
    wins = sum(1 for t in trades if t["ret"] > 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0
    avg_win = float(np.mean([t["ret"] for t in trades if t["ret"] > 0]) * 100) if wins else 0.0
    losses = [t["ret"] for t in trades if t["ret"] <= 0]
    avg_loss = float(np.mean(losses) * 100) if losses else 0.0

    gross_win = sum(t["ret"] for t in trades if t["ret"] > 0)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    # Bars the strategy actually held a position -- a 900% return from three
    # days of exposure is a different claim from the same return held throughout.
    exposure = float((df["exec_position"] > 0).mean() * 100)

    return {
        "df": df[["time", "close", "position", "cum_asset_returns", "cum_strategy_returns"]],
        "trades": trades,
        "metrics": {
            "total_asset_return": total_asset_return,
            "total_strategy_return": total_strat_return,
            "sharpe_ratio": float(sharpe_ratio),
            "max_drawdown": max_drawdown,
            "asset_max_drawdown": asset_max_drawdown,
            "total_trades": len(trades),
            "closed_trades": len(closed),
            "open_trade": open_trade is not None,
            "win_rate": float(win_rate),
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "profit_factor": float(profit_factor),
            "exposure_pct": exposure,
            "bars": int(len(df)),
            "interval": str(interval).upper(),
        }
    }
