import math

import pandas as pd
import pytest

from trading_bot.backtest import run_backtest
from trading_bot.strategy import RuleStrategy, Strategy, StrategyDefinition


def frame(closes: list[float], opens: list[float] | None = None, highs: list[float] | None = None,
          lows: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    opens = opens or closes
    highs = highs or [max(o, c) for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) for o, c in zip(opens, closes)]
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": [1000] * n},
                        index=idx, dtype=float)


class ScriptedStrategy(Strategy):
    name = "scripted"

    def __init__(self, buys: set[int], sells: set[int], stop_loss_pct: float = 0.0,
                 take_profit_pct: float = 0.0) -> None:
        self._buys, self._sells = buys, sells
        self.stop_loss_pct, self.take_profit_pct = stop_loss_pct, take_profit_pct

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        buy = pd.Series([i in self._buys for i in range(len(df))], index=df.index)
        sell = pd.Series([i in self._sells for i in range(len(df))], index=df.index)
        return pd.DataFrame({"buy": buy, "sell": sell})


def test_fills_happen_at_next_open() -> None:
    df = frame(closes=[10, 10, 12, 12, 15, 15], opens=[10, 11, 12, 13, 14, 16])
    result = run_backtest(ScriptedStrategy(buys={0}, sells={3}), df, "X", initial_cash=1000)
    assert result.num_trades == 1
    t = result.trades[0]
    assert t.entry_price == 11  # open of bar 1
    assert t.exit_price == 14   # open of bar 4
    assert t.quantity == pytest.approx(1000 / 11)
    assert t.pnl == pytest.approx((14 - 11) * 1000 / 11)
    assert t.exit_reason == "signal" and t.bars_held == 3
    assert result.final_equity == pytest.approx(1000 / 11 * 14)
    assert result.total_return_pct == pytest.approx((14 / 11 - 1) * 100)
    assert result.buy_markers.iloc[1] and result.sell_markers.iloc[4]
    assert result.equity_curve.iloc[0] == 1000


def test_stop_loss_and_take_profit_exits() -> None:
    df = frame(closes=[10, 10, 10, 10], opens=[10, 10, 10, 10], highs=[10, 10, 10, 10], lows=[10, 10, 8, 10])
    result = run_backtest(ScriptedStrategy(buys={0}, sells=set(), stop_loss_pct=10), df, initial_cash=100)
    assert result.num_trades == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_price == 9.0
    df = frame(closes=[10, 10, 10, 10], opens=[10, 10, 10, 10], highs=[10, 10, 13, 10], lows=[10, 10, 10, 10])
    result = run_backtest(ScriptedStrategy(buys={0}, sells=set(), take_profit_pct=20), df, initial_cash=100)
    assert result.trades[0].exit_reason == "take_profit"
    assert result.trades[0].exit_price == 12.0
    assert result.win_rate == 100.0


def test_gap_through_stop_fills_at_open() -> None:
    df = frame(closes=[10, 10, 7, 7], opens=[10, 10, 7, 7], highs=[10, 10, 7, 7], lows=[10, 10, 7, 7])
    result = run_backtest(ScriptedStrategy(buys={0}, sells=set(), stop_loss_pct=10), df, initial_cash=100)
    assert result.trades[0].exit_price == 7.0  # gapped below the stop: filled at the open, not the stop


def test_open_position_is_liquidated_at_end() -> None:
    df = frame(closes=[10, 11, 12])
    result = run_backtest(ScriptedStrategy(buys={0}, sells=set()), df, initial_cash=100)
    assert result.num_trades == 1 and result.trades[0].exit_reason == "end_of_data"
    assert result.final_equity == pytest.approx(100 / 11 * 12)


def test_commission_and_position_size() -> None:
    df = frame(closes=[10, 10, 10, 10], opens=[10, 10, 10, 10])
    result = run_backtest(ScriptedStrategy(buys={0}, sells={1}), df, initial_cash=1000, position_size=0.5, commission=1.0)
    t = result.trades[0]
    assert t.quantity == pytest.approx((500 - 1) / 10)
    assert t.pnl == pytest.approx(-2.0)
    assert result.final_equity == pytest.approx(998.0)
    assert result.profit_factor == 0.0


def test_no_signals_means_no_trades(ohlcv: pd.DataFrame) -> None:
    result = run_backtest(ScriptedStrategy(set(), set()), ohlcv, initial_cash=100)
    assert result.num_trades == 0 and result.final_equity == 100
    assert result.max_drawdown_pct == 0 and result.win_rate == 0 and result.sharpe_ratio == 0
    summary = result.summary()
    assert summary["trades"] == 0 and summary["profit_factor"] == 0.0


def test_rule_strategy_backtest_runs(ohlcv: pd.DataFrame) -> None:
    s = RuleStrategy(StrategyDefinition(name="ma", buy_rule="crossover(sma(5), sma(20))",
                                        sell_rule="crossunder(sma(5), sma(20))"))
    result = run_backtest(s, ohlcv, "AAPL", 10_000)
    assert result.num_trades > 0
    assert len(result.equity_curve) == len(ohlcv)
    assert result.max_drawdown_pct <= 0
    assert math.isfinite(result.sharpe_ratio)
    summary = result.summary()
    assert summary["symbol"] == "AAPL" and summary["strategy"] == "ma"


def test_backtest_argument_validation(ohlcv: pd.DataFrame) -> None:
    with pytest.raises(ValueError):
        run_backtest(ScriptedStrategy(set(), set()), ohlcv.iloc[:1])
    with pytest.raises(ValueError):
        run_backtest(ScriptedStrategy(set(), set()), ohlcv, position_size=0)
