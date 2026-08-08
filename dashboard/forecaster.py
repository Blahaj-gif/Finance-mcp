import pandas as pd
import numpy as np

def run_ar_forecast(df: pd.DataFrame, forecast_steps: int = 15, ar_order: int = 3) -> dict:
    """
    Fits an Autoregressive AR(p) model on log returns, forecasts future log returns,
    and reconstructs the projected price path with 68% and 95% confidence intervals.
    """
    close = df["close"].values
    if len(close) < ar_order + 10:
        raise ValueError("Insufficient data to fit Autoregressive model.")
        
    # Calculate log returns
    log_returns = np.log(close[1:] / close[:-1])
    n = len(log_returns)
    
    # Construct design matrix for OLS estimation of AR(p)
    # Y = X * beta
    Y = log_returns[ar_order:]
    X = np.ones((n - ar_order, ar_order + 1))
    
    for i in range(ar_order):
        X[:, i + 1] = log_returns[ar_order - 1 - i : n - 1 - i]
        
    # Fit OLS: beta = (X^T * X)^-1 * X^T * Y
    try:
        beta = np.linalg.solve(X.T @ X, X.T @ Y)
    except np.linalg.LinAlgError:
        # Fallback to simple mean if matrix is singular
        beta = np.zeros(ar_order + 1)
        beta[0] = log_returns.mean()
        
    # Calculate residuals and standard error
    residuals = Y - (X @ beta)
    sigma = residuals.std()
    if sigma == 0:
        sigma = 1e-5
        
    # Forecast future returns iteratively
    forecast_returns = []
    lags = list(log_returns[-ar_order:])
    
    for _ in range(forecast_steps):
        # Predict next return: constant + sum(beta_i * lag_i)
        pred = beta[0]
        for i in range(ar_order):
            pred += beta[i + 1] * lags[-(i + 1)]
        forecast_returns.append(pred)
        # Update lags
        lags.append(pred)
        
    # Convert forecasted returns back to price levels
    last_price = close[-1]
    forecast_prices = []
    current_log_price = np.log(last_price)
    
    upper_68 = []
    lower_68 = []
    upper_95 = []
    lower_95 = []
    
    accumulated_ret = 0.0
    
    for k in range(1, forecast_steps + 1):
        accumulated_ret += forecast_returns[k - 1]
        pred_price = last_price * np.exp(accumulated_ret)
        forecast_prices.append(pred_price)
        
        # Volatility envelope (standard error grows by sqrt(k))
        k_std = np.sqrt(k) * sigma
        
        upper_68.append(pred_price * np.exp(k_std))
        lower_68.append(pred_price * np.exp(-k_std))
        upper_95.append(pred_price * np.exp(1.96 * k_std))
        lower_95.append(pred_price * np.exp(-1.96 * k_std))
        
    # Create output timeline
    # Generate future timestamps
    last_time = pd.to_datetime(df["time"].iloc[-1])
    # Estimate time delta from data
    try:
        t_diff = pd.to_datetime(df["time"].iloc[-1]) - pd.to_datetime(df["time"].iloc[-2])
    except Exception:
        t_diff = pd.Timedelta(days=1)
        
    future_times = []
    current_time = last_time
    for _ in range(forecast_steps):
        current_time += t_diff
        # Handle weekends if interval is daily
        if t_diff.days == 1:
            if current_time.weekday() == 5: # Saturday
                current_time += pd.Timedelta(days=2) # skip to Monday
        future_times.append(current_time.strftime("%Y-%m-%d %H:%M:%S"))
        
    return {
        "time": future_times,
        "forecast_price": forecast_prices,
        "upper_68": upper_68,
        "lower_68": lower_68,
        "upper_95": upper_95,
        "lower_95": lower_95
    }
