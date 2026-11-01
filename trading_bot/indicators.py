"""Technical indicators implemented on top of pandas.

Every function accepts and returns :class:`pandas.Series` so the same code path
serves live evaluation (look at the last value) and backtesting (look at the
whole series).  Conventions follow the widely used TA-Lib definitions so that
results line up with what traders see on charting platforms.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def _period(n) -> int:
    if isinstance(n, (pd.Series, pd.DataFrame, np.ndarray)):
        raise ValueError("period must be a number, not a series")
    try:
        n = int(n)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"period must be an integer, got {n!r}") from exc
    if n < 1:
        raise ValueError(f"period must be >= 1, got {n}")
    return n


def sma(series: pd.Series, period) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=_period(period), min_periods=_period(period)).mean()


def ema(series: pd.Series, period) -> pd.Series:
    """Exponential moving average (span based, no adjustment)."""
    p = _period(period)
    return series.ewm(span=p, adjust=False, min_periods=p).mean()


def wma(series: pd.Series, period) -> pd.Series:
    """Linearly weighted moving average."""
    p = _period(period)
    weights = np.arange(1, p + 1, dtype=float)
    return series.rolling(p).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing seeded with a simple average of the first ``period`` values (as TA-Lib does)."""
    p = _period(period)
    values = series.to_numpy(dtype=float)
    valid = np.flatnonzero(~np.isnan(values))
    out = pd.Series(np.nan, index=series.index)
    if len(valid) < p:
        return out
    first = valid[0]
    seed_pos = first + p - 1
    init = series.copy()
    init.iloc[:seed_pos] = np.nan
    init.iloc[seed_pos] = float(np.nanmean(values[first:seed_pos + 1]))
    return init.ewm(alpha=1.0 / p, adjust=False).mean()


def rsi(series: pd.Series, period=14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing (TA-Lib compatible)."""
    p = _period(period)
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = wilder_smooth(gain, p)
    avg_loss = wilder_smooth(loss, p)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - (100.0 / (1.0 + rs))
    # When there are no losses RS is infinite -> RSI is 100 by definition.
    out = out.where(avg_loss != 0, 100.0)
    out[avg_gain.isna() | avg_loss.isna()] = np.nan
    return out


def macd(series: pd.Series, fast=12, slow=26, signal=9) -> pd.DataFrame:
    """MACD line, signal line and histogram."""
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    line = fast_ema - slow_ema
    sig = ema(line.dropna(), signal).reindex(series.index)
    hist = line - sig
    return pd.DataFrame({"macd": line, "signal": sig, "hist": hist})


def bollinger(series: pd.Series, period=20, num_std=2.0) -> pd.DataFrame:
    """Bollinger bands (population std, matching TA-Lib)."""
    p = _period(period)
    mid = sma(series, p)
    std = series.rolling(p, min_periods=p).std(ddof=0)
    return pd.DataFrame({
        "upper": mid + float(num_std) * std,
        "middle": mid,
        "lower": mid - float(num_std) * std,
    })


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1)
    # The first bar has no previous close, so its true range is undefined (as in TA-Lib).
    return ranges.max(axis=1, skipna=False)


def atr(df: pd.DataFrame, period=14) -> pd.Series:
    """Average True Range with Wilder's smoothing."""
    return wilder_smooth(true_range(df), _period(period))


def stochastic(df: pd.DataFrame, k_period=14, d_period=3) -> pd.DataFrame:
    """Stochastic oscillator (%K and its SMA %D)."""
    k = _period(k_period)
    low_min = df["low"].rolling(k, min_periods=k).min()
    high_max = df["high"].rolling(k, min_periods=k).max()
    span = (high_max - low_min).replace(0, np.nan)
    pct_k = 100.0 * (df["close"] - low_min) / span
    pct_d = sma(pct_k, d_period)
    return pd.DataFrame({"k": pct_k, "d": pct_d})


def vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative volume weighted average price over the whole window."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    return (typical * df["volume"]).cumsum() / cum_vol


def obv(df: pd.DataFrame) -> pd.Series:
    """On-balance volume."""
    direction = np.sign(df["close"].diff()).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def highest(series: pd.Series, period) -> pd.Series:
    p = _period(period)
    return series.rolling(p, min_periods=p).max()


def lowest(series: pd.Series, period) -> pd.Series:
    p = _period(period)
    return series.rolling(p, min_periods=p).min()


def change(series: pd.Series, periods=1) -> pd.Series:
    return series.diff(_period(periods))


def pct_change(series: pd.Series, periods=1) -> pd.Series:
    """Percentage change expressed in percent (1.5 == +1.5%)."""
    return series.pct_change(_period(periods)) * 100.0


def shift(series: pd.Series, periods=1) -> pd.Series:
    return series.shift(_period(periods))


def crossover(a: pd.Series, b) -> pd.Series:
    """True on the bar where ``a`` crosses above ``b``."""
    b = _align(b, a)
    prev = (a.shift(1) <= b.shift(1))
    now = a > b
    return (prev & now).fillna(False).astype(bool)


def crossunder(a: pd.Series, b) -> pd.Series:
    """True on the bar where ``a`` crosses below ``b``."""
    b = _align(b, a)
    prev = (a.shift(1) >= b.shift(1))
    now = a < b
    return (prev & now).fillna(False).astype(bool)


def _align(value, like: pd.Series) -> pd.Series:
    if isinstance(value, pd.Series):
        return value
    return pd.Series(float(value), index=like.index)


def validate_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure ``df`` is a well-formed OHLCV frame sorted by time."""
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV frame is missing columns: {', '.join(missing)}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("OHLCV frame must be indexed by a DatetimeIndex")
    out = df.loc[:, list(OHLCV_COLUMNS)].astype(float)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out
