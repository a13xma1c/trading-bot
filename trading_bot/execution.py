"""Trade execution simulation.

Paper accounts fill orders instantly at the reference price, which flatters every
strategy.  :class:`ExecutionSimulator` models what a real fill would look like:

* **slippage** - a fixed half-spread (``slippage_bps``) and, with the ``volume`` model,
  square-root market impact (``impact_coefficient * volatility * sqrt(quantity / average
  bar volume)``), always against the trader;
* **commissions** - per order, per share and/or a percentage of the traded value with a
  minimum;
* **feasibility** - cash, position size, participation of the average bar volume and
  whether a limit order is marketable right now.

The same simulator prices the paper broker's automatic fills (so the P&L history
already includes costs) and powers the *Simulate* button of the order ticket.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from .broker import Broker, PaperBroker
from .config import ExecutionConfig

ORDER_TYPES = ("market", "limit")
SIDES = ("BUY", "SELL")


@dataclass
class OrderRequest:
    symbol: str
    side: str                      # BUY | SELL
    quantity: float
    order_type: str = "market"     # market | limit
    limit_price: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    strategy: str = "manual"

    def __post_init__(self) -> None:
        self.symbol = self.symbol.strip().upper()
        self.side = self.side.strip().upper()
        self.order_type = self.order_type.strip().lower()

    def validate(self) -> str | None:
        if not self.symbol or not self.symbol.replace(".", "").replace("-", "").isalnum():
            return "Invalid symbol"
        if self.side not in SIDES:
            return "Side must be BUY or SELL"
        if self.order_type not in ORDER_TYPES:
            return "Order type must be market or limit"
        if not math.isfinite(self.quantity) or self.quantity <= 0:
            return "Quantity must be positive"
        if self.order_type == "limit" and (self.limit_price is None or self.limit_price <= 0):
            return "A limit order needs a positive limit price"
        for name, value in (("Stop loss", self.stop_loss_pct), ("Take profit", self.take_profit_pct)):
            if value is not None and not 0 <= value < 100:
                return f"{name} % must be between 0 and 100"
        return None


@dataclass
class MarketSnapshot:
    """What the simulator knows about the market for one symbol."""

    price: float
    avg_volume: float | None = None    # average bar volume
    volatility: float | None = None    # typical bar return (std of close-to-close returns)
    bar_range_pct: float | None = None  # average (high-low)/close of recent bars
    bars: int = 0

    @classmethod
    def from_bars(cls, bars: pd.DataFrame | None, price: float | None = None, lookback: int = 50) -> MarketSnapshot:
        if bars is None or bars.empty:
            if price is None:
                raise ValueError("no market data for the symbol")
            return cls(price=float(price))
        recent = bars.tail(lookback)
        close = recent["close"].astype(float)
        last = float(price) if price is not None else float(close.iloc[-1])
        volume = recent["volume"].astype(float) if "volume" in recent else None
        avg_volume = float(volume.mean()) if volume is not None and len(volume) else None
        returns = close.pct_change().dropna()
        volatility = float(returns.std()) if len(returns) >= 5 else None
        ranges = ((recent["high"].astype(float) - recent["low"].astype(float)) / close).replace([math.inf, -math.inf], math.nan).dropna()
        bar_range = float(ranges.mean()) if len(ranges) else None
        return cls(price=last, avg_volume=avg_volume, volatility=volatility, bar_range_pct=bar_range, bars=len(recent))


@dataclass
class SimulationResult:
    symbol: str
    side: str
    order_type: str
    quantity: float
    reference_price: float
    fill_price: float
    slippage_per_share: float
    slippage_bps: float
    slippage_cost: float
    commission: float
    gross_value: float          # quantity * fill price
    total_cost: float           # what leaves/enters the account: gross +/- commission
    cash_before: float
    cash_after: float
    position_before: float
    position_after: float
    avg_price_after: float | None
    realized_pnl: float | None  # SELL only
    participation_pct: float | None
    marketable: bool            # limit orders: would fill at the current price
    accepted: bool
    reason: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None
    warnings: list[str] = field(default_factory=list)
    model: str = "none"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("fill_price", "slippage_per_share", "slippage_bps", "slippage_cost", "commission", "gross_value",
                    "total_cost", "cash_before", "cash_after"):
            data[key] = round(float(data[key]), 4)
        return data


class ExecutionSimulator:
    def __init__(self, cfg: ExecutionConfig) -> None:
        self.cfg = cfg

    # ---------------------------------------------------------------- costs
    def commission(self, quantity: float, value: float) -> float:
        cfg = self.cfg
        fee = cfg.commission_per_order + cfg.commission_per_share * quantity + cfg.commission_pct / 100.0 * abs(value)
        return round(max(fee, cfg.min_commission if fee > 0 or cfg.min_commission > 0 else 0.0), 6)

    def slippage_fraction(self, quantity: float, snapshot: MarketSnapshot) -> float:
        """Adverse price move as a fraction of the reference price."""
        cfg = self.cfg
        if cfg.slippage_model == "none":
            return 0.0
        fraction = cfg.slippage_bps / 10_000.0
        if cfg.slippage_model == "volume" and snapshot.avg_volume and snapshot.avg_volume > 0:
            volatility = snapshot.volatility
            if volatility is None or not math.isfinite(volatility):
                volatility = (snapshot.bar_range_pct or 0.01) / 2.0
            fraction += cfg.impact_coefficient * volatility * math.sqrt(quantity / snapshot.avg_volume)
        return fraction

    def fill_price(self, side: str, quantity: float, snapshot: MarketSnapshot) -> float:
        adverse = self.slippage_fraction(quantity, snapshot)
        price = snapshot.price * (1 + adverse) if side.upper() == "BUY" else snapshot.price * (1 - adverse)
        return max(price, 0.0001)

    # ------------------------------------------------------------- simulate
    def simulate(self, request: OrderRequest, snapshot: MarketSnapshot, broker: Broker,
                 max_positions: int | None = None) -> SimulationResult:
        """Dry-run ``request`` against ``broker``'s current cash and positions."""
        problem = request.validate()
        warnings: list[str] = []
        cash = broker.cash()
        pos = broker.position(request.symbol)
        held = pos.quantity if pos else 0.0
        avg_before = pos.avg_price if pos else None
        reference = snapshot.price
        marketable = True
        if request.order_type == "limit" and request.limit_price is not None:
            marketable = (request.side == "BUY" and request.limit_price >= reference) or \
                         (request.side == "SELL" and request.limit_price <= reference)
            # A limit order fills at its limit (or better; we assume the limit).
            fill = min(request.limit_price, self.fill_price("BUY", request.quantity, snapshot)) if request.side == "BUY" \
                else max(request.limit_price, self.fill_price("SELL", request.quantity, snapshot))
            if not marketable:
                fill = request.limit_price
                warnings.append("Limit price is not marketable right now: the order would rest until a bar trades "
                                "through it")
        else:
            fill = self.fill_price(request.side, request.quantity, snapshot)
        slip_per_share = fill - reference if request.side == "BUY" else reference - fill
        if request.order_type == "limit" and not marketable:
            slip_per_share = 0.0
        gross = request.quantity * fill
        fee = self.commission(request.quantity, gross)
        participation = None
        if snapshot.avg_volume and snapshot.avg_volume > 0:
            participation = 100.0 * request.quantity / snapshot.avg_volume
            if participation > self.cfg.max_participation_pct:
                warnings.append(f"Order is {participation:.0f}% of the average bar volume (limit "
                                f"{self.cfg.max_participation_pct:.0f}%): expect partial fills and heavier impact")
        if snapshot.bars and snapshot.bars < 20:
            warnings.append("Little price history available: slippage estimate is rough")
        realized = None
        stop_loss = take_profit = None
        if request.side == "BUY":
            total = gross + fee
            cash_after = cash - total
            position_after = held + request.quantity
            avg_after = ((avg_before or 0.0) * held + fill * request.quantity) / position_after if position_after else None
            if request.stop_loss_pct:
                stop_loss = fill * (1 - request.stop_loss_pct / 100.0)
            if request.take_profit_pct:
                take_profit = fill * (1 + request.take_profit_pct / 100.0)
            if problem is None and total > cash + 1e-9:
                problem = f"Insufficient cash: order needs {total:,.2f}, account has {cash:,.2f}"
            if problem is None and max_positions is not None and pos is None and len(broker.positions()) >= max_positions:
                warnings.append(f"The account already holds {max_positions} positions (trading.max_positions); "
                                "automatic signals would skip this symbol")
            if problem is None and total > 0.5 * cash:
                warnings.append(f"Order uses {100 * total / cash:.0f}% of the available cash")
        else:
            total = gross - fee
            cash_after = cash + total
            position_after = held - request.quantity
            avg_after = avg_before if position_after > 1e-9 else None
            if problem is None and held <= 0:
                problem = f"No open position in {request.symbol} (short selling is disabled)"
            elif problem is None and request.quantity > held + 1e-9:
                problem = f"Cannot sell {request.quantity:g} {request.symbol}: only {held:g} held"
            if pos is not None:
                realized = (fill - pos.avg_price) * min(request.quantity, held) - fee
        if fee > 0 and gross > 0 and fee / gross > 0.01:
            warnings.append(f"Commission is {100 * fee / gross:.1f}% of the order value")
        return SimulationResult(
            symbol=request.symbol, side=request.side, order_type=request.order_type, quantity=request.quantity,
            reference_price=reference, fill_price=fill, slippage_per_share=slip_per_share,
            slippage_bps=(slip_per_share / reference * 10_000.0) if reference else 0.0,
            slippage_cost=slip_per_share * request.quantity, commission=fee, gross_value=gross, total_cost=total,
            cash_before=cash, cash_after=cash_after, position_before=held, position_after=position_after,
            avg_price_after=avg_after, realized_pnl=realized, participation_pct=participation, marketable=marketable,
            accepted=problem is None, reason=problem or "", stop_loss=stop_loss, take_profit=take_profit,
            warnings=warnings, model=self.cfg.slippage_model,
        )

    def paper_fill_kwargs(self, side: str, quantity: float, snapshot: MarketSnapshot) -> dict[str, float]:
        """Price and commission for a paper-broker market fill."""
        price = self.fill_price(side, quantity, snapshot)
        return {"price": price, "commission": self.commission(quantity, price * quantity)}


def is_paper(broker: Broker) -> bool:
    return isinstance(broker, PaperBroker)


__all__ = ["ExecutionSimulator", "MarketSnapshot", "OrderRequest", "SimulationResult", "ORDER_TYPES", "SIDES", "is_paper"]
