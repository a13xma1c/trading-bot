"""Event-driven backtester for strategies.

The simulation is intentionally conservative:

* long only, one position per symbol;
* orders generated on bar *t* are filled at the *open* of bar ``t + 1``
  (no look-ahead);
* stop-loss / take-profit levels are checked against each bar's low/high and
  filled at the level itself (the stop is checked first - pessimistic);
* a fixed commission is charged per fill.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .strategy.base import Strategy


@dataclass
class BacktestTrade:
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    return_pct: float
    exit_reason: str
    bars_held: int


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    initial_cash: float
    final_equity: float
    equity_curve: pd.Series
    trades: list[BacktestTrade] = field(default_factory=list)
    buy_markers: pd.Series = field(default_factory=lambda: pd.Series(dtype=bool))
    sell_markers: pd.Series = field(default_factory=lambda: pd.Series(dtype=bool))

    # ------------------------------------------------------------ statistics
    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.initial_cash - 1.0) * 100.0

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return 100.0 * sum(1 for t in self.trades if t.pnl > 0) / len(self.trades)

    @property
    def max_drawdown_pct(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        running_max = self.equity_curve.cummax()
        drawdown = (self.equity_curve / running_max - 1.0) * 100.0
        return float(drawdown.min())

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        if losses == 0:
            return math.inf if gains > 0 else 0.0
        return gains / losses

    @property
    def avg_trade_return_pct(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([t.return_pct for t in self.trades]))

    @property
    def sharpe_ratio(self) -> float:
        """Annualised Sharpe of per-bar returns (assumes ~252*78 five-minute bars per year when intraday)."""
        if len(self.equity_curve) < 3:
            return 0.0
        rets = self.equity_curve.pct_change().dropna()
        if rets.std() == 0 or rets.empty:
            return 0.0
        idx = self.equity_curve.index
        step = (idx[1] - idx[0]).total_seconds() if len(idx) > 1 else 86400
        bars_per_year = 252 * max(1.0, min(6.5 * 3600 / max(step, 1), 390))
        return float(rets.mean() / rets.std() * math.sqrt(bars_per_year))

    @property
    def buy_and_hold_return_pct(self) -> float:
        return self._bnh

    _bnh: float = 0.0

    def summary(self) -> dict[str, float | int | str]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "trades": self.num_trades,
            "total_return_pct": round(self.total_return_pct, 2),
            "buy_and_hold_pct": round(self.buy_and_hold_return_pct, 2),
            "win_rate_pct": round(self.win_rate, 1),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "profit_factor": round(self.profit_factor, 2) if math.isfinite(self.profit_factor) else "inf",
            "avg_trade_pct": round(self.avg_trade_return_pct, 2),
            "sharpe": round(self.sharpe_ratio, 2),
            "final_equity": round(self.final_equity, 2),
        }


def run_backtest(strategy: Strategy, df: pd.DataFrame, symbol: str = "", initial_cash: float = 10_000.0,
                 position_size: float = 1.0, commission: float = 0.0) -> BacktestResult:
    if df is None or len(df) < 2:
        raise ValueError("Need at least two bars to run a backtest")
    if not 0 < position_size <= 1:
        raise ValueError("position_size must be in (0, 1]")
    frame = strategy.signals(df)
    buy = frame["buy"].to_numpy(dtype=bool)
    sell = frame["sell"].to_numpy(dtype=bool)
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    times = df.index

    cash = float(initial_cash)
    qty = 0.0
    entry_price = 0.0
    entry_i = -1
    stop = take = None
    pending: str | None = None  # order to execute at the next open
    equity = np.empty(len(df))
    trades: list[BacktestTrade] = []
    buy_marks = np.zeros(len(df), dtype=bool)
    sell_marks = np.zeros(len(df), dtype=bool)

    def close_position(i: int, price: float, reason: str) -> None:
        nonlocal cash, qty, entry_price, entry_i, stop, take
        proceeds = qty * price - commission
        pnl = proceeds - (qty * entry_price + commission)
        trades.append(BacktestTrade(times[entry_i].to_pydatetime(), times[i].to_pydatetime(), entry_price, price,
                                    qty, pnl, (price / entry_price - 1.0) * 100.0, reason, i - entry_i))
        cash += proceeds
        qty = 0.0
        stop = take = None
        sell_marks[i] = True

    for i in range(len(df)):
        # 1. Execute the order queued on the previous bar at this bar's open.
        if pending == "BUY" and qty == 0.0:
            price = opens[i]
            budget = cash * position_size - commission
            if price > 0 and budget > 0:
                qty = budget / price
                cash -= qty * price + commission
                entry_price = price
                entry_i = i
                stop = price * (1 - strategy.stop_loss_pct / 100.0) if strategy.stop_loss_pct else None
                take = price * (1 + strategy.take_profit_pct / 100.0) if strategy.take_profit_pct else None
                buy_marks[i] = True
        elif pending == "SELL" and qty > 0.0:
            close_position(i, opens[i], "signal")
        pending = None

        # 2. Intrabar protective exits.
        if qty > 0.0:
            if stop is not None and lows[i] <= stop:
                close_position(i, min(stop, opens[i]), "stop_loss")
            elif take is not None and highs[i] >= take:
                close_position(i, max(take, opens[i]), "take_profit")

        # 3. Queue orders from this bar's signals.
        if qty == 0.0 and buy[i]:
            pending = "BUY"
        elif qty > 0.0 and sell[i]:
            pending = "SELL"

        equity[i] = cash + qty * closes[i]

    # Liquidate at the last close so open positions are reflected in the trade list.
    if qty > 0.0:
        close_position(len(df) - 1, closes[-1], "end_of_data")
        equity[-1] = cash

    result = BacktestResult(symbol, strategy.name, float(initial_cash), float(equity[-1]),
                            pd.Series(equity, index=times, name="equity"), trades,
                            pd.Series(buy_marks, index=times), pd.Series(sell_marks, index=times))
    result._bnh = (closes[-1] / opens[0] - 1.0) * 100.0 if opens[0] else 0.0
    return result
