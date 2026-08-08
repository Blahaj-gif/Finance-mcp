import pandas as pd
import numpy as np

def run_backtest(df: pd.DataFrame, consensus_col: str = "consensus_score", buy_threshold: float = 1.0, sell_threshold: float = -1.0, transaction_fee: float = 0.0015) -> dict:
    """
    Simulates a trading strategy based on the consensus score.
    Long-only strategy:
      - Enter Long (1) when consensus >= buy_threshold
      - Exit (0) when consensus <= sell_threshold
    
    Includes 1-bar execution delay to eliminate look-ahead bias (signals generated at Close t are executed at Close t+1).
    """
    df = df.copy()
    
    # Calculate positions
    positions = []
    current_pos = 0
    
    for score in df[consensus_col]:
        if score >= buy_threshold:
            current_pos = 1
        elif score <= sell_threshold:
            current_pos = 0
        positions.append(current_pos)
        
    df["position"] = positions
    # Shift position by 1 to represent executing the trade on the NEXT bar
    df["exec_position"] = df["position"].shift(1).fillna(0)
    
    # Asset returns
    df["asset_returns"] = df["close"].pct_change().fillna(0)
    
    # Strategy returns
    df["strategy_returns"] = df["exec_position"] * df["asset_returns"]
    
    # Transaction costs (applied when position changes)
    df["position_changes"] = df["exec_position"].diff().abs().fillna(0)
    df["transaction_costs"] = df["position_changes"] * transaction_fee
    df["strategy_returns"] = df["strategy_returns"] - df["transaction_costs"]
    
    # Cumulative returns
    df["cum_asset_returns"] = (1 + df["asset_returns"]).cumprod() - 1
    df["cum_strategy_returns"] = (1 + df["strategy_returns"]).cumprod() - 1
    
    # Metrics calculation
    total_asset_return = df["cum_asset_returns"].iloc[-1] * 100
    total_strat_return = df["cum_strategy_returns"].iloc[-1] * 100
    
    # Sharpe Ratio (annualized, assuming 252 trading days)
    mean_ret = df["strategy_returns"].mean()
    std_ret = df["strategy_returns"].std()
    if std_ret > 0:
        sharpe_ratio = (mean_ret / std_ret) * np.sqrt(252)
    else:
        sharpe_ratio = 0.0
        
    # Max Drawdown
    equity_curve = (1 + df["strategy_returns"]).cumprod()
    running_max = equity_curve.cummax()
    drawdowns = (equity_curve - running_max) / running_max
    max_drawdown = drawdowns.min() * 100
    
    # Trade statistics
    trade_signals = df["position_changes"]
    total_trades = int(trade_signals.sum() / 2) # Enter + Exit = 1 full trade
    
    # Calculate win rate based on individual trades
    trade_returns = []
    entry_price = 0
    in_trade = False
    
    for i in range(len(df)):
        if df["exec_position"].iloc[i] == 1 and not in_trade:
            # Enter trade
            entry_price = df["close"].iloc[i-1] # executed at previous close
            in_trade = True
        elif df["exec_position"].iloc[i] == 0 and in_trade:
            # Exit trade
            exit_price = df["close"].iloc[i-1]
            trade_ret = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
            trade_returns.append(trade_ret)
            in_trade = False
            
    # If still in trade at the end, close it out
    if in_trade:
        exit_price = df["close"].iloc[-1]
        trade_ret = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
        trade_returns.append(trade_ret)
        
    wins = sum(1 for r in trade_returns if r > 0)
    win_rate = (wins / len(trade_returns) * 100) if trade_returns else 0.0
    
    return {
        "df": df[["time", "close", "position", "cum_asset_returns", "cum_strategy_returns"]],
        "metrics": {
            "total_asset_return": total_asset_return,
            "total_strategy_return": total_strat_return,
            "sharpe_ratio": float(sharpe_ratio),
            "max_drawdown": float(max_drawdown),
            "total_trades": max(1, total_trades),
            "win_rate": float(win_rate)
        }
    }
