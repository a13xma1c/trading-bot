"""Execution simulation: slippage / commission models, order validation and dry-run results."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from trading_bot.broker import PaperBroker
from trading_bot.config import ExecutionConfig
from trading_bot.execution import ExecutionSimulator, MarketSnapshot, OrderRequest


def bars(n: int = 60, price: float = 100.0, volume: float = 10_000.0) -> pd.DataFrame:
    idx = pd.date_range("2024-03-04 14:30", periods=n, freq="5min", tz="UTC")
    close = price + np.sin(np.arange(n) / 5.0)  # gentle wiggle -> small but non-zero volatility
    return pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": volume}, index=idx)


def test_order_request_validation() -> None:
    assert OrderRequest(" aapl ", "buy", 1).validate() is None
    assert OrderRequest("aapl", "buy", 1).symbol == "AAPL" and OrderRequest("aapl", "buy", 1).side == "BUY"
    assert OrderRequest("", "BUY", 1).validate() == "Invalid symbol"
    assert OrderRequest("A$", "BUY", 1).validate() == "Invalid symbol"
    assert OrderRequest("AAPL", "HOLD", 1).validate() == "Side must be BUY or SELL"
    assert OrderRequest("AAPL", "BUY", 1, "stop").validate() == "Order type must be market or limit"
    assert OrderRequest("AAPL", "BUY", 0).validate() == "Quantity must be positive"
    assert OrderRequest("AAPL", "BUY", math.nan).validate() == "Quantity must be positive"
    assert "limit price" in (OrderRequest("AAPL", "BUY", 1, "limit").validate() or "")
    assert "Stop loss" in (OrderRequest("AAPL", "BUY", 1, stop_loss_pct=120).validate() or "")
    assert OrderRequest("AAPL", "BUY", 1, "limit", 99.0, 2.0, 5.0).validate() is None


def test_market_snapshot_from_bars() -> None:
    snap = MarketSnapshot.from_bars(bars())
    assert snap.price == pytest.approx(100 + math.sin(59 / 5.0))
    assert snap.avg_volume == 10_000 and snap.bars == 50  # default lookback
    assert snap.volatility is not None and 0 < snap.volatility < 0.01
    assert snap.bar_range_pct == pytest.approx(1.0 / 100, rel=0.05)
    assert MarketSnapshot.from_bars(bars(), price=123.0).price == 123.0
    assert MarketSnapshot.from_bars(None, price=5.0) == MarketSnapshot(price=5.0)
    with pytest.raises(ValueError):
        MarketSnapshot.from_bars(None)
    empty = MarketSnapshot.from_bars(pd.DataFrame(), price=1.0)
    assert empty.bars == 0 and empty.avg_volume is None


def test_commission_models() -> None:
    assert ExecutionSimulator(ExecutionConfig()).commission(100, 10_000) == 0.0
    flat = ExecutionSimulator(ExecutionConfig(commission_per_order=1.0))
    assert flat.commission(1, 10) == 1.0
    per_share = ExecutionSimulator(ExecutionConfig(commission_per_share=0.005, min_commission=1.0))
    assert per_share.commission(10, 1000) == 1.0 and per_share.commission(1000, 1000) == 5.0
    pct = ExecutionSimulator(ExecutionConfig(commission_pct=0.1))
    assert pct.commission(1, 10_000) == pytest.approx(10.0)
    combined = ExecutionSimulator(ExecutionConfig(commission_per_order=1, commission_per_share=0.01, commission_pct=0.1))
    assert combined.commission(100, 10_000) == pytest.approx(1 + 1 + 10)


def test_slippage_models() -> None:
    snap = MarketSnapshot.from_bars(bars())
    none = ExecutionSimulator(ExecutionConfig(slippage_model="none"))
    assert none.fill_price("BUY", 100, snap) == snap.price and none.fill_price("SELL", 100, snap) == snap.price
    fixed = ExecutionSimulator(ExecutionConfig(slippage_model="fixed", slippage_bps=10))
    assert fixed.fill_price("BUY", 100, snap) == pytest.approx(snap.price * 1.001)
    assert fixed.fill_price("SELL", 100, snap) == pytest.approx(snap.price * 0.999)
    volume = ExecutionSimulator(ExecutionConfig(slippage_model="volume", slippage_bps=10, impact_coefficient=0.5))
    small = volume.slippage_fraction(10, snap)
    large = volume.slippage_fraction(10_000, snap)
    assert 0.001 < small < large  # impact grows with the square root of the participation
    assert large - 0.001 == pytest.approx((small - 0.001) * math.sqrt(1000), rel=1e-6)
    # Without volume information the volume model degrades to the fixed spread.
    assert volume.slippage_fraction(10_000, MarketSnapshot(price=100.0)) == pytest.approx(0.001)
    # Without return volatility the bar range stands in.
    no_vol = MarketSnapshot(price=100.0, avg_volume=1000.0, volatility=None, bar_range_pct=0.02)
    assert volume.slippage_fraction(1000, no_vol) == pytest.approx(0.001 + 0.5 * 0.01)


def test_simulate_buy_sell_and_rejections() -> None:
    cfg = ExecutionConfig(slippage_model="fixed", slippage_bps=10, commission_per_order=1.0, max_participation_pct=5)
    sim = ExecutionSimulator(cfg)
    broker = PaperBroker(10_000)
    snap = MarketSnapshot.from_bars(bars(volume=1000))
    ref = snap.price

    buy = sim.simulate(OrderRequest("AAPL", "BUY", 10, stop_loss_pct=5, take_profit_pct=10), snap, broker, max_positions=5)
    assert buy.accepted and buy.order_type == "market" and buy.marketable
    assert buy.fill_price == pytest.approx(ref * 1.001) and buy.slippage_bps == pytest.approx(10)
    assert buy.slippage_cost == pytest.approx(10 * ref * 0.001) and buy.commission == 1.0
    assert buy.total_cost == pytest.approx(10 * buy.fill_price + 1.0)
    assert buy.cash_after == pytest.approx(10_000 - buy.total_cost) and buy.position_after == 10
    assert buy.avg_price_after == pytest.approx(buy.fill_price) and buy.realized_pnl is None
    assert buy.stop_loss == pytest.approx(buy.fill_price * 0.95) and buy.take_profit == pytest.approx(buy.fill_price * 1.10)
    assert buy.participation_pct == pytest.approx(1.0) and buy.warnings == []
    data = buy.to_dict()
    assert data["accepted"] is True and data["model"] == "fixed" and isinstance(data["warnings"], list)

    big = sim.simulate(OrderRequest("AAPL", "BUY", 90, order_type="market"), snap, broker)
    assert big.accepted and any("average bar volume" in w for w in big.warnings)
    assert any("of the available cash" in w for w in big.warnings)

    too_big = sim.simulate(OrderRequest("AAPL", "BUY", 1000), snap, broker)
    assert not too_big.accepted and too_big.reason.startswith("Insufficient cash")

    no_position = sim.simulate(OrderRequest("AAPL", "SELL", 1), snap, broker)
    assert not no_position.accepted and "No open position" in no_position.reason

    broker.submit_market_order("AAPL", "BUY", 10, 90.0)
    sell = sim.simulate(OrderRequest("AAPL", "SELL", 4), snap, broker)
    assert sell.accepted and sell.fill_price == pytest.approx(ref * 0.999)
    assert sell.realized_pnl == pytest.approx((sell.fill_price - 90.0) * 4 - 1.0)
    assert sell.position_after == 6 and sell.avg_price_after == 90.0 and sell.total_cost == pytest.approx(4 * sell.fill_price - 1)
    assert sell.cash_after == pytest.approx(broker.cash() + sell.total_cost)
    too_many = sim.simulate(OrderRequest("AAPL", "SELL", 11), snap, broker)
    assert not too_many.accepted and "only 10 held" in too_many.reason

    # max_positions only warns (automatic signals would skip, manual orders may proceed).
    for i in range(3):
        broker.submit_market_order(f"S{i}", "BUY", 1, 1.0)
    crowded = sim.simulate(OrderRequest("NEW", "BUY", 1), snap, broker, max_positions=4)
    assert crowded.accepted and any("max_positions" in w for w in crowded.warnings)

    invalid = sim.simulate(OrderRequest("AAPL", "BUY", -1), snap, broker)
    assert not invalid.accepted and invalid.reason == "Quantity must be positive"


def test_simulate_limit_orders() -> None:
    sim = ExecutionSimulator(ExecutionConfig(slippage_model="fixed", slippage_bps=20))
    broker = PaperBroker(10_000)
    snap = MarketSnapshot(price=100.0)
    resting = sim.simulate(OrderRequest("AAPL", "BUY", 5, "limit", 95.0), snap, broker)
    assert resting.accepted and not resting.marketable and resting.fill_price == 95.0
    assert resting.slippage_cost == 0 and any("not marketable" in w for w in resting.warnings)
    marketable = sim.simulate(OrderRequest("AAPL", "BUY", 5, "limit", 105.0), snap, broker)
    assert marketable.marketable and marketable.fill_price == pytest.approx(100.2)  # market price + slippage, capped at limit
    capped = sim.simulate(OrderRequest("AAPL", "BUY", 5, "limit", 100.1), snap, broker)
    assert capped.marketable and capped.fill_price == pytest.approx(100.1)
    broker.submit_market_order("AAPL", "BUY", 5, 100.0)
    sell_resting = sim.simulate(OrderRequest("AAPL", "SELL", 5, "limit", 110.0), snap, broker)
    assert not sell_resting.marketable and sell_resting.fill_price == 110.0
    sell_now = sim.simulate(OrderRequest("AAPL", "SELL", 5, "limit", 90.0), snap, broker)
    assert sell_now.marketable and sell_now.fill_price == pytest.approx(99.8)


def test_little_history_and_expensive_commission_warnings() -> None:
    sim = ExecutionSimulator(ExecutionConfig(commission_per_order=5.0))
    snap = MarketSnapshot.from_bars(bars(n=10))
    result = sim.simulate(OrderRequest("AAPL", "BUY", 1), snap, PaperBroker(1_000))
    assert any("Little price history" in w for w in result.warnings)
    assert any("Commission is" in w for w in result.warnings)
    kwargs = sim.paper_fill_kwargs("BUY", 2, snap)
    assert kwargs == {"price": snap.price, "commission": 5.0}
