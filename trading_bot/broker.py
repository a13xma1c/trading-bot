"""Order execution.

The bot's primary job is to *notify* when a trade should be executed.  Actual
execution goes through the :class:`Broker` interface; the bundled
:class:`PaperBroker` simulates fills so strategies can be evaluated with real
market data and zero risk.  A live broker adapter implements the same
interface.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    pass


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_price: float
    opened_at: datetime
    strategy: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.quantity

    def unrealized_pnl_pct(self, price: float) -> float:
        return (price / self.avg_price - 1.0) * 100.0 if self.avg_price else 0.0


@dataclass
class Fill:
    symbol: str
    side: str            # BUY / SELL
    quantity: float
    price: float
    timestamp: datetime
    strategy: str = ""
    realized_pnl: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


class Broker(ABC):
    @abstractmethod
    def submit_market_order(self, symbol: str, side: str, quantity: float, price: float,
                            strategy: str = "", stop_loss: float | None = None,
                            take_profit: float | None = None) -> Fill: ...

    @abstractmethod
    def positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def cash(self) -> float: ...

    def position(self, symbol: str) -> Position | None:
        return self.positions().get(symbol.upper())

    def equity(self, prices: dict[str, float]) -> float:
        total = self.cash()
        for sym, pos in self.positions().items():
            total += pos.market_value(prices.get(sym, pos.avg_price))
        return total


class PaperBroker(Broker):
    """In-memory simulated broker with optional JSON persistence."""

    def __init__(self, initial_cash: float = 100_000.0, state_file: str | os.PathLike[str] | None = None,
                 commission: float = 0.0):
        self._cash = float(initial_cash)
        self.initial_cash = float(initial_cash)
        self.commission = float(commission)
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._lock = threading.RLock()
        self._state_file = Path(state_file) if state_file else None
        self._load()

    # ------------------------------------------------------------ Broker API
    def submit_market_order(self, symbol, side, quantity, price, strategy="", stop_loss=None, take_profit=None) -> Fill:
        symbol = symbol.upper()
        side = side.upper()
        if quantity <= 0:
            raise BrokerError("quantity must be positive")
        if price <= 0:
            raise BrokerError("price must be positive")
        with self._lock:
            now = datetime.now(UTC)
            if side == "BUY":
                cost = quantity * price + self.commission
                if cost > self._cash + 1e-9:
                    raise BrokerError(f"Insufficient cash: need {cost:.2f}, have {self._cash:.2f}")
                self._cash -= cost
                pos = self._positions.get(symbol)
                if pos is None:
                    self._positions[symbol] = Position(symbol, quantity, price, now, strategy, stop_loss, take_profit)
                else:
                    total_qty = pos.quantity + quantity
                    pos.avg_price = (pos.avg_price * pos.quantity + price * quantity) / total_qty
                    pos.quantity = total_qty
                    pos.stop_loss = stop_loss if stop_loss is not None else pos.stop_loss
                    pos.take_profit = take_profit if take_profit is not None else pos.take_profit
                fill = Fill(symbol, side, quantity, price, now, strategy)
            elif side == "SELL":
                pos = self._positions.get(symbol)
                if pos is None or pos.quantity < quantity - 1e-9:
                    raise BrokerError(f"Cannot sell {quantity} {symbol}: no such position (short selling disabled)")
                pnl = (price - pos.avg_price) * quantity - self.commission
                self._cash += quantity * price - self.commission
                pos.quantity -= quantity
                if pos.quantity <= 1e-9:
                    del self._positions[symbol]
                fill = Fill(symbol, side, quantity, price, now, strategy, realized_pnl=pnl)
            else:
                raise BrokerError(f"Unknown side {side!r}")
            self._fills.append(fill)
            self._save()
        logger.info("Paper fill: %s %s %.4f @ %.2f (pnl %.2f)", side, symbol, quantity, price, fill.realized_pnl)
        return fill

    def positions(self) -> dict[str, Position]:
        with self._lock:
            return dict(self._positions)

    def cash(self) -> float:
        with self._lock:
            return self._cash

    def fills(self) -> list[Fill]:
        with self._lock:
            return list(self._fills)

    def realized_pnl(self) -> float:
        with self._lock:
            return sum(f.realized_pnl for f in self._fills)

    def reset(self) -> None:
        with self._lock:
            self._cash = self.initial_cash
            self._positions.clear()
            self._fills.clear()
            self._save()

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        if not self._state_file:
            return
        data = {
            "cash": self._cash,
            "initial_cash": self.initial_cash,
            "positions": [_serialize(p) for p in self._positions.values()],
            "fills": [_serialize(f) for f in self._fills[-500:]],
        }
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._state_file)
        except OSError as exc:  # pragma: no cover
            logger.warning("Could not persist broker state: %s", exc)

    def _load(self) -> None:
        if not self._state_file or not self._state_file.exists():
            return
        try:
            with self._state_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._cash = float(data.get("cash", self._cash))
            self.initial_cash = float(data.get("initial_cash", self.initial_cash))
            self._positions = {}
            for raw in data.get("positions", []):
                raw["opened_at"] = datetime.fromisoformat(raw["opened_at"])
                pos = Position(**raw)
                self._positions[pos.symbol] = pos
            self._fills = []
            for raw in data.get("fills", []):
                raw["timestamp"] = datetime.fromisoformat(raw["timestamp"])
                self._fills.append(Fill(**raw))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not load broker state from %s: %s", self._state_file, exc)


def _serialize(obj) -> dict[str, Any]:
    d = asdict(obj)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d
